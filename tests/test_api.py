"""Tests for the HTTP surface, driven through FastAPI's test client."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from claude_dashboard import indexer


@pytest.fixture
def client(fake_claude_home, monkeypatch):
    """A test client whose index points at the fake ``~/.claude``."""
    from claude_dashboard.api import create_app

    fresh = indexer.SessionIndex(fake_claude_home / "projects")
    monkeypatch.setattr(indexer, "_INSTANCE", fresh, raising=False)
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True and body["version"]


def test_bootstrap_returns_everything_the_ui_needs(client):
    body = client.get("/api/bootstrap").json()
    assert body["stats"]["session_count"] == 2
    assert body["projects"][0]["path"] == "/home/tester/demo"
    assert body["paths"]["claude_home_exists"] is True
    assert "pricing" in body["config"]
    assert "claude-opus-5" in body["distinct"]["models"]


def test_sessions_list_is_filtered_sorted_and_paged(client):
    body = client.get("/api/sessions").json()
    assert body["total"] == 2
    assert len(body["sessions"]) == 2

    first = body["sessions"][0]
    assert "estimated_cost" in first
    assert "file_size_human" in first
    assert first["is_favorite"] is False
    assert first["tags"] == []
    assert first["project_exists"] is False       # /home/tester/demo is fictional

    assert client.get("/api/sessions?tool=Read").json()["total"] == 1
    assert client.get("/api/sessions?model=claude-opus-5").json()["total"] == 1
    assert client.get("/api/sessions?limit=1").json()["sessions"].__len__() == 1
    assert client.get("/api/sessions?limit=1&offset=1").json()["offset"] == 1


def test_session_detail_and_404(client):
    listing = client.get("/api/sessions").json()["sessions"]
    session_id = listing[0]["session_id"]
    assert client.get(f"/api/sessions/{session_id}").json()["session_id"] == session_id

    missing = client.get("/api/sessions/not-a-real-session")
    assert missing.status_code == 404
    assert "not-a-real-session" in missing.json()["detail"]


def test_messages_endpoint_returns_parsed_blocks(client):
    body = client.get("/api/sessions/11111111-2222-3333-4444-555555555555/messages").json()
    assert body["meta"]["title"] == "Add a health check endpoint"
    kinds = [m["kind"] for m in body["messages"]]
    assert "assistant" in kinds and "tool_result" in kinds
    assert body["errors"] == []


def test_messages_can_exclude_attachments(client):
    url = "/api/sessions/11111111-2222-3333-4444-555555555555/messages"
    with_attachments = client.get(url).json()["messages"]
    without = client.get(url + "?include_attachments=false").json()["messages"]
    assert len(without) < len(with_attachments)
    assert all(m["kind"] != "attachment" for m in without)


def test_raw_endpoint_windows_the_jsonl(client):
    body = client.get(
        "/api/sessions/11111111-2222-3333-4444-555555555555/raw?start=3&count=2"
    ).json()
    assert [line["line"] for line in body["lines"]] == [3, 4]


def test_search_endpoint(client):
    body = client.get("/api/search", params={"q": "health check"}).json()
    assert body["hits"]
    assert body["session_count"] == 1
    assert body["elapsed_seconds"] >= 0

    empty = client.get("/api/search", params={"q": "zzzznotfound"}).json()
    assert empty["hits"] == []

    assert client.get("/api/search", params={"q": ""}).status_code == 422


def test_usage_endpoint_and_granularities(client):
    for granularity in ("day", "week", "month"):
        body = client.get(f"/api/usage?granularity={granularity}").json()
        assert body["granularity"] == granularity
        assert body["totals"]["sessions"] == 2
        assert len(body["heatmap"]["grid"]) == 7
    assert client.get("/api/usage?granularity=fortnight").status_code == 422


def test_usage_csv_export(client):
    text = client.get("/api/usage/csv").text
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][0] == "session_id"
    assert len(rows) == 3


def test_config_round_trip(client):
    original = client.get("/api/config").json()
    assert original["window"]["width"] == 1400

    patched = client.patch("/api/config", json={"window": {"width": 1600}, "theme": "dark"}).json()
    assert patched["window"]["width"] == 1600
    assert patched["window"]["height"] == 900        # untouched keys survive
    assert patched["theme"] == "dark"
    assert client.get("/api/config").json()["window"]["width"] == 1600


def test_config_rejects_a_non_object_body(client):
    assert client.patch("/api/config", json=["nope"]).status_code in (400, 422)


def test_claude_config_is_read_only_and_redacts_identity(client, fake_claude_home):
    body = client.get("/api/claude-config").json()
    assert body["settings"]["theme"] == "dark"
    assert body["settings_path"].endswith("settings.json")
    # The fake home has no .claude.json alongside it.
    assert body["global"] is None or "oauthAccount" not in (body["global"] or {})


def test_index_rebuild_and_status(client):
    started = client.post("/api/index/rebuild").json()
    assert started["started"] is True
    status = client.get("/api/index/status").json()
    assert "percent" in status and "running" in status


def test_shutdown_sets_the_event(client):
    assert client.post("/api/shutdown").json()["stopping"] is True


# ====================================================================
# Deletion, trash and resume
# ====================================================================

def _ids(client):
    """Session ids currently in the index."""
    return [s["session_id"] for s in client.get("/api/sessions").json()["sessions"]]


def test_delete_moves_to_trash_and_leaves_the_index_consistent(client, fake_claude_home):
    ids = _ids(client)
    victim = ids[0]
    transcripts = list((fake_claude_home / "projects").rglob("*.jsonl"))
    assert len(transcripts) == 2

    result = client.post("/api/sessions/delete", json={"session_ids": [victim]}).json()

    assert result["deleted"] == 1
    assert len(list((fake_claude_home / "projects").rglob("*.jsonl"))) == 1
    assert victim not in _ids(client)

    trash = client.get("/api/trash").json()
    assert trash["total_files"] == 1
    assert trash["batches"][0]["items"][0]["session_id"] == victim
    assert trash["batches"][0]["items"][0]["title"], "the title should be recorded"


def test_delete_rejects_an_empty_or_malformed_request(client):
    assert client.post("/api/sessions/delete", json={}).status_code == 400
    assert client.post("/api/sessions/delete", json={"session_ids": []}).status_code == 400
    assert client.post("/api/sessions/delete", json={"session_ids": "abc"}).status_code == 400


def test_delete_refuses_an_id_that_is_not_indexed(client, fake_claude_home):
    response = client.post("/api/sessions/delete", json={"session_ids": ["../../etc/passwd"]})
    assert response.status_code == 404
    assert len(list((fake_claude_home / "projects").rglob("*.jsonl"))) == 2


def test_delete_is_all_or_nothing(client, fake_claude_home):
    """A batch mixing a real id with an unknown one must not delete either."""
    ids = _ids(client)
    response = client.post("/api/sessions/delete",
                           json={"session_ids": [ids[0], "no-such-session"]})
    assert response.status_code == 404
    assert len(list((fake_claude_home / "projects").rglob("*.jsonl"))) == 2


def test_bulk_delete_then_restore_round_trips(client, fake_claude_home):
    ids = _ids(client)
    before = {p.name: p.read_bytes() for p in (fake_claude_home / "projects").rglob("*.jsonl")}

    client.post("/api/sessions/delete", json={"session_ids": ids})
    assert list((fake_claude_home / "projects").rglob("*.jsonl")) == []

    batch_id = client.get("/api/trash").json()["batches"][0]["batch_id"]
    result = client.post("/api/trash/restore", json={"batch_id": batch_id}).json()

    assert sorted(result["restored"]) == sorted(ids)
    after = {p.name: p.read_bytes() for p in (fake_claude_home / "projects").rglob("*.jsonl")}
    assert after == before, "restored files must be byte-identical"
    assert sorted(_ids(client)) == sorted(ids)


def test_delete_preview_names_the_exact_file(client):
    session_id = _ids(client)[0]
    body = client.get(f"/api/sessions/{session_id}/delete-preview").json()
    assert body["path"].endswith(".jsonl")
    assert body["session_id"] == session_id
    assert body["file_size"] > 0


def test_trash_purge_respects_a_zero_retention(client):
    client.post("/api/sessions/delete", json={"session_ids": [_ids(client)[0]]})
    result = client.post("/api/trash/purge", json={"days": 0}).json()
    assert result["disabled"] is True
    assert client.get("/api/trash").json()["total_files"] == 1


def test_permanently_deleting_a_batch(client):
    client.post("/api/sessions/delete", json={"session_ids": [_ids(client)[0]]})
    batch_id = client.get("/api/trash").json()["batches"][0]["batch_id"]
    assert client.request("DELETE", f"/api/trash/{batch_id}").json()["removed_files"] == 1
    assert client.get("/api/trash").json()["total_files"] == 0


def test_a_bogus_batch_id_is_rejected(client):
    assert client.request("DELETE", "/api/trash/..%2F..%2Fetc").status_code in (400, 404)
    assert client.post("/api/trash/restore", json={"batch_id": "nope"}).status_code == 400


# ---------------------------------------------------------- maintenance

def test_maintenance_rules_report_candidates_without_deleting(client, fake_claude_home):
    for rule in ("older_than", "fewer_messages", "orphaned"):
        body = client.get("/api/maintenance/candidates", params={"rule": rule}).json()
        assert body["rule"] == rule
        assert body["count"] == len(body["sessions"])
        assert "description" in body
    # Both fixtures point at a project path that does not exist.
    assert client.get("/api/maintenance/candidates",
                      params={"rule": "orphaned"}).json()["count"] == 2
    assert len(list((fake_claude_home / "projects").rglob("*.jsonl"))) == 2


def test_an_unknown_maintenance_rule_is_rejected(client):
    assert client.get("/api/maintenance/candidates",
                      params={"rule": "drop_everything"}).status_code == 422


# --------------------------------------------------------------- resume

def test_resume_command_explains_a_missing_project_folder(client):
    session_id = _ids(client)[0]
    body = client.get(f"/api/sessions/{session_id}/resume-command").json()
    assert body["command"] == f"claude --resume {session_id}"
    assert body["project_exists"] is False
    assert "no longer exists" in body["reason"]


def test_resume_command_reports_a_present_folder(client, fake_claude_home, tmp_path, monkeypatch):
    """Point the fixture's cwd at a directory that really exists."""
    real = tmp_path / "real-project"
    real.mkdir()
    transcript = next((fake_claude_home / "projects").rglob("*.jsonl"))
    lines = transcript.read_text(encoding="utf-8").replace("/home/tester/demo", str(real))
    transcript.write_text(lines, encoding="utf-8")
    client.post("/api/index/rebuild?force=true")
    import time

    for _ in range(50):
        if not client.get("/api/index/status").json()["running"]:
            break
        time.sleep(0.02)

    session_id = transcript.stem
    body = client.get(f"/api/sessions/{session_id}/resume-command").json()
    assert body["project_exists"] is True
    assert body["reason"] is None
    assert str(real) in body["shell_line"]


def test_resuming_into_a_missing_folder_is_a_conflict(client):
    session_id = _ids(client)[0]
    response = client.post(f"/api/sessions/{session_id}/resume")
    assert response.status_code == 409
    assert "no longer exists" in response.json()["detail"]


def test_opening_a_missing_folder_is_a_conflict(client):
    session_id = _ids(client)[0]
    assert client.post(f"/api/sessions/{session_id}/open?where=folder").status_code == 409
    assert client.post(f"/api/sessions/{session_id}/open?where=editor").status_code == 409


def test_an_unknown_open_target_is_rejected(client):
    session_id = _ids(client)[0]
    assert client.post(f"/api/sessions/{session_id}/open?where=rm").status_code == 422


def test_resume_endpoints_404_for_an_unknown_session(client):
    assert client.get("/api/sessions/ghost/resume-command").status_code == 404
    assert client.post("/api/sessions/ghost/resume").status_code == 404


# ====================================================================
# Annotations, instructions, assets, todos and backup
# ====================================================================

def test_annotations_round_trip_and_never_touch_claude(client, fake_claude_home):
    session_id = _ids(client)[0]
    before = {p: p.read_bytes() for p in (fake_claude_home / "projects").rglob("*.jsonl")}

    starred = client.post(f"/api/sessions/{session_id}/annotate", json={"favorite": True}).json()
    assert starred["is_favorite"] is True

    tagged = client.post(
        f"/api/sessions/{session_id}/annotate",
        json={"tags": ["keep", " demo ", ""], "note": "Worth revisiting."},
    ).json()
    assert tagged["tags"] == ["demo", "keep"], "trimmed, de-duplicated and sorted"
    assert tagged["note"] == "Worth revisiting."

    listed = client.get("/api/sessions").json()["sessions"]
    entry = next(s for s in listed if s["session_id"] == session_id)
    assert entry["is_favorite"] and entry["tags"] == ["demo", "keep"]

    after = {p: p.read_bytes() for p in (fake_claude_home / "projects").rglob("*.jsonl")}
    assert after == before, "annotating must not modify any transcript"


def test_annotations_can_be_cleared(client):
    session_id = _ids(client)[0]
    client.post(f"/api/sessions/{session_id}/annotate", json={"favorite": True, "tags": ["x"]})
    cleared = client.post(
        f"/api/sessions/{session_id}/annotate",
        json={"favorite": False, "tags": [], "note": "   "},
    ).json()
    assert cleared["is_favorite"] is False
    assert cleared["tags"] == []
    assert cleared["note"] == ""


def test_only_favorites_filter(client):
    ids = _ids(client)
    client.post(f"/api/sessions/{ids[0]}/annotate", json={"favorite": True})
    body = client.get("/api/sessions", params={"only_favorites": True}).json()
    assert body["total"] == 1 and body["sessions"][0]["session_id"] == ids[0]


def test_tags_endpoint_counts_usage(client):
    ids = _ids(client)
    client.post(f"/api/sessions/{ids[0]}/annotate", json={"tags": ["alpha", "beta"]})
    client.post(f"/api/sessions/{ids[1]}/annotate", json={"tags": ["alpha"]})
    tags = client.get("/api/tags").json()["tags"]
    assert {"name": "alpha", "count": 2} in tags
    assert {"name": "beta", "count": 1} in tags


def test_annotating_an_unknown_session_is_404(client):
    assert client.post("/api/sessions/ghost/annotate", json={"favorite": True}).status_code == 404


def test_bad_tag_payloads_are_rejected(client):
    session_id = _ids(client)[0]
    assert client.post(
        f"/api/sessions/{session_id}/annotate", json={"tags": "not a list"}
    ).status_code == 400


# -------------------------------------------------------- instructions

def test_claude_md_list_and_empty_state(client, fake_claude_home):
    body = client.get("/api/claude-md").json()
    assert body["files"] == [], "the fixture has no CLAUDE.md"
    assert body["global_path"].endswith("CLAUDE.md")
    assert body["backups"] == []


def test_claude_md_save_and_read_back(client, fake_claude_home):
    path = str(fake_claude_home / "CLAUDE.md")
    saved = client.post("/api/claude-md/save",
                        json={"path": path, "content": "# Rules\n"}).json()
    assert saved["backup"] is None and saved["bytes"] == 8

    again = client.post("/api/claude-md/save",
                        json={"path": path, "content": "# Changed\n"}).json()
    assert again["backup"], "the second save must back up the first"

    read = client.get("/api/claude-md/read", params={"path": path}).json()
    assert read["content"] == "# Changed\n" and read["exists"] is True

    listed = client.get("/api/claude-md").json()
    assert len(listed["files"]) == 1
    assert len(listed["backups"]) == 1


def test_claude_md_refuses_a_path_outside_the_allowed_places(client, tmp_path):
    stray = tmp_path / "CLAUDE.md"
    stray.write_text("# no", encoding="utf-8")
    assert client.post("/api/claude-md/save",
                       json={"path": str(stray), "content": "x"}).status_code == 400
    assert client.get("/api/claude-md/read", params={"path": str(stray)}).status_code == 400
    assert stray.read_text(encoding="utf-8") == "# no"


def test_claude_md_refuses_a_non_instruction_file(client, fake_claude_home):
    target = str(fake_claude_home / "settings.json")
    assert client.post("/api/claude-md/save",
                       json={"path": target, "content": "{}"}).status_code == 400
    assert "theme" in (fake_claude_home / "settings.json").read_text(encoding="utf-8")


# --------------------------------------------------------------- assets

def test_assets_endpoint_reports_an_empty_install_honestly(client):
    body = client.get("/api/assets").json()
    assert body["assets"] == []
    assert body["searched"], "it should still say where it looked"


def test_assets_are_listed_and_previewable(client, fake_claude_home):
    commands = fake_claude_home / "commands"
    commands.mkdir()
    (commands / "ship.md").write_text(
        "---\nname: ship\ndescription: Ships it\n---\n\nBody text.\n", encoding="utf-8"
    )
    body = client.get("/api/assets").json()
    assert body["counts"] == {"command": 1}

    preview = client.get("/api/assets/read",
                         params={"path": body["assets"][0]["path"]}).json()
    assert "Body text." in preview["content"]
    assert preview["frontmatter"]["name"] == "ship"


def test_reading_an_asset_outside_claude_home_is_refused(client, tmp_path):
    outsider = tmp_path / "secret.md"
    outsider.write_text("secret", encoding="utf-8")
    assert client.get("/api/assets/read", params={"path": str(outsider)}).status_code == 400


# ---------------------------------------------------------------- todos

def test_todos_reports_a_missing_directory(client):
    body = client.get("/api/todos").json()
    assert body["exists"] is False and body["total"] == 0


def test_todos_are_linked_to_session_titles(client, fake_claude_home):
    todos = fake_claude_home / "todos"
    todos.mkdir()
    session_id = _ids(client)[0]
    (todos / f"{session_id}-agent-a.json").write_text(
        json.dumps([{"content": "Do the thing", "status": "pending"}]), encoding="utf-8"
    )
    body = client.get("/api/todos").json()
    assert body["total"] == 1
    assert body["lists"][0]["session_title"], "the title should be resolved from the index"


# --------------------------------------------------------------- backup

def test_backup_writes_an_archive(client, tmp_path):
    result = client.post("/api/backup", json={"destination": str(tmp_path)}).json()
    archive = Path(result["path"])
    assert archive.is_file()
    with zipfile.ZipFile(archive) as zf:
        assert len(zf.namelist()) == 2


def test_backup_requires_a_destination(client):
    assert client.post("/api/backup", json={}).status_code == 400


# --------------------------------------------------------------- changes

def test_changes_endpoint_reports_watcher_state(client):
    body = client.get("/api/changes").json()
    assert "revision" in body and "watching" in body
    assert isinstance(body["revision"], int)
