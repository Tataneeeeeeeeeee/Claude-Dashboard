"""FastAPI application: the local HTTP surface consumed by the webview.

The server binds to ``127.0.0.1`` on a random free port and is never exposed
on another interface (see :mod:`agentboard.server`).  It makes no
outbound requests of any kind.

Handlers are thin: they resolve query parameters, call into
:mod:`agentboard.indexer` or :mod:`agentboard.usage`, and return
JSON.  Errors become ``{"detail": ...}`` with a useful message, which the UI
surfaces as a toast rather than a blank screen.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import load_config, save_config, update_config
from .indexer import get_index
from .parser import parse_conversation
from .paths import (
    app_home,
    claude_home,
    claude_json_path,
    claude_projects_dir,
    human_size,
    trash_dir,
)
from . import actions, content, exporters
from .usage import aggregate, session_cost, stats_csv
from .watcher import start_watcher

__all__ = ["create_app"]

WEB_DIR = Path(__file__).resolve().parent / "web"


def _index_ready() -> None:
    """Build the index on first access so any endpoint works standalone."""
    get_index().ensure_built()


def _session_payload(meta, pricing: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Session metadata plus the app-side annotations kept out of ``~/.claude``."""
    data = meta.to_dict()
    data["estimated_cost"] = round(session_cost(meta, pricing), 6)
    data["file_size_human"] = human_size(meta.file_size)
    data["is_favorite"] = meta.session_id in config.get("favorites", [])
    data["tags"] = config.get("tags", {}).get(meta.session_id, [])
    data["note"] = config.get("notes", {}).get(meta.session_id, "")
    data["project_exists"] = bool(meta.project_path) and Path(meta.project_path).is_dir()
    return data


def _dir_size(path: Path) -> int:
    """Total bytes under *path*, ignoring anything unreadable."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _bootstrap_payload() -> Dict[str, Any]:
    """Everything the UI needs on first paint."""
    index = get_index()
    index.ensure_built()
    config = load_config()
    home = claude_home()
    return {
        "version": __version__,
        "config": config,
        "stats": index.stats(),
        "distinct": index.distinct(),
        "projects": [p.to_dict() for p in index.projects],
        "paths": {
            "claude_home": str(home),
            "claude_home_exists": home.is_dir(),
            "projects_dir": str(claude_projects_dir()),
            "projects_dir_exists": claude_projects_dir().is_dir(),
            "claude_json": str(claude_json_path()),
            "app_home": str(app_home()),
        },
    }


def _sessions_payload(
    *,
    project: Optional[str] = None,
    model: Optional[str] = None,
    tool: Optional[str] = None,
    branch: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    query: Optional[str] = None,
    tag: Optional[str] = None,
    only_favorites: bool = False,
    min_messages: Optional[int] = None,
    max_messages: Optional[int] = None,
    sort: str = "recent",
    limit: int = 1000,
    offset: int = 0,
) -> Dict[str, Any]:
    """Filtered, sorted, paged session list."""
    _index_ready()
    index = get_index()
    config = load_config()
    metas = index.filter(
        project=project,
        model=model,
        tool=tool,
        branch=branch,
        since=since,
        until=until,
        query=query,
        tag=tag,
        favorites=config.get("favorites", []),
        only_favorites=only_favorites,
        tags=config.get("tags", {}),
        min_messages=min_messages,
        max_messages=max_messages,
        sort="recent",
    )
    metas = index.sort(metas, sort, config["pricing"])
    window = metas[offset: offset + limit]
    pricing = config["pricing"]
    return {
        "total": len(metas),
        "offset": offset,
        "limit": limit,
        "sessions": [_session_payload(m, pricing, config) for m in window],
    }


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start-up and shut-down work for the local server.

    The trash retention policy runs once, and the filesystem watcher is
    started if the user has auto-refresh on. Neither may stop the
    application from starting, so failures are recorded on app state.
    """
    try:
        app.state.purge_result = actions.purge_trash()
    except Exception as exc:  # pragma: no cover - defensive
        app.state.purge_result = {"error": f"{type(exc).__name__}: {exc}"}

    if load_config().get("auto_refresh", True):
        def refreshed(paths: Set[str]) -> None:
            """Re-scan the transcripts that changed and record a revision."""
            index = get_index()
            for path in paths:
                index.refresh_path(path)
            app.state.revision += 1
            app.state.last_change = datetime.now(timezone.utc).isoformat()

        try:
            app.state.watcher = start_watcher(claude_projects_dir(), refreshed)
        except Exception:  # pragma: no cover - defensive
            app.state.watcher = None
    yield
    watcher = getattr(app.state, "watcher", None)
    if watcher is not None:
        watcher.stop()


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    app = FastAPI(
        title="Agentboard",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    shutdown_event = threading.Event()
    app.state.shutdown_event = shutdown_event
    app.state.purge_result = None
    app.state.watcher = None
    # Bumped whenever the watcher notices a transcript change, so the UI
    # can poll one small endpoint instead of re-fetching the whole index.
    app.state.revision = 0
    app.state.last_change = None

    # ------------------------------------------------------------- meta

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        """Liveness probe used by the window bootstrap before it loads the UI."""
        return {"ok": True, "version": __version__, "pid": os.getpid()}

    @app.get("/api/bootstrap")
    def bootstrap() -> Dict[str, Any]:
        """Everything the UI needs on first paint, in one round trip."""
        return _bootstrap_payload()

    @app.get("/api/changes")
    def changes() -> Dict[str, Any]:
        """Revision counter the UI polls to notice live transcript changes."""
        watcher = app.state.watcher
        return {
            "revision": app.state.revision,
            "last_change": app.state.last_change,
            "watching": watcher is not None,
            "events_seen": watcher.events_seen if watcher is not None else 0,
        }

    @app.get("/api/index/status")
    def index_status() -> Dict[str, Any]:
        """Progress of the current or most recent indexing run."""
        return get_index().progress.to_dict()

    @app.post("/api/index/rebuild")
    def index_rebuild(force: bool = Query(True, description="Ignore the disk cache")) -> Dict[str, Any]:
        """Re-scan every transcript in a background thread."""
        index = get_index()
        if index.progress.running:
            return {"started": False, "reason": "an index build is already running"}
        threading.Thread(target=index.build, kwargs={"force": force}, daemon=True).start()
        return {"started": True, "force": force}

    # --------------------------------------------------------- sessions

    @app.get("/api/sessions")
    def list_sessions(
        project: Optional[str] = None,
        model: Optional[str] = None,
        tool: Optional[str] = None,
        branch: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        query: Optional[str] = None,
        tag: Optional[str] = None,
        only_favorites: bool = False,
        min_messages: Optional[int] = None,
        max_messages: Optional[int] = None,
        sort: str = "recent",
        limit: int = Query(1000, ge=1, le=10000),
        offset: int = Query(0, ge=0),
    ) -> Dict[str, Any]:
        """Filtered, sorted session list for the sidebar."""
        return _sessions_payload(
            project=project, model=model, tool=tool, branch=branch,
            since=since, until=until, query=query, tag=tag,
            only_favorites=only_favorites, min_messages=min_messages,
            max_messages=max_messages, sort=sort, limit=limit, offset=offset,
        )

    @app.get("/api/sessions/{session_id}")
    def session_detail(session_id: str) -> Dict[str, Any]:
        """Metadata for one session."""
        _index_ready()
        meta = get_index().get(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"No session indexed with id {session_id!r}")
        config = load_config()
        return _session_payload(meta, config["pricing"], config)

    @app.get("/api/sessions/{session_id}/messages")
    def session_messages(
        session_id: str,
        include_attachments: bool = True,
        tool_output_limit: int = Query(20000, ge=200, le=2_000_000),
    ) -> Dict[str, Any]:
        """Full parsed conversation for the viewer."""
        _index_ready()
        meta = get_index().get(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"No session indexed with id {session_id!r}")
        path = Path(meta.path)
        if not path.is_file():
            raise HTTPException(status_code=410, detail=f"Transcript no longer on disk: {path}")
        try:
            parsed = parse_conversation(
                path,
                tool_output_limit=tool_output_limit,
                include_attachments=include_attachments,
            )
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not read {path}: {exc}") from exc
        config = load_config()
        parsed["meta"] = _session_payload(meta, config["pricing"], config)
        return parsed

    @app.get("/api/sessions/{session_id}/raw")
    def session_raw(
        session_id: str,
        start: int = Query(1, ge=1),
        count: int = Query(200, ge=1, le=5000),
    ) -> Dict[str, Any]:
        """A window of raw JSONL lines, for the "view source" panel."""
        _index_ready()
        meta = get_index().get(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"No session indexed with id {session_id!r}")
        lines: List[Dict[str, Any]] = []
        try:
            with open(meta.path, "r", encoding="utf-8", errors="replace") as handle:
                for number, text in enumerate(handle, start=1):
                    if number < start:
                        continue
                    if len(lines) >= count:
                        break
                    lines.append({"line": number, "text": text.rstrip("\n")})
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"session_id": session_id, "start": start, "lines": lines}

    @app.get("/api/sessions/{session_id}/export")
    def session_export(
        session_id: str,
        format: str = Query("markdown", pattern="^(markdown|html|json)$"),
        include_thinking: bool = True,
        include_tools: bool = True,
        include_attachments: bool = False,
        tool_output_limit: int = Query(20000, ge=200, le=2_000_000),
    ) -> Dict[str, Any]:
        """Render one conversation for saving.

        The rendered text is returned as JSON rather than a download, because
        the file is written by the *native* save dialog through the pywebview
        bridge - the webview sandbox blocks downloads a page starts itself.
        """
        _index_ready()
        meta = get_index().get(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"No session indexed with id {session_id!r}")
        path = Path(meta.path)
        if not path.is_file():
            raise HTTPException(status_code=410, detail=f"Transcript no longer on disk: {path}")
        try:
            parsed = parse_conversation(
                path,
                tool_output_limit=tool_output_limit,
                include_attachments=include_attachments,
            )
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not read {path}: {exc}") from exc

        config = load_config()
        parsed["meta"] = _session_payload(meta, config["pricing"], config)

        options = {
            "include_thinking": include_thinking,
            "include_tools": include_tools,
            "include_attachments": include_attachments,
        }
        if format == "markdown":
            content = exporters.to_markdown(parsed, **options)
        elif format == "html":
            content = exporters.to_html(parsed, **options)
        else:
            content = exporters.to_json(parsed)

        return {
            "format": format,
            "filename": exporters.suggested_filename(parsed, format),
            "mime": exporters.EXPORT_FORMATS[format]["mime"],
            "bytes": len(content.encode("utf-8")),
            "content": content,
        }

    # ----------------------------------------------------------- search

    @app.get("/api/search")
    def search(
        q: str = Query(..., min_length=1),
        project: Optional[str] = None,
        regex: bool = False,
        case_sensitive: bool = False,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Full-text search across every transcript."""
        _index_ready()
        config = load_config()
        settings = config.get("search", {})
        return get_index().search(
            q,
            limit=limit or int(settings.get("max_results", 400)),
            context_chars=int(settings.get("context_chars", 160)),
            project=project,
            regex=regex,
            case_sensitive=case_sensitive,
        )

    # --------------------------------------------------------- maintenance

    @app.get("/api/maintenance/candidates")
    def maintenance_candidates(
        rule: str = Query(..., pattern="^(older_than|fewer_messages|orphaned)$"),
        days: int = Query(30, ge=0, le=100000),
        messages: int = Query(2, ge=0, le=100000),
    ) -> Dict[str, Any]:
        """Sessions matching a bulk-maintenance rule, for review before deleting.

        Nothing is removed here; the caller still has to confirm and then
        call the delete endpoint with the ids it wants gone.
        """
        _index_ready()
        index = get_index()
        config = load_config()
        if rule == "older_than":
            metas = index.older_than(days)
            description = f"sessions with no activity in the last {days} days"
        elif rule == "fewer_messages":
            metas = index.smaller_than(messages)
            description = f"sessions with fewer than {messages} messages"
        else:
            metas = index.orphaned()
            description = "sessions whose project folder no longer exists"
        pricing = config["pricing"]
        return {
            "rule": rule,
            "description": description,
            "count": len(metas),
            "total_bytes": sum(m.file_size for m in metas),
            "sessions": [_session_payload(m, pricing, config) for m in metas],
        }

    # -------------------------------------------------------------- delete

    @app.post("/api/sessions/delete")
    def delete_sessions(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        """Move the named sessions to the dashboard's trash.

        The request carries session ids, never paths: the path comes from
        the index and is validated again before anything moves.
        """
        _index_ready()
        ids = payload.get("session_ids")
        if not isinstance(ids, list) or not ids:
            raise HTTPException(status_code=400, detail="session_ids must be a non-empty list")

        index = get_index()
        paths: List[str] = []
        metadata: Dict[str, Dict[str, Any]] = {}
        missing: List[str] = []
        for session_id in ids:
            meta = index.get(str(session_id))
            if meta is None:
                missing.append(str(session_id))
                continue
            paths.append(meta.path)
            metadata[meta.session_id] = {
                "title": meta.title,
                "project_path": meta.project_path,
                "message_count": meta.message_count,
            }
        if missing:
            raise HTTPException(
                status_code=404, detail=f"Not in the index: {', '.join(missing)}"
            )

        try:
            batch = actions.move_to_trash(paths, metadata, note=str(payload.get("note", "")))
        except actions.SafetyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        for path in paths:
            index.refresh_path(path)
        return {"deleted": len(batch.items), "batch": batch.to_dict()}

    @app.get("/api/sessions/{session_id}/delete-preview")
    def delete_preview(session_id: str) -> Dict[str, Any]:
        """Exactly which file a deletion would move, for the confirmation text."""
        _index_ready()
        meta = get_index().get(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"No session indexed with id {session_id!r}")
        return {
            "session_id": meta.session_id,
            "title": meta.title,
            "path": meta.path,
            "file_size": meta.file_size,
            "message_count": meta.message_count,
        }

    # --------------------------------------------------------------- trash

    @app.get("/api/trash")
    def trash_list() -> Dict[str, Any]:
        """Everything currently recoverable, newest batch first."""
        batches = [b.to_dict() for b in actions.list_trash()]
        config = load_config()
        return {
            "batches": batches,
            "total_bytes": sum(b["total_bytes"] for b in batches),
            "total_files": sum(b["count"] for b in batches),
            "retention_days": config.get("trash_retention_days", 30),
            "trash_path": str(trash_dir()),
        }

    @app.post("/api/trash/restore")
    def trash_restore(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        """Put files from one batch back where they came from."""
        batch_id = str(payload.get("batch_id", ""))
        session_ids = payload.get("session_ids")
        try:
            result = actions.restore_batch(
                batch_id,
                session_ids if isinstance(session_ids, list) else None,
            )
        except actions.SafetyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        get_index().build()
        return result

    @app.post("/api/trash/purge")
    def trash_purge(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        """Apply the retention policy, or a one-off age given in the body."""
        days = payload.get("days") if isinstance(payload, dict) else None
        return actions.purge_trash(int(days) if isinstance(days, (int, float)) else None)

    @app.delete("/api/trash/{batch_id}")
    def trash_delete(batch_id: str) -> Dict[str, Any]:
        """Remove one batch for good. This is the only real unlink."""
        try:
            return actions.delete_batch_permanently(batch_id)
        except actions.SafetyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # -------------------------------------------------------------- resume

    @app.get("/api/sessions/{session_id}/resume-command")
    def resume_command(session_id: str) -> Dict[str, Any]:
        """The command a terminal would run, and whether it can be launched."""
        _index_ready()
        meta = get_index().get(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"No session indexed with id {session_id!r}")
        config = load_config()
        exists = bool(meta.project_path) and Path(meta.project_path).is_dir()
        try:
            command = actions.resume_command_string(meta.session_id, config)
        except actions.SafetyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "session_id": meta.session_id,
            "cwd": meta.project_path,
            "project_exists": exists,
            "command": command,
            "shell_line": f"cd {meta.project_path!r} && {command}" if exists else command,
            "editor_available": actions.editor_available(config),
            "reason": None if exists else
                      f"The project folder {meta.project_path or '(unknown)'} no longer exists.",
        }

    @app.post("/api/sessions/{session_id}/resume")
    def resume_session(session_id: str) -> Dict[str, Any]:
        """Open a terminal in the session's original directory."""
        _index_ready()
        meta = get_index().get(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"No session indexed with id {session_id!r}")
        try:
            return actions.launch_terminal(meta.project_path, meta.session_id, load_config())
        except actions.SafetyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/sessions/{session_id}/open")
    def open_session_location(
        session_id: str,
        where: str = Query("folder", pattern="^(folder|editor)$"),
    ) -> Dict[str, Any]:
        """Open the session's project directory in the file manager or editor."""
        _index_ready()
        meta = get_index().get(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"No session indexed with id {session_id!r}")
        try:
            if where == "folder":
                return actions.open_folder(meta.project_path)
            return actions.open_in_editor(meta.project_path, load_config())
        except actions.SafetyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # ------------------------------------------------------------ usage

    @app.get("/api/usage")
    def usage_dashboard(
        granularity: str = Query("day", pattern="^(day|week|month)$"),
        project: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        include_home_size: bool = False,
    ) -> Dict[str, Any]:
        """Everything the usage dashboard renders, in one payload."""
        _index_ready()
        index = get_index()
        config = load_config()
        metas = index.filter(project=project, since=since, until=until)
        home_size = _dir_size(claude_home()) if include_home_size else None
        return aggregate(metas, granularity, config["pricing"], home_size)

    @app.get("/api/usage/csv", response_class=PlainTextResponse)
    def usage_csv(project: Optional[str] = None) -> str:
        """Aggregated per-session stats as CSV text."""
        _index_ready()
        config = load_config()
        metas = get_index().filter(project=project)
        return stats_csv(metas, config["pricing"])

    # ----------------------------------------------------------- config

    @app.get("/api/config")
    def get_config() -> Dict[str, Any]:
        """The dashboard's own configuration."""
        return load_config()

    @app.patch("/api/config")
    def patch_config(patch: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        """Deep-merge a patch into the configuration and persist it."""
        if not isinstance(patch, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")
        return update_config(patch)

    @app.get("/api/claude-config")
    def claude_config() -> Dict[str, Any]:
        """Read-only view of Claude Code's own configuration files."""
        result: Dict[str, Any] = {"settings": None, "global": None, "errors": []}

        settings_file = claude_home() / "settings.json"
        if settings_file.is_file():
            try:
                result["settings"] = json.loads(settings_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                result["errors"].append(f"{settings_file}: {exc}")
        result["settings_path"] = str(settings_file)

        global_file = claude_json_path()
        if global_file.is_file():
            try:
                raw = json.loads(global_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                result["errors"].append(f"{global_file}: {exc}")
                raw = {}
            # Drop identity and bulky feature-flag caches before they reach
            # the browser; the viewer shows a redaction note instead.
            redacted = {}
            hidden = []
            for key, value in raw.items():
                if key in {"oauthAccount", "userID", "machineID"}:
                    hidden.append(key)
                    continue
                if key.startswith("cached") or key.endswith("Cache"):
                    hidden.append(key)
                    continue
                redacted[key] = value
            projects = redacted.pop("projects", {})
            result["global"] = redacted
            result["redacted_keys"] = sorted(hidden)
            result["projects"] = projects if isinstance(projects, dict) else {}
            servers: List[Dict[str, Any]] = []
            if isinstance(projects, dict):
                for project_path, entry in projects.items():
                    if not isinstance(entry, dict):
                        continue
                    for name, definition in (entry.get("mcpServers") or {}).items():
                        servers.append(
                            {"project": project_path, "name": name, "definition": definition}
                        )
            result["mcp_servers"] = servers
        result["global_path"] = str(global_file)
        return result

    # ------------------------------------------------------- instructions

    @app.get("/api/claude-md")
    def claude_md_list() -> Dict[str, Any]:
        """Instruction files found in ~/.claude and the indexed projects."""
        _index_ready()
        projects = [p.path for p in get_index().projects if p.exists]
        return {
            "files": content.find_claude_md(projects),
            "searched_projects": projects,
            "global_path": str(claude_home() / "CLAUDE.md"),
            "backups": content.list_claude_md_backups()[:50],
        }

    @app.get("/api/claude-md/read")
    def claude_md_read(path: str = Query(...)) -> Dict[str, Any]:
        """Read one instruction file for the editor."""
        _index_ready()
        projects = [p.path for p in get_index().projects if p.exists]
        try:
            target = content.validate_claude_md_path(path, projects)
        except actions.SafetyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.is_file():
            return {"path": str(target), "content": "", "exists": False, "size": 0}
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "path": str(target), "content": text, "exists": True,
            "size": target.stat().st_size,
        }

    @app.post("/api/claude-md/save")
    def claude_md_save(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        """Write an instruction file after taking a timestamped backup."""
        _index_ready()
        projects = [p.path for p in get_index().projects if p.exists]
        try:
            return content.save_claude_md(
                str(payload.get("path", "")),
                payload.get("content", ""),
                projects,
            )
        except actions.SafetyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------ assets

    @app.get("/api/assets")
    def assets() -> Dict[str, Any]:
        """Skills, slash commands and sub-agents visible to Claude Code."""
        return content.list_assets()

    @app.get("/api/assets/read")
    def asset_read(path: str = Query(...)) -> Dict[str, Any]:
        """Read one asset definition for the preview pane."""
        try:
            return content.read_asset(path)
        except actions.SafetyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/todos")
    def todos() -> Dict[str, Any]:
        """Task lists from ~/.claude/todos, linked to their sessions."""
        _index_ready()
        titles = {m.session_id: m.title for m in get_index().sessions}
        return content.read_todos(titles)

    # ------------------------------------------------------------ backup

    @app.post("/api/backup")
    def backup(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        """ZIP the whole projects tree to a chosen directory or file."""
        destination = str(payload.get("destination") or "") if isinstance(payload, dict) else ""
        if not destination:
            raise HTTPException(status_code=400, detail="A destination path is required")
        try:
            return content.backup_projects(destination)
        except actions.SafetyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------- session annotations

    @app.post("/api/sessions/{session_id}/annotate")
    def annotate_session(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        """Set the favourite flag, tags or note for one session.

        These live in the dashboard's own config, never in ~/.claude, so
        annotating a session leaves Claude Code's data untouched.
        """
        _index_ready()
        if get_index().get(session_id) is None:
            raise HTTPException(status_code=404, detail=f"No session indexed with id {session_id!r}")

        config = load_config()
        favorites = list(config.get("favorites", []))
        tags = dict(config.get("tags", {}))
        notes = dict(config.get("notes", {}))

        if "favorite" in payload:
            if payload["favorite"]:
                if session_id not in favorites:
                    favorites.append(session_id)
            else:
                favorites = [f for f in favorites if f != session_id]
        if "tags" in payload:
            values = payload["tags"]
            if not isinstance(values, list):
                raise HTTPException(status_code=400, detail="tags must be a list of strings")
            cleaned = sorted({str(v).strip()[:40] for v in values if str(v).strip()})
            if cleaned:
                tags[session_id] = cleaned
            else:
                tags.pop(session_id, None)
        if "note" in payload:
            text = str(payload["note"])[:20000]
            if text.strip():
                notes[session_id] = text
            else:
                notes.pop(session_id, None)

        updated = save_config({**config, "favorites": favorites, "tags": tags, "notes": notes})
        return {
            "session_id": session_id,
            "is_favorite": session_id in updated["favorites"],
            "tags": updated["tags"].get(session_id, []),
            "note": updated["notes"].get(session_id, ""),
        }

    @app.get("/api/tags")
    def all_tags() -> Dict[str, Any]:
        """Every tag in use, with how many sessions carry it."""
        counts: Dict[str, int] = {}
        for values in load_config().get("tags", {}).values():
            for tag in values if isinstance(values, list) else []:
                counts[tag] = counts.get(tag, 0) + 1
        return {"tags": [{"name": k, "count": v} for k, v in sorted(counts.items())]}

    # -------------------------------------------------------- lifecycle

    @app.post("/api/shutdown")
    def shutdown() -> Dict[str, Any]:
        """Ask the server to stop; the window bootstrap waits on this event."""
        shutdown_event.set()
        return {"stopping": True}

    # -------------------------------------------------------------- UI

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/", response_class=HTMLResponse)
        def index_page(
            open: Optional[str] = None,
            view: Optional[str] = None,
        ) -> HTMLResponse:
            """Serve the single-page UI with its first screen of data inlined.

            Injecting the bootstrap payload and the first page of sessions
            removes two round trips from startup, so the window opens with
            content instead of a spinner.  If anything goes wrong the marker
            is simply omitted and the page falls back to fetching.
            """
            target = WEB_DIR / "index.html"
            if not target.is_file():
                raise HTTPException(status_code=404, detail="UI not built: web/index.html missing")
            html = target.read_text(encoding="utf-8")
            # The page's own CSP forbids inline scripts, so the injected
            # payload is authorised with a fresh per-response nonce.
            nonce = secrets.token_urlsafe(16)
            html = html.replace("__CSP_NONCE__", nonce)
            try:
                payload = {
                    "bootstrap": _bootstrap_payload(),
                    "sessions": _sessions_payload(limit=400),
                }
                # A deep link such as /?open=<session-id> renders the
                # conversation on first paint instead of after two fetches.
                if view == "usage":
                    # Same reasoning as the conversation preload: the
                    # dashboard paints on first frame instead of after a
                    # round trip.
                    config = load_config()
                    payload["usage"] = aggregate(
                        get_index().filter(), "day", config["pricing"], _dir_size(claude_home())
                    )
                if open:
                    meta = get_index().get(open)
                    if meta is not None and Path(meta.path).is_file():
                        conversation = parse_conversation(Path(meta.path))
                        config = load_config()
                        conversation["meta"] = _session_payload(
                            meta, config["pricing"], config
                        )
                        payload["conversation"] = conversation
                # A literal `</script>` inside the JSON would close the tag.
                encoded = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
                html = html.replace(
                    "</head>",
                    f'<script nonce="{nonce}">window.__PRELOAD__={encoded};</script>\n</head>',
                    1,
                )
            except Exception as exc:
                # The page still works by fetching; surface the reason anyway.
                html = html.replace(
                    "</head>",
                    f'<script nonce="{nonce}">window.__PRELOAD_ERROR__='
                    f'{json.dumps(f"{type(exc).__name__}: {exc}")};</script>\n</head>',
                    1,
                )
            # The page now carries live data, so it must never be cached.
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.exception_handler(Exception)
    async def unhandled(_request, exc: Exception) -> JSONResponse:  # pragma: no cover
        """Turn any unexpected error into a JSON message the UI can toast."""
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(exc).__name__}: {exc}"},
        )

    return app
