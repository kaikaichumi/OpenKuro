"""DPI scaling detection for Windows desktop automation.

On Windows with display scaling (e.g., 125%, 150%, 200%), the physical
pixel coordinates from mss screenshots do NOT match the logical coordinates
used by pyautogui.  For example, at 150% scaling on a 1920x1080 display:

  - mss captures at 2880x1620 (physical pixels)
  - pyautogui operates at 1920x1080 (logical pixels)
  - OCR detects text at physical coord (1440, 810)
  - But pyautogui.click(1440, 810) is WRONG — should be (960, 540)

This module detects the scaling factor and provides a converter.
"""

from __future__ import annotations

import platform
import structlog

logger = structlog.get_logger()

_cached_scale: float | None = None


def get_dpi_scale() -> float:
    """Return the DPI scale factor (physical / logical).

    1.0 = no scaling, 1.25 = 125%, 1.5 = 150%, 2.0 = 200%, etc.
    On non-Windows or if detection fails, returns 1.0.
    """
    global _cached_scale
    if _cached_scale is not None:
        return _cached_scale

    scale = _detect_scale()
    _cached_scale = scale
    return scale


def reset_cache() -> None:
    """Reset cached scale factor (useful if display settings change)."""
    global _cached_scale
    _cached_scale = None


def physical_to_logical(x: int, y: int, scale: float | None = None) -> tuple[int, int]:
    """Convert physical (screenshot) coordinates to logical (pyautogui) coordinates."""
    s = scale or get_dpi_scale()
    if s == 1.0:
        return x, y
    return int(x / s), int(y / s)


def logical_to_physical(x: int, y: int, scale: float | None = None) -> tuple[int, int]:
    """Convert logical (pyautogui) coordinates to physical (screenshot) coordinates."""
    s = scale or get_dpi_scale()
    if s == 1.0:
        return x, y
    return int(x * s), int(y * s)


def _detect_scale() -> float:
    """Detect the actual DPI scale by comparing mss and pyautogui sizes."""

    # Method 1: Compare mss capture size with pyautogui logical size
    try:
        import mss
        import pyautogui

        pyautogui.FAILSAFE = True
        logical_w, logical_h = pyautogui.size()

        with mss.mss() as sct:
            monitor = sct.monitors[1]  # Primary monitor
            physical_w = monitor["width"]
            physical_h = monitor["height"]

        if logical_w > 0 and physical_w > 0:
            scale_x = physical_w / logical_w
            scale_y = physical_h / logical_h
            # Use the average (they should be the same)
            scale = round((scale_x + scale_y) / 2, 4)
            if 0.8 < scale < 4.0:  # Sanity check
                if scale != 1.0:
                    logger.info(
                        "dpi_scale_detected",
                        scale=scale,
                        physical=f"{physical_w}x{physical_h}",
                        logical=f"{logical_w}x{logical_h}",
                    )
                return scale
    except Exception as e:
        logger.debug("dpi_detect_mss_failed", error=str(e))

    # Method 2: Windows API (fallback)
    if platform.system() == "Windows":
        try:
            import ctypes
            # Make this process DPI-aware first
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor V2
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass

            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
            scale = dpi / 96.0
            if 0.8 < scale < 4.0:
                logger.info("dpi_scale_from_api", scale=scale, dpi=dpi)
                return scale
        except Exception as e:
            logger.debug("dpi_detect_winapi_failed", error=str(e))

    return 1.0
