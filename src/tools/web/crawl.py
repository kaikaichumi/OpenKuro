"""Batch web crawling tool for high-volume data collection.

This tool is designed to reduce LLM token costs for research-heavy tasks:
- Crawl many pages in one tool call (instead of repeated navigate/get_text loops)
- Return compact previews to the LLM
- Persist full crawl results to JSONL for downstream processing
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import aiohttp
import structlog

from src.config import get_kuro_home
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult
from src.tools.web.browse import BrowserManager

logger = structlog.get_logger()

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style|noscript).*?>.*?</\1>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")


def _to_int(value: Any, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    """Best-effort int conversion with optional clamping."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default

    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def _normalize_seed(url: str) -> str | None:
    """Normalize a seed URL and enforce http/https scheme."""
    raw = str(url or "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return None
    return urldefrag(parsed.geturl()).url


def _normalize_link(base_url: str, href: str) -> str | None:
    """Resolve and normalize discovered href values."""
    href = str(href or "").strip()
    if not href:
        return None
    if href.startswith(("javascript:", "mailto:", "tel:", "data:")):
        return None

    resolved = urldefrag(urljoin(base_url, href)).url
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"}:
        return None
    return parsed.geturl()


def _extract_title(html: str) -> str:
    """Extract HTML <title> as plain text."""
    match = _TITLE_RE.search(html or "")
    if not match:
        return ""
    title = html_lib.unescape(match.group(1))
    title = re.sub(r"\s+", " ", title).strip()
    return title[:240]


def _extract_text(html: str, max_chars: int) -> str:
    """Convert HTML to compact plain text."""
    cleaned = _SCRIPT_STYLE_RE.sub(" ", html or "")
    text = _TAG_RE.sub(" ", cleaned)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _mask_proxy(proxy: str | None) -> str | None:
    """Mask proxy URL to avoid exposing credentials."""
    if not proxy:
        return None
    try:
        parsed = urlparse(proxy)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        return f"{scheme}{host}{port}"
    except Exception:
        return "proxy://masked"


class _HrefParser(HTMLParser):
    """Minimal href extractor for <a href=\"...\"> links."""

    def __init__(self, max_links: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_links = max_links
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or len(self.links) >= self.max_links:
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)
                break


def _extract_links(html: str, *, max_links: int) -> list[str]:
    """Extract raw href values from HTML."""
    parser = _HrefParser(max_links=max_links)
    try:
        parser.feed(html or "")
    except Exception:
        return parser.links
    return parser.links


def _matches_filters(url: str, include_patterns: list[str], exclude_patterns: list[str]) -> bool:
    """Apply include/exclude substring filters to URLs."""
    lowered = url.lower()
    includes = [p.lower() for p in include_patterns if str(p).strip()]
    excludes = [p.lower() for p in exclude_patterns if str(p).strip()]

    if includes and not any(token in lowered for token in includes):
        return False
    if excludes and any(token in lowered for token in excludes):
        return False
    return True


def _headless_for_context(context: ToolContext) -> bool:
    """Reuse existing web-tool behavior for background sessions."""
    if getattr(context, "session_id", "") == "scheduler":
        return True
    session = getattr(context, "session", None)
    return bool(session and getattr(session, "adapter", "") == "agent")


@dataclass
class _QueueItem:
    """Crawl frontier item."""

    url: str
    depth: int
    parent_url: str | None


@dataclass
class _FetchedPage:
    """Normalized fetched-page payload."""

    url: str
    final_url: str
    depth: int
    parent_url: str | None
    status: int
    title: str
    text: str
    links: list[str]
    duration_ms: int
    source: str
    proxy: str | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return not self.error and self.status < 400 and bool(self.text)

    def to_record(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "depth": self.depth,
            "parent_url": self.parent_url,
            "status": self.status,
            "title": self.title,
            "text": self.text,
            "links": self.links,
            "duration_ms": self.duration_ms,
            "source": self.source,
            "proxy": self.proxy,
            "error": self.error,
        }


class _RuntimeController:
    """Runtime control for domain rate-limiting and proxy selection."""

    def __init__(
        self,
        *,
        per_domain_delay_ms: int,
        proxy_pool: list[str],
        proxy_mode: str,
    ) -> None:
        self.per_domain_delay_s = max(0, per_domain_delay_ms) / 1000.0
        self.proxy_pool = proxy_pool
        self.proxy_mode = proxy_mode if proxy_mode in {"rotate", "sticky"} else "rotate"
        self.sticky_proxy = proxy_pool[0] if proxy_pool and self.proxy_mode == "sticky" else None
        self.proxy_index = 0
        self.proxy_lock = asyncio.Lock()
        self.domain_locks: dict[str, asyncio.Lock] = {}
        self.domain_next_allowed: dict[str, float] = {}

    async def wait_domain_slot(self, url: str) -> None:
        """Enforce delay between requests to the same domain."""
        if self.per_domain_delay_s <= 0:
            return

        domain = urlparse(url).netloc.lower()
        if not domain:
            return

        lock = self.domain_locks.get(domain)
        if lock is None:
            lock = asyncio.Lock()
            self.domain_locks[domain] = lock

        async with lock:
            now = time.monotonic()
            wait_s = self.domain_next_allowed.get(domain, 0.0) - now
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            self.domain_next_allowed[domain] = time.monotonic() + self.per_domain_delay_s

    async def pick_proxy(self) -> str | None:
        """Select proxy in rotate/sticky mode."""
        if not self.proxy_pool:
            return None
        if self.sticky_proxy:
            return self.sticky_proxy

        async with self.proxy_lock:
            proxy = self.proxy_pool[self.proxy_index % len(self.proxy_pool)]
            self.proxy_index += 1
            return proxy


class WebCrawlBatchTool(BaseTool):
    """Crawl multiple pages asynchronously and persist structured results."""

    name = "web_crawl_batch"
    description = (
        "Batch-crawl many web pages efficiently in one tool call. "
        "Supports URL deduplication, depth-limited discovery, same-domain mode, "
        "optional dynamic fallback with browser rendering, per-domain rate limiting, "
        "checkpoint/resume, and proxy pools. "
        "Returns compact previews and can save full results to JSONL."
    )
    parameters = {
        "type": "object",
        "properties": {
            "seeds": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Seed URLs to start crawling from.",
            },
            "max_pages": {
                "type": "integer",
                "description": "Maximum pages to fetch (default: 20, max: 300).",
            },
            "max_depth": {
                "type": "integer",
                "description": "Link-follow depth from seeds (default: 1, max: 5).",
            },
            "concurrency": {
                "type": "integer",
                "description": "Concurrent fetches per batch (default: 6, max: 20).",
            },
            "same_domain_only": {
                "type": "boolean",
                "description": "Only crawl seed domains (default: true).",
            },
            "include_patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional URL include filters (substring match).",
            },
            "exclude_patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional URL exclude filters (substring match).",
            },
            "request_timeout": {
                "type": "integer",
                "description": "HTTP timeout seconds (default: 15).",
            },
            "max_links_per_page": {
                "type": "integer",
                "description": "Maximum discovered links kept per page (default: 30, max: 200).",
            },
            "max_text_chars": {
                "type": "integer",
                "description": "Maximum extracted text chars per page (default: 4000, max: 12000).",
            },
            "dynamic_fallback": {
                "type": "boolean",
                "description": "Use Playwright fallback for blocked/empty pages (default: false).",
            },
            "per_domain_delay_ms": {
                "type": "integer",
                "description": "Minimum delay between requests to same domain in ms (default: 0).",
            },
            "proxy_pool": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional proxy URLs, e.g. http://user:pass@host:port.",
            },
            "proxy_mode": {
                "type": "string",
                "description": "Proxy mode: 'rotate' (default) or 'sticky'.",
            },
            "save_checkpoint": {
                "type": "boolean",
                "description": "Save crawl state checkpoint during run (default: true).",
            },
            "resume": {
                "type": "boolean",
                "description": "Resume from existing checkpoint_path (default: false).",
            },
            "checkpoint_path": {
                "type": "string",
                "description": "Checkpoint file path. Defaults to ~/.kuro/crawls/checkpoints/*.json.",
            },
            "checkpoint_every": {
                "type": "integer",
                "description": "Save checkpoint every N fetched pages (default: 10).",
            },
            "clear_checkpoint_on_success": {
                "type": "boolean",
                "description": "Delete checkpoint file when crawl finishes (default: false).",
            },
            "save_to_file": {
                "type": "boolean",
                "description": "Save full crawl records to JSONL file (default: true).",
            },
            "output_path": {
                "type": "string",
                "description": "Optional output file path. Defaults to ~/.kuro/crawls/*.jsonl.",
            },
            "user_agent": {
                "type": "string",
                "description": "Optional custom User-Agent header for HTTP fetches.",
            },
        },
        "required": ["seeds"],
    }
    risk_level = RiskLevel.MEDIUM

    async def _fetch_http(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        proxy: str | None = None,
    ) -> tuple[int, str, str, str, str | None]:
        """Fetch a page over HTTP and return status, final URL, content-type, html, error."""
        try:
            async with session.get(url, allow_redirects=True, proxy=proxy or None) as resp:
                status = int(resp.status)
                final_url = str(resp.url)
                content_type = (resp.headers.get("content-type") or "").lower()
                if "text/html" not in content_type:
                    return status, final_url, content_type, "", f"non-html content-type: {content_type}"
                html = await resp.text(errors="ignore")
                return status, final_url, content_type, html, None
        except Exception as e:
            return 0, url, "", "", str(e)

    async def _fetch_dynamic(self, url: str, context: ToolContext) -> tuple[str, str, str | None]:
        """Fetch rendered HTML with Playwright."""
        try:
            manager = BrowserManager.get_instance()
            page = await manager.ensure_page(headless=_headless_for_context(context))
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            html = await page.content()
            title = await page.title()
            return str(page.url), html, title
        except Exception as e:
            return url, "", f"dynamic_fallback_failed: {e}"

    async def _fetch_one(
        self,
        item: _QueueItem,
        *,
        session: aiohttp.ClientSession,
        context: ToolContext,
        max_text_chars: int,
        max_links_per_page: int,
        dynamic_fallback: bool,
        runtime: _RuntimeController,
    ) -> _FetchedPage:
        """Fetch one page and extract title/text/links."""
        started = time.perf_counter()
        await runtime.wait_domain_slot(item.url)
        proxy = await runtime.pick_proxy()
        masked_proxy = _mask_proxy(proxy)
        status, final_url, content_type, html, error = await self._fetch_http(
            session,
            item.url,
            proxy=proxy,
        )
        source = "http"
        title = ""

        text = _extract_text(html, max_chars=max_text_chars) if html else ""
        should_fallback = bool(dynamic_fallback and (error or status >= 400 or len(text) < 120))

        if should_fallback:
            fb_url, fb_html, fb_meta = await self._fetch_dynamic(final_url or item.url, context)
            if fb_html:
                final_url = fb_url
                html = fb_html
                text = _extract_text(html, max_chars=max_text_chars)
                source = "dynamic"
                error = None
                status = status or 200
                title = fb_meta if fb_meta and not str(fb_meta).startswith("dynamic_fallback_failed") else ""
            else:
                if isinstance(fb_meta, str):
                    error = f"{error or 'http_fetch_failed'}; {fb_meta}"

        if not title and html:
            title = _extract_title(html)

        raw_links = _extract_links(html, max_links=max_links_per_page) if html else []
        links: list[str] = []
        for href in raw_links:
            normalized = _normalize_link(final_url or item.url, href)
            if normalized:
                links.append(normalized)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if not error and status >= 400:
            error = f"http_status_{status}"
        if not error and "text/html" not in content_type and source == "http":
            error = f"unsupported_content_type: {content_type}"

        return _FetchedPage(
            url=item.url,
            final_url=final_url or item.url,
            depth=item.depth,
            parent_url=item.parent_url,
            status=status,
            title=title,
            text=text,
            links=links,
            duration_ms=elapsed_ms,
            source=source,
            proxy=masked_proxy,
            error=error,
        )

    def _resolve_output_path(self, output_path: str | None) -> Path:
        """Resolve output path for JSONL results."""
        if output_path:
            candidate = Path(output_path).expanduser()
            if candidate.suffix.lower() != ".jsonl":
                candidate = candidate / f"crawl_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
            return candidate

        crawl_dir = get_kuro_home() / "crawls"
        return crawl_dir / f"crawl_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"

    def _resolve_checkpoint_path(self, checkpoint_path: str | None, start_urls: list[str]) -> Path:
        """Resolve checkpoint path for resume-able crawl state."""
        if checkpoint_path:
            candidate = Path(checkpoint_path).expanduser()
            if candidate.suffix.lower() != ".json":
                candidate = candidate / "crawl_checkpoint.json"
            return candidate

        host = "default"
        if start_urls:
            host = urlparse(start_urls[0]).netloc.lower().replace(":", "_").replace(".", "_") or "default"
        cp_dir = get_kuro_home() / "crawls" / "checkpoints"
        return cp_dir / f"crawl_checkpoint_{host}.json"

    def _load_checkpoint(self, checkpoint_path: Path) -> tuple[list[str], deque[_QueueItem], set[str], list[_FetchedPage]]:
        """Load checkpoint state from JSON file."""
        raw = json.loads(checkpoint_path.read_text(encoding="utf-8"))

        start_urls = []
        for u in raw.get("start_urls", []):
            normalized = _normalize_seed(str(u))
            if normalized:
                start_urls.append(normalized)

        frontier: deque[_QueueItem] = deque()
        for item in raw.get("frontier", []):
            if not isinstance(item, dict):
                continue
            normalized = _normalize_seed(str(item.get("url", "")))
            if not normalized:
                continue
            frontier.append(
                _QueueItem(
                    url=normalized,
                    depth=_to_int(item.get("depth"), 0, min_value=0, max_value=20),
                    parent_url=str(item.get("parent_url")) if item.get("parent_url") else None,
                )
            )

        seen_urls: set[str] = set()
        for u in raw.get("seen_urls", []):
            normalized = _normalize_seed(str(u))
            if normalized:
                seen_urls.add(normalized)

        pages: list[_FetchedPage] = []
        for row in raw.get("pages", []):
            if not isinstance(row, dict):
                continue
            url = _normalize_seed(str(row.get("url", "")))
            final_url = _normalize_seed(str(row.get("final_url", ""))) or url
            if not url or not final_url:
                continue

            links = []
            for link in row.get("links", []):
                normalized = _normalize_seed(str(link))
                if normalized:
                    links.append(normalized)

            pages.append(
                _FetchedPage(
                    url=url,
                    final_url=final_url,
                    depth=_to_int(row.get("depth"), 0, min_value=0, max_value=20),
                    parent_url=str(row.get("parent_url")) if row.get("parent_url") else None,
                    status=_to_int(row.get("status"), 0, min_value=0, max_value=999),
                    title=str(row.get("title") or ""),
                    text=str(row.get("text") or ""),
                    links=links,
                    duration_ms=_to_int(row.get("duration_ms"), 0, min_value=0),
                    source=str(row.get("source") or "http"),
                    proxy=str(row.get("proxy")) if row.get("proxy") else None,
                    error=str(row.get("error")) if row.get("error") else None,
                )
            )

        if not start_urls:
            if frontier:
                start_urls = [frontier[0].url]
            elif pages:
                start_urls = [pages[0].url]

        return start_urls, frontier, seen_urls, pages

    def _save_checkpoint(
        self,
        checkpoint_path: Path,
        *,
        start_urls: list[str],
        frontier: deque[_QueueItem],
        seen_urls: set[str],
        pages: list[_FetchedPage],
    ) -> None:
        """Persist checkpoint state as JSON for resume."""
        payload = {
            "version": 1,
            "updated_at": int(time.time()),
            "start_urls": start_urls,
            "frontier": [
                {
                    "url": item.url,
                    "depth": item.depth,
                    "parent_url": item.parent_url,
                }
                for item in frontier
            ],
            "seen_urls": sorted(seen_urls),
            "pages": [page.to_record() for page in pages],
        }

        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(checkpoint_path)

    async def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        seeds = params.get("seeds", [])
        if isinstance(seeds, str):
            seeds = [seeds]
        if not isinstance(seeds, list) or not seeds:
            return ToolResult.fail("'seeds' must be a non-empty list of URLs")

        normalized_seeds = [_normalize_seed(s) for s in seeds]
        start_urls = [u for u in normalized_seeds if u]
        if not start_urls:
            return ToolResult.fail("No valid http/https seed URLs provided")

        max_pages = _to_int(params.get("max_pages"), 20, min_value=1, max_value=300)
        max_depth = _to_int(params.get("max_depth"), 1, min_value=0, max_value=5)
        concurrency = _to_int(params.get("concurrency"), 6, min_value=1, max_value=20)
        same_domain_only = bool(params.get("same_domain_only", True))
        request_timeout = _to_int(params.get("request_timeout"), 15, min_value=5, max_value=120)
        max_links_per_page = _to_int(params.get("max_links_per_page"), 30, min_value=1, max_value=200)
        max_text_chars = _to_int(params.get("max_text_chars"), 4000, min_value=200, max_value=12000)
        dynamic_fallback = bool(params.get("dynamic_fallback", False))
        per_domain_delay_ms = _to_int(params.get("per_domain_delay_ms"), 0, min_value=0, max_value=60000)

        proxy_pool_raw = params.get("proxy_pool") or []
        if isinstance(proxy_pool_raw, list):
            proxy_pool = [str(p).strip() for p in proxy_pool_raw if str(p).strip()]
        else:
            proxy_pool = []
        proxy_mode = str(params.get("proxy_mode", "rotate")).strip().lower()
        if proxy_mode not in {"rotate", "sticky"}:
            proxy_mode = "rotate"

        save_checkpoint = bool(params.get("save_checkpoint", True))
        resume = bool(params.get("resume", False))
        checkpoint_every = _to_int(params.get("checkpoint_every"), 10, min_value=1, max_value=200)
        clear_checkpoint_on_success = bool(params.get("clear_checkpoint_on_success", False))
        checkpoint_path_raw = params.get("checkpoint_path")

        save_to_file = bool(params.get("save_to_file", True))
        output_path_raw = params.get("output_path")
        user_agent = str(params.get("user_agent") or _DEFAULT_USER_AGENT).strip() or _DEFAULT_USER_AGENT

        include_patterns = params.get("include_patterns") or []
        exclude_patterns = params.get("exclude_patterns") or []
        if not isinstance(include_patterns, list):
            include_patterns = []
        if not isinstance(exclude_patterns, list):
            exclude_patterns = []

        checkpoint_path = self._resolve_checkpoint_path(
            str(checkpoint_path_raw) if checkpoint_path_raw else None,
            start_urls,
        )

        resumed_from_checkpoint = False
        if resume:
            if not checkpoint_path.exists():
                return ToolResult.fail(f"Checkpoint not found: {checkpoint_path}")
            try:
                loaded_start_urls, frontier, seen_urls, pages = self._load_checkpoint(checkpoint_path)
                if loaded_start_urls:
                    start_urls = loaded_start_urls
                resumed_from_checkpoint = True
            except Exception as e:
                return ToolResult.fail(f"Failed to load checkpoint: {e}")
        else:
            frontier = deque(_QueueItem(url=u, depth=0, parent_url=None) for u in start_urls)
            seen_urls = set(start_urls)
            pages = []

        if not start_urls:
            return ToolResult.fail("No valid start URLs after checkpoint restore")

        allowed_domains = {urlparse(u).netloc.lower() for u in start_urls}
        runtime = _RuntimeController(
            per_domain_delay_ms=per_domain_delay_ms,
            proxy_pool=proxy_pool,
            proxy_mode=proxy_mode,
        )

        timeout = aiohttp.ClientTimeout(total=request_timeout)
        connector = aiohttp.TCPConnector(limit=max(concurrency * 2, 10), ttl_dns_cache=300)

        started = time.perf_counter()
        last_checkpoint_pages = len(pages)

        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={"User-Agent": user_agent},
        ) as session:
            while frontier and len(pages) < max_pages:
                batch: list[_QueueItem] = []
                batch_limit = min(concurrency, max_pages - len(pages))
                while frontier and len(batch) < batch_limit:
                    batch.append(frontier.popleft())

                fetched_batch = await asyncio.gather(
                    *[
                        self._fetch_one(
                            item,
                            session=session,
                            context=context,
                            max_text_chars=max_text_chars,
                            max_links_per_page=max_links_per_page,
                            dynamic_fallback=dynamic_fallback,
                            runtime=runtime,
                        )
                        for item in batch
                    ]
                )

                for page in fetched_batch:
                    pages.append(page)

                    if page.depth >= max_depth:
                        continue

                    for candidate_url in page.links:
                        parsed = urlparse(candidate_url)
                        domain = parsed.netloc.lower()

                        if same_domain_only and domain not in allowed_domains:
                            continue
                        if not _matches_filters(candidate_url, include_patterns, exclude_patterns):
                            continue
                        if candidate_url in seen_urls:
                            continue

                        seen_urls.add(candidate_url)
                        frontier.append(
                            _QueueItem(
                                url=candidate_url,
                                depth=page.depth + 1,
                                parent_url=page.final_url or page.url,
                            )
                        )

                if save_checkpoint and (len(pages) - last_checkpoint_pages >= checkpoint_every):
                    self._save_checkpoint(
                        checkpoint_path,
                        start_urls=start_urls,
                        frontier=frontier,
                        seen_urls=seen_urls,
                        pages=pages,
                    )
                    last_checkpoint_pages = len(pages)

        if save_checkpoint:
            self._save_checkpoint(
                checkpoint_path,
                start_urls=start_urls,
                frontier=frontier,
                seen_urls=seen_urls,
                pages=pages,
            )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        success_pages = [p for p in pages if p.success]
        failed_pages = [p for p in pages if not p.success]

        dataset_path: Path | None = None
        if save_to_file:
            dataset_path = self._resolve_output_path(str(output_path_raw) if output_path_raw else None)
            dataset_path.parent.mkdir(parents=True, exist_ok=True)
            with dataset_path.open("w", encoding="utf-8") as f:
                for page in pages:
                    f.write(json.dumps(page.to_record(), ensure_ascii=False) + "\n")

        if clear_checkpoint_on_success and not failed_pages and checkpoint_path.exists():
            try:
                checkpoint_path.unlink()
            except Exception:
                pass

        logger.info(
            "web_crawl_batch_completed",
            seeds=len(start_urls),
            crawled=len(pages),
            success=len(success_pages),
            failed=len(failed_pages),
            elapsed_ms=elapsed_ms,
            dynamic_fallback=dynamic_fallback,
            per_domain_delay_ms=per_domain_delay_ms,
            resumed=resumed_from_checkpoint,
            proxy_pool_size=len(proxy_pool),
            proxy_mode=proxy_mode,
        )

        top_pages = []
        for page in success_pages[:10]:
            top_pages.append(
                {
                    "url": page.final_url or page.url,
                    "title": page.title,
                    "status": page.status,
                    "depth": page.depth,
                    "text_preview": page.text[:500],
                    "source": page.source,
                    "proxy": page.proxy,
                }
            )

        error_preview = []
        for page in failed_pages[:10]:
            error_preview.append(
                {
                    "url": page.url,
                    "status": page.status,
                    "error": page.error or "unknown",
                    "depth": page.depth,
                    "source": page.source,
                    "proxy": page.proxy,
                }
            )

        summary_lines = [
            f"Crawl completed in {elapsed_ms} ms",
            f"Seeds: {len(start_urls)} | Crawled: {len(pages)} | Success: {len(success_pages)} | Failed: {len(failed_pages)}",
            f"Discovered URLs: {len(seen_urls)} | Max depth: {max_depth} | Same-domain only: {same_domain_only}",
            f"Rate limit: {per_domain_delay_ms} ms/domain | Proxy mode: {proxy_mode} ({len(proxy_pool)} proxies)",
            f"Checkpoint: {checkpoint_path} | Resumed: {resumed_from_checkpoint}",
        ]
        if dataset_path is not None:
            summary_lines.append(f"Saved JSONL: {dataset_path}")
        if top_pages:
            summary_lines.append("Top pages:")
            for p in top_pages[:5]:
                summary_lines.append(f"- [{p['status']}] {p['url']} | {p['title'][:80]}")

        return ToolResult.ok(
            "\n".join(summary_lines),
            pages_crawled=len(pages),
            success_count=len(success_pages),
            failure_count=len(failed_pages),
            discovered_count=len(seen_urls),
            elapsed_ms=elapsed_ms,
            dataset_path=str(dataset_path) if dataset_path else None,
            checkpoint_path=str(checkpoint_path),
            resumed=resumed_from_checkpoint,
            proxy_mode=proxy_mode,
            proxy_pool_size=len(proxy_pool),
            per_domain_delay_ms=per_domain_delay_ms,
            pages=top_pages,
            errors=error_preview,
        )
