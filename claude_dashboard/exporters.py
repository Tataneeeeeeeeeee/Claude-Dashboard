"""Render a parsed conversation to Markdown, self-contained HTML or JSON.

All three take the dictionary produced by
:func:`claude_dashboard.parser.parse_conversation` so the exported file shows
exactly what the viewer shows, including the same tool-output truncation.

The HTML export embeds its own stylesheet and references nothing external, so
the saved file opens correctly on a machine with no network.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any, Dict, List

from .parser import parse_timestamp

__all__ = ["to_markdown", "to_html", "to_json", "EXPORT_FORMATS"]

#: Supported export formats mapped to their file extension and MIME type.
EXPORT_FORMATS: Dict[str, Dict[str, str]] = {
    "markdown": {"extension": "md", "mime": "text/markdown"},
    "html": {"extension": "html", "mime": "text/html"},
    "json": {"extension": "json", "mime": "application/json"},
}

_ROLE_LABEL = {
    "user": "You",
    "assistant": "Claude",
    "tool_result": "Tool result",
    "system": "System",
    "attachment": "Attachment",
}


def _stamp(value: Any) -> str:
    """Format a transcript timestamp for a document heading."""
    parsed = parse_timestamp(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC") if parsed else ""


def _tool_input_text(payload: Any, limit: int = 4000) -> str:
    """Pretty-print a tool's input for inclusion in a document."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        text = payload
    else:
        try:
            text = json.dumps(payload, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            text = repr(payload)
    if len(text) > limit:
        text = text[:limit] + f"\n… [truncated, {len(text):,} characters]"
    return text


def _result_text(content: Any) -> str:
    """Flatten a tool_result payload to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
            elif item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif item.get("type") == "image":
                parts.append(f"[image, {item.get('media_type', 'image')}, "
                             f"{item.get('bytes', 0):,} bytes]")
            else:
                parts.append(json.dumps(item, ensure_ascii=False)[:2000])
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)[:4000]


def _summary_lines(parsed: Dict[str, Any], meta: Dict[str, Any] | None) -> List[str]:
    """Front-matter facts shared by the Markdown and HTML exports."""
    info = parsed.get("info", {})
    lines = [
        ("Session", info.get("session_id", "")),
        ("Project", info.get("cwd", "")),
        ("Git branch", info.get("git_branch", "")),
        ("Claude Code", ", ".join(info.get("versions", []))),
        ("Entries", str(len(parsed.get("messages", [])))),
    ]
    if meta:
        lines.extend([
            ("First message", _stamp(meta.get("first_timestamp"))),
            ("Last message", _stamp(meta.get("last_timestamp"))),
            ("Messages", str(meta.get("message_count", ""))),
            ("Tool calls", str(meta.get("tool_calls", ""))),
            ("Models", ", ".join(sorted(meta.get("models", {})))),
            ("Total tokens", f"{meta.get('total_tokens', 0):,}"),
            ("Estimated cost", f"US${meta.get('estimated_cost', 0):.4f} (local estimate)"),
        ])
        if meta.get("reported_cost_usd") is not None:
            lines.append(("Cost recorded by Claude Code",
                          f"US${meta['reported_cost_usd']:.4f}"))
    if parsed.get("errors"):
        lines.append(("Unparsable lines", str(len(parsed["errors"]))))
    return [f"- **{label}:** {value}" for label, value in lines if value not in ("", None)]


def to_markdown(
    parsed: Dict[str, Any],
    include_thinking: bool = True,
    include_tools: bool = True,
    include_attachments: bool = False,
) -> str:
    """Render a conversation as Markdown.

    Thinking blocks and tool calls are folded into ``<details>`` elements,
    which both GitHub and most Markdown viewers render as collapsed.
    """
    info = parsed.get("info", {})
    meta = parsed.get("meta")
    out: List[str] = [f"# {info.get('title') or info.get('session_id', 'Conversation')}", ""]
    out.extend(_summary_lines(parsed, meta))
    out.append("")
    out.append("---")
    out.append("")

    for message in parsed.get("messages", []):
        kind = message.get("kind")
        if kind == "attachment" and not include_attachments:
            continue
        if kind == "tool_result" and not include_tools:
            continue

        label = _ROLE_LABEL.get(kind, kind)
        stamp = _stamp(message.get("timestamp"))
        suffix = []
        if message.get("model"):
            suffix.append(message["model"])
        if message.get("is_sidechain"):
            suffix.append("sub-agent")
        if message.get("is_error"):
            suffix.append("error")
        heading = f"## {label}"
        detail = " · ".join([s for s in [stamp, *suffix] if s])
        out.append(heading)
        if detail:
            out.append(f"*{detail}*")
        out.append("")

        if kind == "attachment":
            out.append(f"> Injected context: `{message.get('subtype', 'unknown')}`")
            out.append("")
            continue

        if kind == "system":
            text = "\n".join(b.get("text", "") for b in message.get("blocks", [])
                             if b.get("type") == "text")
            subtype = message.get("subtype") or "system"
            out.append(f"> `{subtype}` {text}".strip())
            out.append("")
            continue

        for block in message.get("blocks", []):
            btype = block.get("type")
            if btype == "text":
                out.append(block.get("text", ""))
                out.append("")
            elif btype == "thinking":
                # Most thinking blocks hold only a signature (SCHEMA.md
                # 3.4.1); an empty <details> would be noise.
                if not include_thinking or not str(block.get("text", "")).strip():
                    continue
                out.append("<details>")
                out.append("<summary>Thinking</summary>")
                out.append("")
                out.append(block.get("text", ""))
                out.append("")
                out.append("</details>")
                out.append("")
            elif btype == "tool_use":
                if not include_tools:
                    continue
                out.append("<details>")
                out.append(f"<summary>Tool call: {block.get('name', 'unknown')}</summary>")
                out.append("")
                out.append("```json")
                out.append(_tool_input_text(block.get("input")))
                out.append("```")
                out.append("")
                out.append("</details>")
                out.append("")
            elif btype == "tool_result":
                if not include_tools:
                    continue
                name = block.get("name") or "tool"
                flag = " (error)" if block.get("is_error") else ""
                out.append("<details>")
                out.append(f"<summary>Result from {name}{flag}</summary>")
                out.append("")
                out.append("```")
                out.append(_result_text(block.get("content")))
                out.append("```")
                out.append("")
                out.append("</details>")
                out.append("")
            elif btype == "image":
                out.append(f"*[image, {block.get('media_type', 'image')}, "
                           f"{block.get('bytes', 0):,} bytes]*")
                out.append("")
            else:
                out.append(f"*[unsupported block: {btype}]*")
                out.append("")

    out.append("---")
    out.append("")
    out.append(
        "*Exported by Claude Code Dashboard on "
        f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}. "
        "Costs shown are local estimates, not billing data.*"
    )
    return "\n".join(out).rstrip() + "\n"


_HTML_STYLE = """
:root {
  --bg: #ffffff; --fg: #1e1f22; --dim: #63656c; --faint: #8d8f97;
  --line: #e2e0da; --raised: #f7f7f5; --accent: #c2643c; --user: #3b6ea5;
  --error: #b3453a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181d; --fg: #e8e6e3; --dim: #9aa0ad; --faint: #6b7280;
    --line: #2a2e38; --raised: #1c1f26; --accent: #d97757; --user: #6ea8dc;
    --error: #e06c60;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0 auto; padding: 32px 20px 80px; max-width: 900px;
  background: var(--bg); color: var(--fg); line-height: 1.62;
  font: 15px/1.62 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
h1 { font-size: 23px; margin: 0 0 6px; line-height: 1.3; }
.meta { color: var(--faint); font-size: 13px; margin-bottom: 26px; }
.meta div { margin: 2px 0; }
.msg { border-top: 1px solid var(--line); padding: 18px 0; }
.msg:first-of-type { border-top: none; }
.who {
  font-size: 11px; font-weight: 700; letter-spacing: .07em;
  text-transform: uppercase; margin-bottom: 8px;
}
.msg.user .who { color: var(--user); }
.msg.assistant .who { color: var(--accent); }
.msg.system .who, .msg.attachment .who { color: var(--faint); }
.msg.error { border-left: 3px solid var(--error); padding-left: 14px; }
.msg.sidechain { border-left: 3px solid var(--faint); padding-left: 14px; }
.when { font-weight: 400; color: var(--faint); text-transform: none; letter-spacing: 0; }
.text { white-space: pre-wrap; overflow-wrap: anywhere; }
details {
  margin: 10px 0; border: 1px solid var(--line); border-radius: 7px;
  background: var(--raised); overflow: hidden;
}
summary {
  cursor: pointer; padding: 7px 11px; font-size: 12.5px; color: var(--dim);
  user-select: none;
}
summary:hover { color: var(--fg); }
details > pre { margin: 0; border-top: 1px solid var(--line); }
pre {
  margin: 10px 0; padding: 11px 13px; overflow-x: auto;
  background: var(--raised); border-radius: 7px;
  font: 12.5px/1.55 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: .92em; }
footer { margin-top: 40px; color: var(--faint); font-size: 12px; }
"""


def to_html(
    parsed: Dict[str, Any],
    include_thinking: bool = True,
    include_tools: bool = True,
    include_attachments: bool = False,
) -> str:
    """Render a conversation as a single self-contained HTML file."""
    info = parsed.get("info", {})
    meta = parsed.get("meta")
    title = info.get("title") or info.get("session_id", "Conversation")
    esc = html.escape

    parts: List[str] = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(title)}</title>",
        f"<style>{_HTML_STYLE}</style>",
        "</head><body>",
        f"<h1>{esc(title)}</h1>",
        '<div class="meta">',
    ]
    for line in _summary_lines(parsed, meta):
        clean = line.lstrip("- ").replace("**", "")
        parts.append(f"<div>{esc(clean)}</div>")
    parts.append("</div>")

    for message in parsed.get("messages", []):
        kind = message.get("kind")
        if kind == "attachment" and not include_attachments:
            continue
        if kind == "tool_result" and not include_tools:
            continue

        classes = ["msg", kind or "unknown"]
        if message.get("is_error"):
            classes.append("error")
        if message.get("is_sidechain"):
            classes.append("sidechain")
        stamp = _stamp(message.get("timestamp"))
        extra = " · ".join(
            filter(None, [stamp, message.get("model"),
                          "sub-agent" if message.get("is_sidechain") else None])
        )
        parts.append(f'<div class="{" ".join(classes)}">')
        parts.append(
            f'<div class="who">{esc(_ROLE_LABEL.get(kind, kind or ""))}'
            f'<span class="when"> {esc(extra)}</span></div>'
        )

        if kind == "attachment":
            parts.append(f'<div class="text">Injected context: '
                         f'{esc(str(message.get("subtype", "unknown")))}</div>')
            parts.append("</div>")
            continue

        for block in message.get("blocks", []):
            btype = block.get("type")
            if btype == "text":
                parts.append(f'<div class="text">{esc(block.get("text", ""))}</div>')
            elif btype == "thinking":
                if not include_thinking or not str(block.get("text", "")).strip():
                    continue
                parts.append("<details><summary>Thinking</summary>"
                             f'<pre>{esc(block.get("text", ""))}</pre></details>')
            elif btype == "tool_use":
                if not include_tools:
                    continue
                parts.append(
                    f'<details><summary>Tool call: {esc(str(block.get("name", "unknown")))}'
                    f'</summary><pre>{esc(_tool_input_text(block.get("input")))}</pre></details>'
                )
            elif btype == "tool_result":
                if not include_tools:
                    continue
                name = esc(str(block.get("name") or "tool"))
                flag = " (error)" if block.get("is_error") else ""
                parts.append(
                    f"<details><summary>Result from {name}{flag}</summary>"
                    f'<pre>{esc(_result_text(block.get("content")))}</pre></details>'
                )
            elif btype == "image":
                parts.append(f'<div class="text"><em>[image, '
                             f'{esc(str(block.get("media_type", "image")))}, '
                             f'{block.get("bytes", 0):,} bytes]</em></div>')
        parts.append("</div>")

    parts.append(
        "<footer>Exported by Claude Code Dashboard on "
        f"{esc(datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z'))}. "
        "Costs shown are local estimates, not billing data.</footer>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


def to_json(parsed: Dict[str, Any]) -> str:
    """Render the parsed conversation as indented JSON.

    This is the dashboard's normalised view, not a copy of the original
    JSONL; the untouched source is always still on disk.
    """
    payload = {
        "exported_by": "Claude Code Dashboard",
        "exported_at": datetime.now().astimezone().isoformat(),
        "note": "Normalised view of the transcript. Costs are local estimates.",
        "info": parsed.get("info", {}),
        "meta": parsed.get("meta"),
        "messages": parsed.get("messages", []),
        "errors": parsed.get("errors", []),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def suggested_filename(parsed: Dict[str, Any], fmt: str) -> str:
    """A safe default filename for the native save dialog."""
    info = parsed.get("info", {})
    title = (info.get("title") or info.get("session_id") or "conversation")[:60]
    safe = "".join(c if c.isalnum() or c in " -_" else "-" for c in title)
    safe = "-".join(safe.split())
    # Collapse runs of separators and trim them, so a title made only of
    # punctuation such as "///" does not become the filename "---".
    while "--" in safe:
        safe = safe.replace("--", "-")
    safe = safe.strip("-_") or "conversation"
    extension = EXPORT_FORMATS.get(fmt, {}).get("extension", "txt")
    return f"{safe}.{extension}"
