"""Image analysis tool: OCR + OpenCV UI element detection.

Converts screenshots into structured text or simplified SVG that text-only
LLMs can understand. Preserves coordinates for desktop automation.

Two output formats:
  - text: structured text with elements, grid layout, and click targets
  - svg:  compact SVG (XML) with rectangles, text, and icons

Used by:
  1. LLM can call ``analyze_image`` tool explicitly
  2. Engine auto-fallback when the active model does not support vision
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult

logger = structlog.get_logger()

# Max dimension before running analysis (matches engine screenshot resize)
_MAX_ANALYSIS_DIMENSION = 1280

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TextElement:
    """A text element detected by OCR."""

    text: str
    bbox: tuple[int, int, int, int]  # (x1, y1, x2, y2)
    center: tuple[int, int]
    confidence: float = 0.0


@dataclass
class UIElement:
    """A non-text UI element detected by OpenCV."""

    elem_type: str  # panel | button | icon | divider | input
    bbox: tuple[int, int, int, int]  # (x1, y1, x2, y2)
    center: tuple[int, int]
    color: str = "#000000"  # hex color
    area: int = 0


@dataclass
class ImageAnalysis:
    """Combined analysis result."""

    width: int = 0
    height: int = 0
    text_elements: list[TextElement] = field(default_factory=list)
    ui_elements: list[UIElement] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------

_ocr_instance = None


def _get_ocr():
    """Lazy-init RapidOCR singleton."""
    global _ocr_instance
    if _ocr_instance is None:
        from rapidocr_onnxruntime import RapidOCR

        _ocr_instance = RapidOCR()
    return _ocr_instance


# ---------------------------------------------------------------------------
# Core analysis functions
# ---------------------------------------------------------------------------


def _resize_for_analysis(img):
    """Resize image if too large (save OCR time)."""
    from PIL import Image

    w, h = img.size
    if max(w, h) <= _MAX_ANALYSIS_DIMENSION:
        return img, w, h
    ratio = _MAX_ANALYSIS_DIMENSION / max(w, h)
    new_w, new_h = int(w * ratio), int(h * ratio)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    return resized, w, h  # return original dimensions for coordinate mapping


def _run_ocr(img) -> list[TextElement]:
    """Run OCR on a PIL Image and return text elements."""
    import numpy as np

    ocr = _get_ocr()

    # Convert PIL → numpy for RapidOCR
    img_array = np.array(img)

    result, _ = ocr(img_array)
    if not result:
        return []

    elements = []
    for item in result:
        bbox_points, text, confidence = item
        # bbox_points: [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]
        x1 = int(min(p[0] for p in bbox_points))
        y1 = int(min(p[1] for p in bbox_points))
        x2 = int(max(p[0] for p in bbox_points))
        y2 = int(max(p[1] for p in bbox_points))
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2

        elements.append(TextElement(
            text=text.strip(),
            bbox=(x1, y1, x2, y2),
            center=(cx, cy),
            confidence=confidence,
        ))

    return elements


def _detect_ui_elements(img, max_elements: int = 50) -> list[UIElement]:
    """Detect non-text UI elements using OpenCV."""
    import cv2
    import numpy as np

    img_array = np.array(img)
    if len(img_array.shape) == 2:
        gray = img_array
        color_img = cv2.cvtColor(img_array, cv2.COLOR_GRAY2BGR)
    else:
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
        color_img = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)

    h, w = gray.shape[:2]
    min_area = int(w * h * 0.0001)  # 0.01% of image = noise threshold
    max_area = int(w * h * 0.95)  # 95% of image = background, skip

    # Edge detection + contour finding
    edges = cv2.Canny(gray, 50, 150)
    # Dilate to close small gaps in edges
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    edges = cv2.dilate(edges, kernel, iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    elements: list[UIElement] = []

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area or area > max_area:
            continue

        x, y, cw, ch = cv2.boundingRect(contour)
        aspect_ratio = cw / max(ch, 1)

        # Classify element type by shape heuristics
        if area < min_area * 20:
            elem_type = "icon"
        elif 0.7 < aspect_ratio < 1.4 and area < min_area * 50:
            elem_type = "button"
        elif aspect_ratio > 6 and ch < h * 0.03:
            elem_type = "divider"
        elif aspect_ratio < 0.2 and cw < w * 0.03:
            elem_type = "divider"  # vertical divider
        else:
            elem_type = "panel"

        # Sample average color from the region
        roi = color_img[y:y + ch, x:x + cw]
        if roi.size > 0:
            avg = roi.mean(axis=(0, 1)).astype(int)
            hex_color = f"#{avg[2]:02x}{avg[1]:02x}{avg[0]:02x}"
        else:
            hex_color = "#000000"

        elements.append(UIElement(
            elem_type=elem_type,
            bbox=(x, y, x + cw, y + ch),
            center=(x + cw // 2, y + ch // 2),
            color=hex_color,
            area=area,
        ))

    # Sort by area descending, keep top N
    elements.sort(key=lambda e: e.area, reverse=True)
    return elements[:max_elements]


def _filter_overlapping(
    text_elements: list[TextElement],
    ui_elements: list[UIElement],
) -> list[UIElement]:
    """Remove UI elements that substantially overlap with text elements."""

    def _overlap_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
        """Compute IoU between two bounding boxes."""
        x1 = max(a[0], b[0])
        y1 = max(a[1], b[1])
        x2 = min(a[2], b[2])
        y2 = min(a[3], b[3])
        inter = max(0, x2 - x1) * max(0, y2 - y1)
        area_a = (a[2] - a[0]) * (a[3] - a[1])
        area_b = (b[2] - b[0]) * (b[3] - b[1])
        union = area_a + area_b - inter
        return inter / max(union, 1)

    text_bboxes = [t.bbox for t in text_elements]
    filtered = []
    for ui in ui_elements:
        # Skip UI elements that heavily overlap with any text element
        overlaps = any(_overlap_ratio(ui.bbox, tb) > 0.5 for tb in text_bboxes)
        if not overlaps:
            filtered.append(ui)
    return filtered


def _get_region_label(cx: int, cy: int, w: int, h: int) -> str:
    """Get a human-readable region label for a coordinate."""
    col = "left" if cx < w / 3 else ("right" if cx > w * 2 / 3 else "center")
    row = "top" if cy < h / 3 else ("bottom" if cy > h * 2 / 3 else "middle")
    if row == "middle" and col == "center":
        return "center"
    return f"{row}-{col}"


def _scale_coordinates(
    analysis: ImageAnalysis,
    orig_w: int,
    orig_h: int,
    resized_w: int,
    resized_h: int,
) -> ImageAnalysis:
    """Scale coordinates from resized image back to original dimensions."""
    if orig_w == resized_w and orig_h == resized_h:
        return analysis

    sx = orig_w / resized_w
    sy = orig_h / resized_h

    for t in analysis.text_elements:
        x1, y1, x2, y2 = t.bbox
        t.bbox = (int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy))
        t.center = (int(t.center[0] * sx), int(t.center[1] * sy))

    for u in analysis.ui_elements:
        x1, y1, x2, y2 = u.bbox
        u.bbox = (int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy))
        u.center = (int(u.center[0] * sx), int(u.center[1] * sy))

    return analysis


def _analyze(
    image_path: str,
    grid_size: int = 4,
    max_elements: int = 50,
) -> ImageAnalysis:
    """Core analysis: OCR + OpenCV on a single image file."""
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    orig_w, orig_h = img.size

    # Resize for faster processing
    resized_img, _, _ = _resize_for_analysis(img)
    resized_w, resized_h = resized_img.size

    # Run OCR
    try:
        text_elements = _run_ocr(resized_img)
    except ImportError:
        logger.warning("ocr_not_available", hint="pip install rapidocr-onnxruntime")
        text_elements = []
    except Exception as e:
        logger.warning("ocr_failed", error=str(e))
        text_elements = []

    # Run OpenCV UI detection
    try:
        ui_elements = _detect_ui_elements(resized_img, max_elements)
        ui_elements = _filter_overlapping(text_elements, ui_elements)
    except ImportError:
        logger.warning("opencv_not_available", hint="pip install opencv-python-headless")
        ui_elements = []
    except Exception as e:
        logger.warning("opencv_failed", error=str(e))
        ui_elements = []

    analysis = ImageAnalysis(
        width=orig_w,
        height=orig_h,
        text_elements=text_elements,
        ui_elements=ui_elements,
    )

    # Scale coordinates back to original resolution
    return _scale_coordinates(analysis, orig_w, orig_h, resized_w, resized_h)


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


def _format_grid(
    analysis: ImageAnalysis,
    grid_size: int,
) -> str:
    """Build spatial grid table."""
    w, h = analysis.width, analysis.height
    col_w = w / grid_size
    row_h = h / grid_size

    # Assign elements to grid cells
    grid: dict[tuple[int, int], list[str]] = {}
    for t in analysis.text_elements:
        c = int(t.center[0] / col_w)
        r = int(t.center[1] / row_h)
        c = min(c, grid_size - 1)
        r = min(r, grid_size - 1)
        label = f'"{t.text}"' if len(t.text) <= 15 else f'"{t.text[:12]}..."'
        grid.setdefault((r, c), []).append(label)

    for u in analysis.ui_elements:
        c = int(u.center[0] / col_w)
        r = int(u.center[1] / row_h)
        c = min(c, grid_size - 1)
        r = min(r, grid_size - 1)
        label = f"[{u.elem_type}]"
        grid.setdefault((r, c), []).append(label)

    # Build table
    col_ranges = [f"Col{i + 1}({int(i * col_w)}-{int((i + 1) * col_w)})" for i in range(grid_size)]
    header = "|     | " + " | ".join(col_ranges) + " |"
    sep = "|-----|" + "|".join(["---" for _ in range(grid_size)]) + "|"

    rows = [header, sep]
    for r in range(grid_size):
        cells = []
        for c in range(grid_size):
            items = grid.get((r, c), [])
            cell = ", ".join(items[:3])
            if len(items) > 3:
                cell += f" +{len(items) - 3}"
            cells.append(cell)
        rows.append(f"| R{r + 1}  | " + " | ".join(cells) + " |")

    return "\n".join(rows)


def analyze_image_to_text(
    image_path: str,
    grid_size: int = 4,
    detail_level: str = "standard",
    max_elements: int = 50,
) -> str:
    """Convert an image to structured text description with coordinates.

    This is the primary function called by both the tool and the engine
    auto-fallback.

    Args:
        image_path: Path to the image file.
        grid_size: NxN grid for spatial layout (default 4).
        detail_level: "brief" | "standard" | "detailed".
        max_elements: Maximum UI elements to include.

    Returns:
        Structured text description of the image.
    """
    analysis = _analyze(image_path, grid_size, max_elements)
    w, h = analysis.width, analysis.height

    lines: list[str] = [f"[Screen Analysis] {w}x{h}"]

    # --- Text elements ---
    if analysis.text_elements:
        lines.append(f"\n== Text Elements ({len(analysis.text_elements)} found) ==")
        for i, t in enumerate(analysis.text_elements, 1):
            region = _get_region_label(t.center[0], t.center[1], w, h)
            if detail_level == "brief":
                lines.append(f'[T{i}] "{t.text}" center:({t.center[0]},{t.center[1]})')
            else:
                lines.append(
                    f'[T{i}] "{t.text}" at ({t.bbox[0]},{t.bbox[1]})-'
                    f"({t.bbox[2]},{t.bbox[3]}) "
                    f"center:({t.center[0]},{t.center[1]}) — {region}"
                )
    else:
        lines.append("\n== Text Elements: none detected ==")

    # --- UI elements ---
    if detail_level != "brief" and analysis.ui_elements:
        lines.append(f"\n== UI Regions ({len(analysis.ui_elements)} found) ==")
        for i, u in enumerate(analysis.ui_elements, 1):
            region = _get_region_label(u.center[0], u.center[1], w, h)
            lines.append(
                f"[U{i}] {u.elem_type} ({u.bbox[0]},{u.bbox[1]})-"
                f"({u.bbox[2]},{u.bbox[3]}) "
                f"color:{u.color} — {region}"
            )

    # --- Spatial grid ---
    if detail_level in ("standard", "detailed"):
        lines.append(f"\n== Spatial Grid ({grid_size}x{grid_size}) ==")
        lines.append(_format_grid(analysis, grid_size))

    # --- Click targets ---
    lines.append("\n== Click Targets ==")
    for t in analysis.text_elements[:20]:
        lines.append(f'"{t.text}" -> click({t.center[0]}, {t.center[1]})')
    for u in analysis.ui_elements[:10]:
        if u.elem_type in ("icon", "button"):
            lines.append(f"[{u.elem_type}] -> click({u.center[0]}, {u.center[1]})")

    return "\n".join(lines)


def analyze_image_to_svg(
    image_path: str,
    grid_size: int = 4,
    max_elements: int = 50,
) -> str:
    """Convert an image to a simplified SVG representation.

    Returns compact SVG XML + a text summary with click targets.
    """
    analysis = _analyze(image_path, grid_size, max_elements)
    w, h = analysis.width, analysis.height

    svg_lines: list[str] = [
        f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">',
        f"  <!-- Screen: {w}x{h} -->",
    ]

    # Background
    svg_lines.append(f'  <rect x="0" y="0" width="{w}" height="{h}" fill="#1a1a2e" opacity="0.3"/>')

    # UI elements (panels, icons, dividers, buttons)
    for i, u in enumerate(analysis.ui_elements, 1):
        x1, y1, x2, y2 = u.bbox
        uw, uh = x2 - x1, y2 - y1
        data_type = u.elem_type
        uid = f"U{i}"

        if u.elem_type == "divider":
            if uw > uh:
                svg_lines.append(
                    f'  <line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y1}" '
                    f'stroke="{u.color}" stroke-width="1" data-type="divider" data-id="{uid}"/>'
                )
            else:
                svg_lines.append(
                    f'  <line x1="{x1}" y1="{y1}" x2="{x1}" y2="{y2}" '
                    f'stroke="{u.color}" stroke-width="1" data-type="divider" data-id="{uid}"/>'
                )
        else:
            opacity = "0.6" if data_type == "panel" else "0.8"
            svg_lines.append(
                f'  <rect x="{x1}" y="{y1}" width="{uw}" height="{uh}" '
                f'fill="{u.color}" opacity="{opacity}" '
                f'data-type="{data_type}" data-id="{uid}"/>'
            )

    # Text elements
    for i, t in enumerate(analysis.text_elements, 1):
        # Estimate font size from bounding box height
        _, y1, _, y2 = t.bbox
        font_size = max(8, min(16, y2 - y1))
        # Escape XML special characters
        safe_text = (
            t.text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        svg_lines.append(
            f'  <text x="{t.center[0]}" y="{t.center[1]}" '
            f'font-size="{font_size}" text-anchor="middle" fill="#ffffff" '
            f'data-id="T{i}">{safe_text}</text>'
        )

    svg_lines.append("</svg>")

    # Build summary
    summary_lines = [
        "\n".join(svg_lines),
        "",
        f"[Screen Analysis] {w}x{h} | "
        f"Text:{len(analysis.text_elements)} | "
        f"UI:{len(analysis.ui_elements)}",
        "",
        "Click Targets:",
    ]
    for t in analysis.text_elements[:15]:
        summary_lines.append(f'  "{t.text}" -> click({t.center[0]}, {t.center[1]})')
    for u in analysis.ui_elements[:10]:
        if u.elem_type in ("icon", "button"):
            summary_lines.append(f"  [{u.elem_type}] -> click({u.center[0]}, {u.center[1]})")

    return "\n".join(summary_lines)


# ---------------------------------------------------------------------------
# Convenience wrapper for Engine auto-fallback
# ---------------------------------------------------------------------------


def run_image_analysis(
    image_path: str,
    fallback_format: str = "text",
    detail_level: str = "standard",
    grid_size: int = 4,
    max_elements: int = 50,
) -> str:
    """High-level wrapper used by engine & agents for auto-fallback.

    Catches all errors gracefully so the engine never crashes.
    """
    try:
        if fallback_format == "svg":
            return analyze_image_to_svg(image_path, grid_size, max_elements)
        return analyze_image_to_text(image_path, grid_size, detail_level, max_elements)
    except ImportError as e:
        missing = str(e)
        logger.warning("image_analysis_import_error", error=missing)
        return (
            f"[Image analysis unavailable]\n"
            f"Missing dependency: {missing}\n"
            f"Install with: pip install rapidocr-onnxruntime opencv-python-headless\n"
            f"Image saved at: {image_path}"
        )
    except Exception as e:
        logger.warning("image_analysis_failed", error=str(e))
        return (
            f"[Image analysis failed: {e}]\n"
            f"Image saved at: {image_path}"
        )


# ---------------------------------------------------------------------------
# Tool class (auto-discovered by ToolSystem)
# ---------------------------------------------------------------------------


class AnalyzeImageTool(BaseTool):
    """Analyze an image using OCR + OpenCV to extract text and UI elements."""

    name = "analyze_image"
    description = (
        "Analyze an image file using OCR and UI detection to extract text elements, "
        "UI regions (buttons, panels, icons, dividers), and their coordinates. "
        "Returns structured text or simplified SVG that describes the screen layout. "
        "Useful when you need exact coordinate information for mouse_action, "
        "or when the current model cannot process images directly. "
        "Supports CJK text (Chinese, Japanese, Korean)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": "Absolute path to the image file to analyze",
            },
            "output_format": {
                "type": "string",
                "enum": ["text", "svg"],
                "description": (
                    "Output format: 'text' for structured text description, "
                    "'svg' for simplified SVG with XML coordinates. Default: text"
                ),
            },
            "detail_level": {
                "type": "string",
                "enum": ["brief", "standard", "detailed"],
                "description": (
                    "Level of detail (text format only): "
                    "'brief' (text + centers), "
                    "'standard' (text + bboxes + grid), "
                    "'detailed' (full analysis + grid). Default: standard"
                ),
            },
            "grid_size": {
                "type": "integer",
                "description": "Grid size for spatial layout (NxN). Default: 4",
            },
        },
        "required": ["image_path"],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        image_path = params.get("image_path", "")
        output_format = params.get("output_format", "text")
        detail_level = params.get("detail_level", "standard")
        grid_size = params.get("grid_size", 4)

        if not image_path:
            return ToolResult.fail("image_path is required")

        if not Path(image_path).exists():
            return ToolResult.fail(f"Image not found: {image_path}")

        try:
            if output_format == "svg":
                result = analyze_image_to_svg(image_path, grid_size)
            else:
                result = analyze_image_to_text(image_path, grid_size, detail_level)

            return ToolResult.ok(result)

        except ImportError as e:
            return ToolResult.fail(
                f"Missing dependency: {e}. "
                "Install with: pip install rapidocr-onnxruntime opencv-python-headless"
            )
        except Exception as e:
            return ToolResult.fail(f"Image analysis failed: {e}")
