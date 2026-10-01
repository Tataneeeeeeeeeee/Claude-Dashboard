"""The provider-aware HTTP surface: listing, settings, custom providers,
filters and the comparison payload."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentboard import indexer

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "generic"

SPEC = {
    "id": "mytool",
    "name": "My Tool",
    "color": "#e11d48",
    "home": str(FIXTURES / "jsonl-tool"),
    "glob": "logs/**/*.jsonl",
    "format": "jsonl",
    "session": {"id": "session_id", "cwd": "cwd"},
    "fields": {"role": "role", "text": "content", "timestamp": "ts", "model": "model",
               "input_tokens": "usage.prompt_tokens", "output_tokens": "usage.completion_tokens"},
}


@pytest.fixture
def client(fake_claude_home, fake_codex_home, fake_gemini_home, monkeypatch):
    """A client whose index follows the live provider registry."""
    from agentboard.api import create_app

    monkeypatch.setattr(indexer, "_INSTANCE", indexer.SessionIndex(), raising=False)
    with TestClient(create_app()) as test_client:
        yield test_client


def _ids(body):
    return [p["id"] for p in body["providers"]]


def test_every_builtin_provider_is_listed_with_its_state(client):
    body = client.get("/api/providers").json()
    assert _ids(body)[:3] == ["claude", "codex", "gemini"]
    by_id = {p["id"]: p for p in body["providers"]}
    for provider_id, sessions in (("claude", 2), ("codex", 2), ("gemini", 3)):
        provider = by_id[provider_id]
        assert provider["detection"]["data_found"] is True
        assert provider["enabled"] is True and provider["indexed"] is True
        assert provider["stats"]["sessions"] == sessions
        assert provider["color"].startswith("#")
    assert by_id["claude"]["capabilities"]["delete"] is True
    assert by_id["gemini"]["capabilities"]["delete"] is False


def test_bootstrap_carries_the_product_and_providers(client):
    body = client.get("/api/bootstrap").json()
    assert body["product"] == "Agentboard"
    assert {p["id"] for p in body["providers"]} >= {"claude", "codex", "gemini"}
    assert body["stats"]["providers"]["codex"]["sessions"] == 2


def test_sessions_and_search_filter_by_provider(client):
    everything = client.get("/api/sessions").json()
    assert everything["total"] == 7
    assert {s["provider"] for s in everything["sessions"]} == {"claude", "codex", "gemini"}
    codex = client.get("/api/sessions?provider=codex").json()
    assert codex["total"] == 2 and {s["provider"] for s in codex["sessions"]} == {"codex"}
    assert client.get("/api/sessions?provider=claude,gemini").json()["total"] == 5

    hits = client.get("/api/search?q=redirect&provider=codex").json()["hits"]
    assert hits and {h["provider"] for h in hits} == {"codex"}


def test_any_providers_session_opens_in_the_viewer(client):
    for session in client.get("/api/sessions").json()["sessions"]:
        response = client.get(f"/api/sessions/{session['session_id']}/messages")
        assert response.status_code == 200, session["session_id"]
        assert response.json()["meta"]["provider"] == session["provider"]


def test_usage_filters_by_provider_and_reports_per_provider(client):
    body = client.get("/api/usage?provider=codex").json()
    assert body["totals"]["sessions"] == 2
    assert [p["provider"] for p in body["providers"]] == ["codex"]
    everything = client.get("/api/usage").json()
    assert {p["provider"] for p in everything["providers"]} == {"claude", "codex", "gemini"}


def test_the_comparison_aligns_every_providers_series(client):
    body = client.get("/api/usage/compare?granularity=day").json()
    assert {p["provider"] for p in body["providers"]} == {"claude", "codex", "gemini"}
    lengths = {len(rows) for rows in body["timeseries"].values()}
    assert lengths == {len(body["periods"])}, "every series covers every period"
    codex = next(p for p in body["providers"] if p["provider"] == "codex")
    assert codex["sessions"] == 2 and codex["cost"] > 0
    assert codex["avg_messages"] > 0
    assert "gpt-5-codex" in codex["models"]
    assert set(body["heatmaps"]) == {"claude", "codex", "gemini"}


def test_the_csv_names_each_rows_provider(client):
    text = client.get("/api/usage/csv?provider=gemini").text
    header, *rows = text.strip().splitlines()
    assert header.split(",")[1] == "provider"
    assert rows and all(",gemini," in row for row in rows)


def test_a_provider_can_be_switched_off_and_on(client):
    off = client.patch("/api/providers/codex", json={"enabled": False}).json()
    codex = next(p for p in off["providers"] if p["id"] == "codex")
    assert codex["enabled"] is False and codex["indexed"] is False
    assert client.get("/api/sessions?provider=codex").json()["total"] == 0
    assert json.loads(client.get("/api/config").text)["providers"]["codex"]["enabled"] is False

    client.patch("/api/providers/codex", json={"enabled": True})
    assert client.get("/api/sessions?provider=codex").json()["total"] == 2


def test_a_custom_path_is_applied(client, tmp_path):
    body = client.patch("/api/providers/gemini", json={"path": str(tmp_path / "empty")}).json()
    gemini = next(p for p in body["providers"] if p["id"] == "gemini")
    assert gemini["detection"]["data_found"] is False
    assert gemini["settings"]["path"] == str(tmp_path / "empty")
    assert client.get("/api/sessions?provider=gemini").json()["total"] == 0


def test_pricing_overrides_change_the_estimate(client):
    before = client.get("/api/usage?provider=codex").json()["totals"]["estimated_cost"]
    client.patch("/api/providers/codex", json={"pricing": {
        "gpt-5-codex": {"input": 0, "output": 0, "cache_read": 0}}})
    after = client.get("/api/usage?provider=codex").json()["totals"]["estimated_cost"]
    assert before > 0 and after < before


@pytest.mark.parametrize("patch", [
    {"colour": "red"},
    {"enabled": "yes"},
    {"path": 3},
    {"pricing": {"m": {"input": -1}}},
    {"pricing": ["not", "a", "table"]},
])
def test_bad_provider_settings_are_refused(client, patch):
    assert client.patch("/api/providers/codex", json=patch).status_code == 400


def test_an_unknown_provider_is_a_404(client):
    assert client.patch("/api/providers/nope", json={"enabled": False}).status_code == 404


def test_a_custom_provider_can_be_added_used_and_removed(client):
    added = client.post("/api/providers/custom", json=SPEC)
    assert added.status_code == 200
    assert "mytool" in _ids(added.json())
    sessions = client.get("/api/sessions?provider=mytool").json()
    assert sessions["total"] == 1
    session_id = sessions["sessions"][0]["session_id"]
    assert client.get(f"/api/sessions/{session_id}/messages").status_code == 200

    replaced = client.post("/api/providers/custom", json={**SPEC, "name": "Renamed"})
    assert next(p for p in replaced.json()["providers"] if p["id"] == "mytool")["name"] == "Renamed"

    removed = client.delete("/api/providers/custom/mytool")
    assert "mytool" not in _ids(removed.json())
    assert client.get("/api/sessions?provider=mytool").json()["total"] == 0


def test_an_invalid_or_clashing_custom_provider_is_refused(client):
    bad = client.post("/api/providers/custom", json={**SPEC, "glob": "../x/*.jsonl"})
    assert bad.status_code == 400 and "'..'" in bad.json()["detail"]
    clash = client.post("/api/providers/custom", json={**SPEC, "id": "codex"})
    assert clash.status_code == 400 and "already used" in clash.json()["detail"]


def test_removing_a_builtin_or_unknown_custom_provider_is_refused(client):
    assert client.delete("/api/providers/custom/claude").status_code == 404
    assert client.delete("/api/providers/custom/ghost").status_code == 404


def test_deleting_is_refused_for_read_only_providers(client):
    codex = client.get("/api/sessions?provider=codex").json()["sessions"][0]
    response = client.post("/api/sessions/delete", json={"session_ids": [codex["session_id"]]})
    assert response.status_code == 400
    assert "not supported" in response.json()["detail"]
    assert Path(codex["path"]).is_file(), "nothing may move"


def test_resume_commands_come_from_each_provider(client):
    codex = client.get("/api/sessions?provider=codex").json()["sessions"][0]
    body = client.get(f"/api/sessions/{codex['session_id']}/resume-command").json()
    assert body["command"] == f"codex resume {codex['session_id']}"

    gemini = client.get("/api/sessions?provider=gemini").json()["sessions"][0]
    refused = client.get(f"/api/sessions/{gemini['session_id']}/resume-command")
    assert refused.status_code == 400
    assert "cannot reopen" in refused.json()["detail"]


def test_rescan_reports_providers(client):
    body = client.post("/api/providers/rescan").json()
    assert {"claude", "codex", "gemini"} <= set(_ids(body))
