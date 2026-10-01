"""Helpers shared by every provider adapter.

Streaming line readers that tolerate bad encodings and truncated final
lines, content-block normalisation, and the trimming applied to tool output
before it is sent to the browser.  Nothing in here knows about any one AI
tool's format.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

__all__ = [
    "iter_lines",
    "iter_entries",
    "block_list",
    "text_of_blocks",
    "truncate",
    "clean_tool_result_content",
    "file_signature",
    "dig",
]


def iter_lines(path: Path | str) -> Iterator[Tuple[int, str]]:
    """Yield ``(line_number, text)`` for a transcript, tolerating bad encodings.

    The file is opened with ``errors="replace"`` so a truncated multi-byte
    sequence degrades to U+FFFD instead of raising.
    """
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace", newline="")
    except OSError:
        return
    with handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if stripped:
                yield number, stripped


def iter_entries(path: Path | str) -> Iterator[Tuple[int, Dict[str, Any], str | None]]:
    """Yield ``(line_number, entry, error)`` for every line of a transcript.

    On a malformed line *entry* is ``{}`` and *error* holds a short reason, so
    callers can surface corruption counts instead of silently dropping data.
    A truncated final line - common while a session is still being written -
    produces exactly one error.
    """
    for number, text in iter_lines(path):
        try:
            obj = json.loads(text)
        except ValueError as exc:
            yield number, {}, f"invalid JSON: {exc.args[0] if exc.args else exc}"
            continue
        if not isinstance(obj, dict):
            yield number, {}, f"expected object, got {type(obj).__name__}"
            continue
        yield number, obj, None


def block_list(message: Any) -> List[Dict[str, Any]]:
    """Normalise ``message.content`` to a list of block dicts.

    ``content`` is a plain string on 361 of 4 338 user turns and a list
    elsewhere (``SCHEMA.md`` 3.2).  List elements are not guaranteed to be
    dicts, so non-dict entries are wrapped as text blocks.
    """
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    blocks: List[Dict[str, Any]] = []
    for item in content:
        if isinstance(item, dict):
            blocks.append(item)
        elif isinstance(item, str):
            blocks.append({"type": "text", "text": item})
        else:
            blocks.append({"type": "text", "text": str(item)})
    return blocks


def text_of_blocks(blocks: List[Dict[str, Any]]) -> str:
    """Concatenate the human-readable text of *blocks*, ignoring the rest."""
    parts: List[str] = []
    for block in blocks:
        if block.get("type") == "text":
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def truncate(text: str, limit: int) -> str:
    """Collapse whitespace in *text* and cut it to *limit* characters."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def clean_tool_result_content(content: Any, limit: int) -> Any:
    """Shrink a ``tool_result`` payload for transport to the browser.

    Long stdout is truncated with a marker, and inline image blocks are
    replaced by a small descriptor so a screenshot-heavy session does not
    push megabytes of base64 through the API.
    """
    if isinstance(content, str):
        if len(content) > limit:
            return content[:limit] + f"\n… [truncated, {len(content):,} characters total]"
        return content
    if isinstance(content, list):
        out = []
        for item in content:
            if not isinstance(item, dict):
                out.append({"type": "text", "text": str(item)[:limit]})
                continue
            if item.get("type") == "image":
                source = item.get("source") if isinstance(item.get("source"), dict) else {}
                data = source.get("data")
                out.append(
                    {
                        "type": "image",
                        "media_type": source.get("media_type", "image/png"),
                        "bytes": len(data) if isinstance(data, str) else 0,
                    }
                )
            elif item.get("type") == "text":
                text = item.get("text")
                out.append(
                    {
                        "type": "text",
                        "text": text[:limit] if isinstance(text, str) else str(text)[:limit],
                    }
                )
            else:
                out.append(item)
        return out
    return content


def file_signature(path: Path | str) -> Tuple[str, float, int] | None:
    """Return ``(path, mtime, size)`` used as the cache key, or ``None``."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return str(path), stat.st_mtime, stat.st_size


def dig(source: Any, path: str | None, default: Any = None) -> Any:
    """Follow a dotted path such as ``message.usage.0.tokens`` into *source*.

    Integer segments index lists.  Any missing step yields *default*, so
    callers never have to guard each level of an unfamiliar format.
    """
    if not path:
        return default
    current = source
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return default
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return current
