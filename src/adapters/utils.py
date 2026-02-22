"""Shared utilities for messaging adapters."""

from __future__ import annotations


def split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split a long message into chunks within the character limit.

    Splitting priority:
    1. By code block boundaries (``` markers) — keeps code intact
    2. By paragraph (double newline)
    3. By line (single newline)
    4. By word (space)
    5. By character (hard split)

    Args:
        text: The message text to split.
        max_len: Maximum characters per chunk.

    Returns:
        List of message chunks, each within max_len characters.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []

    # Try splitting by code blocks first
    if "```" in text:
        parts = text.split("```")
        current = ""
        for i, part in enumerate(parts):
            block = part if i % 2 == 0 else f"```{part}```"
            if len(current) + len(block) <= max_len:
                current += block
            else:
                if current:
                    chunks.append(current.strip())
                if len(block) > max_len:
                    # Large code block: split by lines
                    chunks.extend(_split_by_lines(block, max_len))
                    current = ""
                else:
                    current = block
        if current:
            chunks.append(current.strip())
        return [c for c in chunks if c]

    return _split_by_lines(text, max_len)


def _split_by_lines(text: str, max_len: int) -> list[str]:
    """Split text by paragraphs → lines → words → characters."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []

    # Try paragraphs
    paragraphs = text.split("\n\n")
    current = ""
    for para in paragraphs:
        candidate = (current + "\n\n" + para).lstrip("\n") if current else para
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(para) > max_len:
                chunks.extend(_split_by_single_lines(para, max_len))
                current = ""
            else:
                current = para
    if current:
        chunks.append(current)
    return chunks


def _split_by_single_lines(text: str, max_len: int) -> list[str]:
    """Split text by single newlines → words → characters."""
    chunks: list[str] = []
    lines = text.split("\n")
    current = ""
    for line in lines:
        candidate = (current + "\n" + line) if current else line
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(line) > max_len:
                # Split by words
                words = line.split(" ")
                current = ""
                for word in words:
                    candidate = (current + " " + word) if current else word
                    if len(candidate) <= max_len:
                        current = candidate
                    else:
                        if current:
                            chunks.append(current)
                        if len(word) > max_len:
                            # Hard split
                            for i in range(0, len(word), max_len):
                                chunks.append(word[i:i + max_len])
                            current = ""
                        else:
                            current = word
                if current:
                    chunks.append(current)
                    current = ""
            else:
                current = line
    if current:
        chunks.append(current)
    return chunks
