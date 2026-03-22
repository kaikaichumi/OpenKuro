"""Data Firewall (Phase 5): sanitize untrusted tool output before model context."""

from __future__ import annotations

import fnmatch
import re
from typing import Any


_PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?previous", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?previous", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"developer\s*:\s*", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
    re.compile(r"<<\s*sys\s*>>", re.IGNORECASE),
]

_COMMAND_LIKE_LINE_RE = re.compile(
    r"^\s*(?:"
    r"sudo\b|"
    r"rm\s+-rf\b|"
    r"curl\b|"
    r"wget\b|"
    r"powershell\b|"
    r"invoke-webrequest\b|"
    r"bash\s+-c\b|"
    r"sh\s+-c\b|"
    r"python\s+-c\b|"
    r"pip(?:3)?\s+install\b|"
    r"apt(?:-get)?\s+install\b|"
    r"choco\s+install\b|"
    r"npm\s+(?:install|i)\b|"
    r"pnpm\s+add\b|"
    r"yarn\s+add\b"
    r")",
    re.IGNORECASE,
)


class DataFirewall:
    """Content sanitizer for untrusted tool outputs (web/MCP)."""

    def __init__(self, config: Any) -> None:
        self.config = config

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", False))

    def should_filter_tool(self, tool_name: str) -> bool:
        if not self.enabled:
            return False
        name = str(tool_name or "").strip().lower()
        if not name:
            return False
        patterns = getattr(self.config, "tool_name_patterns", []) or []
        for raw in patterns:
            pat = str(raw or "").strip().lower()
            if not pat:
                continue
            if fnmatch.fnmatch(name, pat):
                return True
        return False

    def sanitize_output(
        self,
        text: str,
        *,
        tool_name: str = "",
    ) -> tuple[str, dict[str, Any]]:
        raw = str(text or "")
        report: dict[str, Any] = {
            "tool_name": str(tool_name or ""),
            "changed": False,
            "prompt_injection_lines": 0,
            "command_like_lines_removed": 0,
            "base64_chunks_removed": 0,
            "truncated": False,
            "input_chars": len(raw),
            "output_chars": len(raw),
        }
        if not raw:
            return raw, report

        max_base64 = max(256, int(getattr(self.config, "max_base64_chunk_chars", 2048) or 2048))
        base64_re = re.compile(
            rf"(?:data:[^;,\s]+;base64,)?[A-Za-z0-9+/]{{{max_base64},}}={{0,2}}"
        )

        neutralize_injection = bool(getattr(self.config, "neutralize_prompt_injection", True))
        remove_command_like = bool(getattr(self.config, "remove_command_like_lines", True))
        annotation_prefix = str(getattr(self.config, "annotation_prefix", "[Data Firewall]") or "[Data Firewall]").strip()
        if not annotation_prefix:
            annotation_prefix = "[Data Firewall]"

        out_lines: list[str] = []
        for line in raw.splitlines():
            line_to_keep = line
            if neutralize_injection:
                if any(p.search(line_to_keep) for p in _PROMPT_INJECTION_PATTERNS):
                    report["prompt_injection_lines"] = int(report["prompt_injection_lines"]) + 1
                    report["changed"] = True
                    continue

            if remove_command_like and _COMMAND_LIKE_LINE_RE.search(line_to_keep):
                report["command_like_lines_removed"] = int(report["command_like_lines_removed"]) + 1
                report["changed"] = True
                continue

            line_to_keep, count = base64_re.subn("[DATA_FIREWALL_BASE64_REMOVED]", line_to_keep)
            if count:
                report["base64_chunks_removed"] = int(report["base64_chunks_removed"]) + int(count)
                report["changed"] = True
            out_lines.append(line_to_keep)

        output = "\n".join(out_lines)
        max_chars = max(1000, int(getattr(self.config, "max_context_chars", 16000) or 16000))
        if len(output) > max_chars:
            output = output[:max_chars].rstrip() + "\n[DATA_FIREWALL_TRUNCATED]"
            report["truncated"] = True
            report["changed"] = True

        if report["changed"]:
            notes = [
                f"injection={int(report['prompt_injection_lines'])}",
                f"command={int(report['command_like_lines_removed'])}",
                f"base64={int(report['base64_chunks_removed'])}",
                f"truncated={'yes' if bool(report['truncated']) else 'no'}",
            ]
            output = f"{annotation_prefix} sanitized untrusted content ({', '.join(notes)})\n{output}".strip()

        report["output_chars"] = len(output)
        return output, report
