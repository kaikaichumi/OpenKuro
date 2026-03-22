"""ComfyUI integration plugin — txt2img, img2img, custom workflow, status check.

Connects to a remote ComfyUI instance via its REST API (HTTP).
Configuration is read from ``config.comfyui`` (managed in the Web UI).

Dependencies: httpx (already in project requirements).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from src.core.security.egress import EgressBroker
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_comfyui_cfg(context: ToolContext) -> dict[str, Any]:
    """Extract ComfyUI config from the runtime KuroConfig."""
    cfg = getattr(context.config, "comfyui", None)
    if cfg is None:
        return {}
    return {
        "enabled": getattr(cfg, "enabled", False),
        "api_url": getattr(cfg, "api_url", "http://localhost:8188"),
        "timeout": getattr(cfg, "timeout", 120),
        "output_dir": getattr(cfg, "output_dir", ""),
        "default_checkpoint": getattr(cfg, "default_checkpoint", ""),
        "default_width": getattr(cfg, "default_width", 512),
        "default_height": getattr(cfg, "default_height", 512),
        "default_steps": getattr(cfg, "default_steps", 20),
        "default_cfg_scale": getattr(cfg, "default_cfg_scale", 7.0),
        "default_sampler": getattr(cfg, "default_sampler", "euler"),
        "default_scheduler": getattr(cfg, "default_scheduler", "normal"),
    }


def _get_egress_broker(context: ToolContext | None) -> EgressBroker:
    cfg = getattr(context, "config", None) if context is not None else None
    egress_cfg = getattr(cfg, "egress_policy", None) if cfg is not None else None
    return EgressBroker(egress_cfg)


def _assert_egress_allowed(
    broker: EgressBroker | None,
    url: str,
    *,
    tool_name: str,
) -> None:
    if broker is None:
        return
    allowed, reason = broker.check_url(url, tool_name=tool_name)
    if not allowed:
        raise PermissionError(f"Egress blocked URL '{url}': {reason}")


async def _api_get(
    url: str,
    timeout: int,
    *,
    broker: EgressBroker | None = None,
    tool_name: str = "comfyui_api",
) -> dict:
    _assert_egress_allowed(broker, url, tool_name=tool_name)
    proxy = broker.resolve_proxy(url, tool_name=tool_name) if broker else None
    async with httpx.AsyncClient(timeout=timeout, proxy=proxy or None) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def _api_post(
    url: str,
    data: dict,
    timeout: int,
    *,
    broker: EgressBroker | None = None,
    tool_name: str = "comfyui_api",
) -> dict:
    _assert_egress_allowed(broker, url, tool_name=tool_name)
    proxy = broker.resolve_proxy(url, tool_name=tool_name) if broker else None
    async with httpx.AsyncClient(timeout=timeout, proxy=proxy or None) as client:
        resp = await client.post(url, json=data)
        resp.raise_for_status()
        return resp.json()


async def _download_image(
    url: str,
    timeout: int,
    *,
    broker: EgressBroker | None = None,
    tool_name: str = "comfyui_download",
) -> bytes:
    _assert_egress_allowed(broker, url, tool_name=tool_name)
    proxy = broker.resolve_proxy(url, tool_name=tool_name) if broker else None
    async with httpx.AsyncClient(
        timeout=timeout,
        proxy=proxy or None,
        follow_redirects=True,
        headers={"User-Agent": "OpenKuro/ComfyUI"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


def _normalize_image_ref(image_ref: str) -> str:
    """Normalize image reference text to a usable raw ref/path."""
    raw = str(image_ref or "").strip().strip("'\"")
    if not raw:
        return ""

    # Discord/Markdown wrappers like <https://...> or ![img](https://...)
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1].strip()

    md_link_match = re.search(r"\((https?://[^\s)]+)\)", raw, flags=re.IGNORECASE)
    if md_link_match:
        return md_link_match.group(1).strip()

    md_data_match = re.search(r"\((data:image/[^\s)]+)\)", raw, flags=re.IGNORECASE)
    if md_data_match:
        return md_data_match.group(1).strip()

    # Fallback: first URL in free-form text
    url_match = re.search(r"(https?://\S+)", raw, flags=re.IGNORECASE)
    if url_match:
        return url_match.group(1).rstrip('>)}]\'",.;')

    return raw


def _ext_from_data_uri(uri: str) -> str:
    lower = uri.lower()
    if lower.startswith("data:image/jpeg"):
        return ".jpg"
    if lower.startswith("data:image/webp"):
        return ".webp"
    if lower.startswith("data:image/bmp"):
        return ".bmp"
    if lower.startswith("data:image/gif"):
        return ".gif"
    return ".png"


def _ext_from_url(url: str) -> str:
    try:
        path = urlparse(url).path or ""
    except Exception:
        path = ""
    suffix = Path(path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".png"


async def _resolve_image_ref_to_local_path(
    image_ref: str,
    timeout: int,
    *,
    broker: EgressBroker | None = None,
    tool_name: str = "comfyui_img2img",
) -> tuple[str, bool]:
    """Resolve local path / URL / data URI to a local file path.

    Returns: (local_path, is_temporary_file)
    """
    raw = _normalize_image_ref(image_ref)
    if not raw:
        raise FileNotFoundError("image_path is required")

    p = Path(raw)
    if p.is_file():
        return str(p), False

    if raw.startswith("data:image/"):
        if "," not in raw:
            raise ValueError("Invalid data URI image format")
        header, payload = raw.split(",", 1)
        if ";base64" not in header.lower():
            raise ValueError("Data URI must be base64-encoded")
        image_bytes = base64.b64decode(payload, validate=False)
        suffix = _ext_from_data_uri(header)
        tmp = tempfile.NamedTemporaryFile(
            suffix=suffix,
            prefix="kuro_img2img_",
            delete=False,
        )
        tmp.write(image_bytes)
        tmp.close()
        return tmp.name, True

    if raw.lower().startswith("http://") or raw.lower().startswith("https://"):
        image_bytes = await _download_image(
            raw,
            timeout=min(timeout, 60),
            broker=broker,
            tool_name=tool_name,
        )
        suffix = _ext_from_url(raw)
        tmp = tempfile.NamedTemporaryFile(
            suffix=suffix,
            prefix="kuro_img2img_",
            delete=False,
        )
        tmp.write(image_bytes)
        tmp.close()
        return tmp.name, True

    raise FileNotFoundError(f"Image not found: {image_ref}")


async def _upload_image_to_comfyui(
    api_url: str,
    image_path: str,
    timeout: int,
    *,
    broker: EgressBroker | None = None,
    tool_name: str = "comfyui_img2img",
) -> str:
    """Upload a local image to ComfyUI and return the server-side filename.

    ComfyUI stores uploaded images in its ``input/`` folder and returns the
    filename that ``LoadImage`` can reference.
    """
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"Image not found: {image_path}")

    # Guess content type
    suffix = path.suffix.lower()
    content_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(suffix, "image/png")

    upload_url = f"{api_url}/upload/image"
    _assert_egress_allowed(broker, upload_url, tool_name=tool_name)
    proxy = broker.resolve_proxy(upload_url, tool_name=tool_name) if broker else None
    async with httpx.AsyncClient(timeout=timeout, proxy=proxy or None) as client:
        resp = await client.post(
            upload_url,
            files={"image": (path.name, path.read_bytes(), content_type)},
            data={"overwrite": "true"},
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("name", path.name)


def _build_img2img_workflow(
    prompt: str,
    negative_prompt: str,
    checkpoint: str,
    image_name: str,
    steps: int,
    cfg_scale: float,
    sampler: str,
    scheduler: str,
    seed: int,
    denoise: float,
) -> dict:
    """Build a ComfyUI img2img API workflow.

    Key difference from txt2img:
    - Uses LoadImage → VAEEncode instead of EmptyLatentImage
    - KSampler denoise < 1.0 to preserve original image features
    """
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg_scale,
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": denoise,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["10", 0],
            },
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        # LoadImage — loads the uploaded image by server-side filename
        "5": {
            "class_type": "LoadImage",
            "inputs": {"image": image_name},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": prompt,
                "clip": ["4", 1],
            },
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": negative_prompt,
                "clip": ["4", 1],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["3", 0],
                "vae": ["4", 2],
            },
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": "kuro_img2img",
                "images": ["8", 0],
            },
        },
        # VAEEncode — encode the input image into latent space
        "10": {
            "class_type": "VAEEncode",
            "inputs": {
                "pixels": ["5", 0],
                "vae": ["4", 2],
            },
        },
    }


def _build_txt2img_workflow(
    prompt: str,
    negative_prompt: str,
    checkpoint: str,
    width: int,
    height: int,
    steps: int,
    cfg_scale: float,
    sampler: str,
    scheduler: str,
    seed: int,
    batch_size: int,
) -> dict:
    """Build a standard ComfyUI txt2img API workflow (prompt format)."""
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg_scale,
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": width,
                "height": height,
                "batch_size": batch_size,
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": prompt,
                "clip": ["4", 1],
            },
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": negative_prompt,
                "clip": ["4", 1],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["3", 0],
                "vae": ["4", 2],
            },
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": "kuro",
                "images": ["8", 0],
            },
        },
    }


async def _queue_prompt_and_wait(
    api_url: str,
    workflow: dict,
    timeout: int,
    *,
    broker: EgressBroker | None = None,
    tool_name: str = "comfyui_api",
) -> dict:
    """Queue a prompt on ComfyUI and poll until it finishes.

    Returns the history entry for the completed prompt.
    """
    client_id = str(uuid.uuid4())
    payload = {"prompt": workflow, "client_id": client_id}
    result = await _api_post(
        f"{api_url}/prompt",
        payload,
        timeout,
        broker=broker,
        tool_name=tool_name,
    )
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return a prompt_id: {result}")

    # Poll history until our prompt_id shows up as completed
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        history = await _api_get(
            f"{api_url}/history/{prompt_id}",
            timeout=10,
            broker=broker,
            tool_name=tool_name,
        )
        entry = history.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("completed", False) or status.get("status_str") == "success":
                return entry
            # Check for errors
            msgs = status.get("messages", [])
            for msg in msgs:
                if isinstance(msg, list) and len(msg) >= 2:
                    if msg[0] == "execution_error":
                        raise RuntimeError(f"ComfyUI execution error: {msg[1]}")
        await asyncio.sleep(1.5)

    raise TimeoutError(f"ComfyUI prompt {prompt_id} did not complete within {timeout}s")


def _extract_image_filenames(history_entry: dict) -> list[dict[str, str]]:
    """Extract output image info from a completed history entry."""
    images = []
    outputs = history_entry.get("outputs", {})
    for _node_id, node_out in outputs.items():
        for img in node_out.get("images", []):
            images.append({
                "filename": img.get("filename", ""),
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output"),
            })
    return images


def _save_images_locally(
    output_dir: str, img_info: dict[str, str], img_bytes: bytes
) -> Path:
    """Save downloaded image bytes to the local output directory."""
    if not output_dir:
        output_dir = str(Path.home() / ".kuro" / "comfyui_output")
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    local_path = Path(output_dir) / img_info["filename"]
    local_path.write_bytes(img_bytes)
    return local_path


# ---------------------------------------------------------------------------
# Tool 1: comfyui_generate  — Text-to-Image
# ---------------------------------------------------------------------------


class ComfyUIGenerateTool(BaseTool):
    name = "comfyui_generate"
    description = (
        "Generate an image using ComfyUI (txt2img). "
        "Sends a prompt to a remote ComfyUI server and returns the generated image. "
        "Supports custom resolution, steps, CFG scale, sampler, and seed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The positive prompt describing the desired image.",
            },
            "negative_prompt": {
                "type": "string",
                "description": "Negative prompt (things to avoid). Default: ''",
            },
            "checkpoint": {
                "type": "string",
                "description": (
                    "Checkpoint model name (e.g. 'v1-5-pruned.safetensors'). "
                    "Leave empty for the default configured in Settings."
                ),
            },
            "width": {
                "type": "integer",
                "description": "Image width in pixels. Default from config.",
            },
            "height": {
                "type": "integer",
                "description": "Image height in pixels. Default from config.",
            },
            "steps": {
                "type": "integer",
                "description": "Number of sampling steps. Default from config.",
            },
            "cfg_scale": {
                "type": "number",
                "description": "CFG scale (guidance). Default from config.",
            },
            "sampler": {
                "type": "string",
                "description": "Sampler name (euler, euler_ancestral, dpmpp_2m, etc.). Default from config.",
            },
            "scheduler": {
                "type": "string",
                "description": "Scheduler (normal, karras, exponential, etc.). Default from config.",
            },
            "seed": {
                "type": "integer",
                "description": "Random seed. -1 for random.",
            },
            "batch_size": {
                "type": "integer",
                "description": "Number of images to generate. Default: 1",
            },
        },
        "required": ["prompt"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        cfg = _get_comfyui_cfg(context)
        if not cfg.get("enabled"):
            return ToolResult.fail(
                "ComfyUI integration is disabled. Enable it in Settings > ComfyUI."
            )

        api_url = cfg["api_url"].rstrip("/")
        timeout = cfg["timeout"]
        broker = _get_egress_broker(context)

        prompt_text = params["prompt"]
        negative = params.get("negative_prompt", "")
        checkpoint = params.get("checkpoint") or cfg.get("default_checkpoint", "")
        if not checkpoint:
            return ToolResult.fail(
                "No checkpoint model specified and no default configured. "
                "Set a default in Settings > ComfyUI or pass 'checkpoint' parameter."
            )

        width = params.get("width") or cfg.get("default_width", 512)
        height = params.get("height") or cfg.get("default_height", 512)
        steps = params.get("steps") or cfg.get("default_steps", 20)
        cfg_scale = params.get("cfg_scale") or cfg.get("default_cfg_scale", 7.0)
        sampler = params.get("sampler") or cfg.get("default_sampler", "euler")
        scheduler = params.get("scheduler") or cfg.get("default_scheduler", "normal")
        seed = params.get("seed", -1)
        if seed == -1:
            import random

            seed = random.randint(0, 2**32 - 1)
        batch_size = params.get("batch_size", 1)

        workflow = _build_txt2img_workflow(
            prompt=prompt_text,
            negative_prompt=negative,
            checkpoint=checkpoint,
            width=width,
            height=height,
            steps=steps,
            cfg_scale=cfg_scale,
            sampler=sampler,
            scheduler=scheduler,
            seed=seed,
            batch_size=batch_size,
        )

        try:
            history = await _queue_prompt_and_wait(
                api_url,
                workflow,
                timeout,
                broker=broker,
                tool_name=self.name,
            )
        except Exception as e:
            return ToolResult.fail(f"ComfyUI generation failed: {e}")

        images = _extract_image_filenames(history)
        if not images:
            return ToolResult.fail("ComfyUI returned no images.")

        # Download first image and save locally
        img_info = images[0]
        img_url = (
            f"{api_url}/view?"
            f"filename={quote(img_info['filename'])}"
            f"&subfolder={quote(img_info['subfolder'])}"
            f"&type={quote(img_info['type'])}"
        )
        try:
            img_bytes = await _download_image(
                img_url,
                timeout=30,
                broker=broker,
                tool_name=self.name,
            )
        except Exception as e:
            return ToolResult.fail(f"Failed to download generated image: {e}")

        local_path = _save_images_locally(
            cfg.get("output_dir", ""), img_info, img_bytes
        )

        summary_lines = [
            "Image generated successfully!",
            f"Saved to: {local_path}",
            f"Prompt: {prompt_text[:80]}{'...' if len(prompt_text) > 80 else ''}",
            f"Size: {width}x{height} | Steps: {steps} | CFG: {cfg_scale}",
            f"Seed: {seed} | Sampler: {sampler}/{scheduler}",
            f"Checkpoint: {checkpoint}",
        ]
        if len(images) > 1:
            summary_lines.append(f"Batch: {len(images)} images (first one saved)")

        return ToolResult.ok(
            "\n".join(summary_lines),
            image_path=str(local_path),
            seed=seed,
            width=width,
            height=height,
            all_images=[i["filename"] for i in images],
        )


# ---------------------------------------------------------------------------
# Tool 2: comfyui_img2img  — Image-to-Image
# ---------------------------------------------------------------------------


class ComfyUIImg2ImgTool(BaseTool):
    name = "comfyui_img2img"
    description = (
        "Modify an existing image using ComfyUI (img2img). "
        "Upload a source image, apply a prompt to transform it. "
        "Use 'denoise' to control how much to change: "
        "0.2-0.4 = subtle tweaks, 0.5-0.7 = moderate changes, 0.8-1.0 = heavy redraw. "
        "Great for style transfer, adding details, fixing parts, or re-interpreting a scene."
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": (
                    "Source image path/reference. Supports absolute local path, "
                    "http(s) image URL, or data:image/... base64 URI."
                ),
            },
            "image_url": {
                "type": "string",
                "description": (
                    "Alias of image_path for URL inputs. "
                    "Supports http(s) image URL or data:image/... base64 URI."
                ),
            },
            "prompt": {
                "type": "string",
                "description": "The positive prompt describing the desired output.",
            },
            "negative_prompt": {
                "type": "string",
                "description": "Negative prompt (things to avoid). Default: ''",
            },
            "denoise": {
                "type": "number",
                "description": (
                    "Denoise strength (0.0-1.0). Controls how much to change the image. "
                    "0.3 = keep most of original, 0.7 = change a lot. Default: 0.5"
                ),
            },
            "checkpoint": {
                "type": "string",
                "description": (
                    "Checkpoint model name. Leave empty for the default in Settings."
                ),
            },
            "steps": {
                "type": "integer",
                "description": "Number of sampling steps. Default from config.",
            },
            "cfg_scale": {
                "type": "number",
                "description": "CFG scale (guidance). Default from config.",
            },
            "sampler": {
                "type": "string",
                "description": "Sampler name. Default from config.",
            },
            "scheduler": {
                "type": "string",
                "description": "Scheduler. Default from config.",
            },
            "seed": {
                "type": "integer",
                "description": "Random seed. -1 for random.",
            },
        },
        "required": ["image_path", "prompt"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        cfg = _get_comfyui_cfg(context)
        if not cfg.get("enabled"):
            return ToolResult.fail(
                "ComfyUI integration is disabled. Enable it in Settings > ComfyUI."
            )

        api_url = cfg["api_url"].rstrip("/")
        timeout = cfg["timeout"]
        broker = _get_egress_broker(context)

        image_ref = (
            params.get("image_path")
            or params.get("image_url")
            or params.get("source_image")
        )
        if not image_ref:
            return ToolResult.fail(
                "image_path is required (supports local path, http(s) URL, or data URI)."
            )
        prompt_text = params["prompt"]
        negative = params.get("negative_prompt", "")
        denoise = params.get("denoise", 0.5)
        denoise = max(0.0, min(1.0, denoise))  # Clamp to [0, 1]

        checkpoint = params.get("checkpoint") or cfg.get("default_checkpoint", "")
        if not checkpoint:
            return ToolResult.fail(
                "No checkpoint model specified and no default configured. "
                "Set a default in Settings > ComfyUI or pass 'checkpoint' parameter."
            )

        steps = params.get("steps") or cfg.get("default_steps", 20)
        cfg_scale = params.get("cfg_scale") or cfg.get("default_cfg_scale", 7.0)
        sampler = params.get("sampler") or cfg.get("default_sampler", "euler")
        scheduler = params.get("scheduler") or cfg.get("default_scheduler", "normal")
        seed = params.get("seed", -1)
        if seed == -1:
            import random

            seed = random.randint(0, 2**32 - 1)

        resolved_image_path = ""
        temp_image_created = False
        try:
            # Step 1: Resolve source image (path/url/data-uri) and upload to ComfyUI
            try:
                resolved_image_path, temp_image_created = await _resolve_image_ref_to_local_path(
                    str(image_ref),
                    timeout,
                    broker=broker,
                    tool_name=self.name,
                )
                server_image_name = await _upload_image_to_comfyui(
                    api_url,
                    resolved_image_path,
                    timeout,
                    broker=broker,
                    tool_name=self.name,
                )
            except FileNotFoundError as e:
                return ToolResult.fail(str(e))
            except Exception as e:
                return ToolResult.fail(f"Failed to upload image to ComfyUI: {e}")

            # Step 2: Build and execute img2img workflow
            workflow = _build_img2img_workflow(
                prompt=prompt_text,
                negative_prompt=negative,
                checkpoint=checkpoint,
                image_name=server_image_name,
                steps=steps,
                cfg_scale=cfg_scale,
                sampler=sampler,
                scheduler=scheduler,
                seed=seed,
                denoise=denoise,
            )

            try:
                history = await _queue_prompt_and_wait(
                    api_url,
                    workflow,
                    timeout,
                    broker=broker,
                    tool_name=self.name,
                )
            except Exception as e:
                return ToolResult.fail(f"ComfyUI img2img failed: {e}")

            images = _extract_image_filenames(history)
            if not images:
                return ToolResult.fail("ComfyUI returned no images.")

            # Step 3: Download and save result
            img_info = images[0]
            img_url = (
                f"{api_url}/view?"
                f"filename={quote(img_info['filename'])}"
                f"&subfolder={quote(img_info['subfolder'])}"
                f"&type={quote(img_info['type'])}"
            )
            try:
                img_bytes = await _download_image(
                    img_url,
                    timeout=30,
                    broker=broker,
                    tool_name=self.name,
                )
            except Exception as e:
                return ToolResult.fail(f"Failed to download generated image: {e}")

            local_path = _save_images_locally(
                cfg.get("output_dir", ""), img_info, img_bytes
            )

            summary_lines = [
                "img2img completed successfully!",
                f"Source: {image_ref}",
                f"Saved to: {local_path}",
                f"Prompt: {prompt_text[:80]}{'...' if len(prompt_text) > 80 else ''}",
                f"Denoise: {denoise} | Steps: {steps} | CFG: {cfg_scale}",
                f"Seed: {seed} | Sampler: {sampler}/{scheduler}",
                f"Checkpoint: {checkpoint}",
            ]

            return ToolResult.ok(
                "\n".join(summary_lines),
                image_path=str(local_path),
                source_image=image_ref,
                seed=seed,
                denoise=denoise,
            )
        finally:
            if temp_image_created and resolved_image_path:
                try:
                    Path(resolved_image_path).unlink(missing_ok=True)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Tool 3: comfyui_status  — Check server status & list models
# ---------------------------------------------------------------------------


class ComfyUIStatusTool(BaseTool):
    name = "comfyui_status"
    description = (
        "Check ComfyUI server status: connectivity, queue length, "
        "available checkpoint models, samplers, and schedulers."
    )
    parameters = {
        "type": "object",
        "properties": {
            "list_models": {
                "type": "boolean",
                "description": "Whether to list available checkpoint models. Default: true",
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.LOW

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        cfg = _get_comfyui_cfg(context)
        if not cfg.get("enabled"):
            return ToolResult.fail(
                "ComfyUI integration is disabled. Enable it in Settings > ComfyUI."
            )

        api_url = cfg["api_url"].rstrip("/")
        timeout = min(cfg.get("timeout", 30), 15)
        list_models = params.get("list_models", True)
        broker = _get_egress_broker(context)

        lines: list[str] = []

        # 1. System stats / queue
        try:
            system = await _api_get(
                f"{api_url}/system_stats",
                timeout,
                broker=broker,
                tool_name=self.name,
            )
            queue = await _api_get(
                f"{api_url}/queue",
                timeout,
                broker=broker,
                tool_name=self.name,
            )
            running = len(queue.get("queue_running", []))
            pending = len(queue.get("queue_pending", []))
            lines.append(f"ComfyUI is online at {api_url}")

            sys_info = system.get("system", {})
            if sys_info:
                os_name = sys_info.get("os", "unknown")
                python_ver = sys_info.get("python_version", "?")
                lines.append(f"OS: {os_name} | Python: {python_ver}")

            devices = system.get("devices", [])
            for dev in devices:
                name = dev.get("name", "GPU")
                vram_total = dev.get("vram_total", 0)
                vram_free = dev.get("vram_free", 0)
                vram_total_gb = vram_total / (1024**3) if vram_total else 0
                vram_free_gb = vram_free / (1024**3) if vram_free else 0
                lines.append(
                    f"GPU: {name} — {vram_free_gb:.1f} / {vram_total_gb:.1f} GB VRAM free"
                )

            lines.append(f"Queue: {running} running, {pending} pending")
        except Exception as e:
            return ToolResult.fail(f"Cannot connect to ComfyUI at {api_url}: {e}")

        # 2. List checkpoint models
        if list_models:
            try:
                obj_info = await _api_get(
                    f"{api_url}/object_info/CheckpointLoaderSimple",
                    timeout,
                    broker=broker,
                    tool_name=self.name,
                )
                ckpt_info = obj_info.get("CheckpointLoaderSimple", {})
                ckpt_input = ckpt_info.get("input", {}).get("required", {})
                ckpt_list = ckpt_input.get("ckpt_name", [[]])[0]
                if ckpt_list:
                    lines.append(f"\nAvailable Checkpoints ({len(ckpt_list)}):")
                    for name in ckpt_list[:20]:
                        default_marker = (
                            " [DEFAULT]"
                            if cfg.get("default_checkpoint")
                            and name == cfg["default_checkpoint"]
                            else ""
                        )
                        lines.append(f"  - {name}{default_marker}")
                    if len(ckpt_list) > 20:
                        lines.append(f"  ... and {len(ckpt_list) - 20} more")
                else:
                    lines.append(
                        "\nNo checkpoint models found. "
                        "Place .safetensors files in ComfyUI/models/checkpoints/"
                    )
            except Exception as e:
                lines.append(f"\nCould not list models: {e}")

        # 3. List samplers
        try:
            sampler_info = await _api_get(
                f"{api_url}/object_info/KSampler",
                timeout,
                broker=broker,
                tool_name=self.name,
            )
            ks = sampler_info.get("KSampler", {}).get("input", {}).get("required", {})
            samplers = ks.get("sampler_name", [[]])[0]
            schedulers = ks.get("scheduler", [[]])[0]
            if samplers:
                lines.append(f"\nSamplers: {', '.join(samplers[:15])}")
            if schedulers:
                lines.append(f"Schedulers: {', '.join(schedulers)}")
        except Exception:
            pass  # Non-critical

        return ToolResult.ok("\n".join(lines), api_url=api_url)


# ---------------------------------------------------------------------------
# Tool 4: comfyui_workflow  — Run a custom workflow JSON
# ---------------------------------------------------------------------------


class ComfyUIWorkflowTool(BaseTool):
    name = "comfyui_workflow"
    description = (
        "Execute a custom ComfyUI workflow (API format JSON). "
        "Use this for advanced pipelines: img2img, ControlNet, LoRA, upscale, etc. "
        "Pass the full workflow JSON as a string."
    )
    parameters = {
        "type": "object",
        "properties": {
            "workflow_json": {
                "type": "string",
                "description": "The full ComfyUI API-format workflow as a JSON string.",
            },
        },
        "required": ["workflow_json"],
    }
    risk_level = RiskLevel.MEDIUM

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        cfg = _get_comfyui_cfg(context)
        if not cfg.get("enabled"):
            return ToolResult.fail(
                "ComfyUI integration is disabled. Enable it in Settings > ComfyUI."
            )

        api_url = cfg["api_url"].rstrip("/")
        timeout = cfg["timeout"]
        broker = _get_egress_broker(context)

        try:
            workflow = json.loads(params["workflow_json"])
        except json.JSONDecodeError as e:
            return ToolResult.fail(f"Invalid workflow JSON: {e}")

        try:
            history = await _queue_prompt_and_wait(
                api_url,
                workflow,
                timeout,
                broker=broker,
                tool_name=self.name,
            )
        except Exception as e:
            return ToolResult.fail(f"ComfyUI workflow execution failed: {e}")

        images = _extract_image_filenames(history)

        # Download and save images
        saved_paths: list[str] = []
        output_dir = cfg.get("output_dir", "").strip()

        for img_info in images[:4]:  # Limit to 4 images
            img_url = (
                f"{api_url}/view?"
                f"filename={quote(img_info['filename'])}"
                f"&subfolder={quote(img_info['subfolder'])}"
                f"&type={quote(img_info['type'])}"
            )
            try:
                img_bytes = await _download_image(
                    img_url,
                    timeout=30,
                    broker=broker,
                    tool_name=self.name,
                )
                local_path = _save_images_locally(output_dir, img_info, img_bytes)
                saved_paths.append(str(local_path))
            except Exception as e:
                logger.warning(
                    "Failed to download image %s: %s", img_info["filename"], e
                )

        if not saved_paths and not images:
            return ToolResult.ok(
                "Workflow executed successfully (no image output detected — "
                "this workflow may produce other output types)."
            )

        first_image = saved_paths[0] if saved_paths else None
        lines = [
            "Custom workflow executed successfully!",
            f"{len(saved_paths)} image(s) saved:",
        ]
        for p in saved_paths:
            lines.append(f"  {p}")

        return ToolResult.ok(
            "\n".join(lines),
            image_path=first_image,
            saved_paths=saved_paths,
        )
