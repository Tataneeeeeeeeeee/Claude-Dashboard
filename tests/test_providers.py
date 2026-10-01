"""The provider adapter interface, the registry, and the Claude adapter.

The Claude adapter wraps the original reader unchanged, so these tests pin
that the wrapping is faithful: same sessions, same parse, same search hits
as before the dashboard became provider-agnostic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agentboard.indexer import SessionIndex, project_key
from agentboard.model import SessionMeta
from agentboard.parser import parse_conversation, scan_session
from agentboard.providers import (
    Capabilities,
    ProviderAdapter,
    Registry,
    merge_pricing,
)
from agentboard.providers.claude import ClaudeAdapter
from agentboard.usage import session_cost


# ------------------------------------------------------------ a toy adapter

class NotesAdapter(ProviderAdapter):
    """The smallest useful adapter: one JSON file per session.

    ``{"cwd": ..., "model": ..., "turns": [{"role", "text", "at", "in", "out"}]}``
    """

    id = "notes"
    name = "Notes"
    monogram = "N"
    color = "#3b82f6"
    file_suffixes = (".json",)
    default_pricing = {"note-1": {"input": 1.0, "output": 2.0},
                       "default": {"input": 0.5, "output": 0.5}}
    capabilities = Capabilities(cache_tokens=False, tool_calls=False)

    def __init__(self, folder: Path, settings: Dict[str, Any] | None = None) -> None:
        super().__init__(settings)
        self.folder = folder

    def default_home(self) -> Path:
        return self.folder

    def session_files(self) -> List[Path]:
        return sorted(self.root.glob("*.json")) if self.root.is_dir() else []

    def _load(self, path: Path) -> Dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def scan(self, path: Path) -> SessionMeta:
        data = self._load(path)
        meta = SessionMeta(session_id=path.stem, path=str(path), project_dir="notes",
                           provider=self.id, project_path=data.get("cwd", ""))
        model = data.get("model", "note-1")
        row = meta.models.setdefault(model, {"input": 0, "output": 0, "cache_write": 0,
                                             "cache_write_5m": 0, "cache_write_1h": 0,
                                             "cache_read": 0, "messages": 0})
        meta.tokens = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        for turn in data["turns"]:
            meta.message_count += 1
            stamp = turn["at"]
            meta.first_timestamp = meta.first_timestamp or stamp
            meta.last_timestamp = stamp
            if turn["role"] == "user":
                meta.user_messages += 1
                meta.title = meta.title or turn["text"]
            else:
                meta.assistant_messages += 1
                row["messages"] += 1
                for key, source in (("input", "in"), ("output", "out")):
                    row[key] += turn.get(source, 0)
                    meta.tokens[key] += turn.get(source, 0)
                day = meta.daily.setdefault(stamp[:10], {"input": 0, "output": 0, "cache_write": 0,
                                                          "cache_write_5m": 0, "cache_write_1h": 0,
                                                          "cache_read": 0, "messages": 0,
                                                          "tool_calls": 0})
                day["messages"] += 1
                day["input"] += turn.get("in", 0)
                day["output"] += turn.get("out", 0)
        return meta

    def parse(self, path: Path, tool_output_limit: int = 20_000,
              include_attachments: bool = True) -> Dict[str, Any]:
        data = self._load(path)
        messages = [
            {"index": i, "line": i, "uuid": None, "parent_uuid": None,
             "kind": t["role"], "role": t["role"], "timestamp": t["at"], "model": None,
             "is_sidechain": False, "is_meta": False, "is_error": False,
             "blocks": [{"type": "text", "text": t["text"]}], "usage": None,
             "subtype": None, "extra": {}}
            for i, t in enumerate(data["turns"])
        ]
        return {"info": {"session_id": path.stem, "title": messages[0]["blocks"][0]["text"]},
                "messages": messages, "errors": []}


@pytest.fixture
def notes_dir(tmp_path) -> Path:
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "n1.json").write_text(json.dumps({
        "cwd": "/home/tester/demo",
        "model": "note-1",
        "turns": [
            {"role": "user", "text": "rename the parser module", "at": "2026-09-01T10:00:00Z"},
            {"role": "assistant", "text": "Done, see agentboard/providers.", "at": "2026-09-01T10:00:05Z",
             "in": 1_000_000, "out": 1_000_000},
        ],
    }), encoding="utf-8")
    return folder


# ------------------------------------------------------------ the interface

def test_the_base_class_cannot_be_instantiated_without_the_contract():
    with pytest.raises(TypeError):
        ProviderAdapter()  # type: ignore[abstract]


def test_capabilities_serialise_every_flag():
    flags = Capabilities().to_dict()
    for name in ("usage", "cost", "cache_tokens", "tool_calls", "live", "search",
                 "resume", "delete", "reported_cost", "instructions", "extras"):
        assert name in flags
    assert flags["delete"] is False, "new providers must be read-only by default"
    assert flags["resume"] is False


def test_a_configured_path_overrides_the_default_home(notes_dir, tmp_path):
    adapter = NotesAdapter(notes_dir, {"path": str(tmp_path / "elsewhere")})
    assert adapter.root == tmp_path / "elsewhere"
    assert adapter.session_files() == []


def test_detection_reports_data_and_explains_itself(notes_dir, tmp_path):
    found = NotesAdapter(notes_dir).detect()
    assert found.data_found and found.detected
    assert str(notes_dir) in found.reason

    missing = NotesAdapter(tmp_path / "nope").detect()
    assert not missing.detected
    assert "not installed" in missing.reason


def test_the_convenience_entry_points_work_for_any_adapter(notes_dir):
    adapter = NotesAdapter(notes_dir)
    sessions = adapter.list_sessions()
    assert [s.session_id for s in sessions] == ["n1"]
    assert adapter.get_session("n1")["messages"][1]["role"] == "assistant"
    assert adapter.get_session("missing") is None
    usage = adapter.get_usage(sessions, config={"pricing": {}})
    assert usage["totals"]["sessions"] == 1
    # 1M input at $1 plus 1M output at $2, from the adapter's own table.
    assert usage["totals"]["estimated_cost"] == pytest.approx(3.0)


def test_the_default_search_works_from_parse_alone(notes_dir):
    entries = list(NotesAdapter(notes_dir).search_entries(notes_dir / "n1.json"))
    assert [e.role for e in entries] == ["user", "assistant"]
    assert "parser" in entries[0].text


def test_owns_matches_suffix_and_location(notes_dir, tmp_path):
    adapter = NotesAdapter(notes_dir)
    assert adapter.owns(notes_dir / "n1.json")
    assert not adapter.owns(notes_dir / "n1.jsonl")
    assert not adapter.owns(tmp_path / "n1.json")


def test_a_provider_without_resume_returns_no_command(notes_dir):
    assert NotesAdapter(notes_dir).resume_command("n1") is None


def test_a_configured_resume_command_is_always_honoured(notes_dir):
    adapter = NotesAdapter(notes_dir, {"resume_command": "notes open {session_id}"})
    assert adapter.resume_command("n1") == "notes open n1"


def test_describe_carries_what_the_ui_needs(notes_dir):
    described = NotesAdapter(notes_dir).describe()
    for key in ("id", "name", "monogram", "color", "enabled", "capabilities", "detection", "settings"):
        assert key in described
    assert described["detection"]["detected"] is True


def test_pricing_layers_merge_in_order():
    adapter_rows = {"m": {"input": 1}, "default": {"input": 9}}
    global_rows = {"m": {"input": 2}, "default": {"input": 3}, "other": {"input": 4}}
    overrides = {"other": {"input": 5}}
    table = merge_pricing(adapter_rows, global_rows, overrides)
    assert table["m"] == {"input": 2}, "the user's global table beats built-in rows"
    assert table["default"] == {"input": 9}, "a provider's own default beats the global one"
    assert table["other"] == {"input": 5}, "provider overrides win over everything"


def test_a_session_is_priced_with_its_own_providers_table(notes_dir):
    meta = NotesAdapter(notes_dir).scan(notes_dir / "n1.json")
    tables = {"notes": {"note-1": {"input": 10.0, "output": 0.0}}}
    assert session_cost(meta, lambda provider: tables[provider]) == pytest.approx(10.0)


# --------------------------------------------------------- the Claude adapter

def test_the_claude_adapter_follows_the_configured_home(fake_claude_home):
    adapter = ClaudeAdapter()
    assert adapter.root == fake_claude_home / "projects"
    assert adapter.detect().data_found


def test_the_claude_adapter_lists_exactly_the_transcripts(fake_claude_home):
    files = ClaudeAdapter().session_files()
    assert [f.name for f in files] == [
        "11111111-2222-3333-4444-555555555555.jsonl",
        "99999999-8888-7777-6666-555555555555.jsonl",
    ]


def test_the_claude_adapter_skips_symlinked_transcripts(fake_claude_home, tmp_path):
    project = fake_claude_home / "projects" / "-home-tester-demo"
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    (project / "linked.jsonl").symlink_to(outside)
    assert "linked.jsonl" not in [f.name for f in ClaudeAdapter().session_files()]


def test_the_claude_adapter_scans_and_parses_exactly_as_before(fake_claude_home):
    adapter = ClaudeAdapter()
    for path in adapter.session_files():
        assert adapter.scan(path).to_dict() == scan_session(path).to_dict()
        assert adapter.parse(path) == parse_conversation(path)
        assert adapter.scan(path).provider == "claude"


def test_the_claude_adapter_search_entries_point_at_real_lines(fake_claude_home):
    adapter = ClaudeAdapter()
    path = adapter.session_files()[0]
    lines = path.read_text(encoding="utf-8").splitlines()
    entries = list(adapter.search_entries(path))
    assert entries, "the basic fixture has searchable messages"
    for entry in entries:
        assert entry.role in {"user", "assistant"}
        source = json.loads(lines[entry.line - 1])
        assert source.get("uuid") == entry.uuid


def test_the_claude_adapter_owns_only_its_transcripts(fake_claude_home, tmp_path):
    adapter = ClaudeAdapter()
    inside = fake_claude_home / "projects" / "-home-tester-demo" / "x.jsonl"
    assert adapter.owns(inside)
    assert not adapter.owns(fake_claude_home / "history.jsonl")
    assert not adapter.owns(tmp_path / "x.jsonl")


def test_the_claude_resume_command_honours_both_settings(fake_claude_home):
    assert ClaudeAdapter().resume_command("abc") == "claude --resume abc"
    legacy = {"terminal": {"resume_command": "claude -r {session_id}"}}
    assert ClaudeAdapter().resume_command("abc", legacy) == "claude -r abc"
    own = ClaudeAdapter({"resume_command": "ccr {session_id}"})
    assert own.resume_command("abc", legacy) == "ccr abc"


def test_claude_pricing_is_the_global_table_unchanged(fake_claude_home):
    table = {"claude-opus-5": {"input": 1}, "default": {"input": 7}}
    assert ClaudeAdapter().pricing_table({"pricing": table}) == table


def test_the_claude_capabilities_keep_every_existing_feature():
    caps = ClaudeAdapter.capabilities
    assert caps.delete and caps.resume and caps.reported_cost
    assert caps.instructions and caps.extras and caps.cache_tokens


# ------------------------------------------------------------- the registry

def test_the_registry_builds_every_builtin_with_its_settings(fake_claude_home):
    registry = Registry({"providers": {"claude": {"enabled": False}}, "pricing": {}})
    claude = registry.get("claude")
    assert claude is not None and claude.enabled is False
    assert claude not in registry.enabled()


def test_the_registry_enables_providers_by_default(fake_claude_home):
    registry = Registry({"pricing": {}})
    assert registry.get("claude") in registry.enabled()


# ------------------------------------------------- indexing several providers

@pytest.fixture
def mixed_index(fake_claude_home, notes_dir) -> SessionIndex:
    index = SessionIndex(adapters=[ClaudeAdapter(), NotesAdapter(notes_dir)])
    index.build(force=True)
    return index


def test_the_index_holds_every_providers_sessions(mixed_index):
    providers = sorted(m.provider for m in mixed_index.sessions)
    assert providers == ["claude", "claude", "notes"]
    stats = mixed_index.stats()
    assert stats["providers"]["notes"]["sessions"] == 1
    assert stats["providers"]["claude"]["sessions"] == 2


def test_sessions_can_be_filtered_by_provider(mixed_index):
    assert [m.provider for m in mixed_index.filter(provider="notes")] == ["notes"]
    assert len(mixed_index.filter(provider="claude")) == 2
    assert len(mixed_index.filter(provider="all")) == 3
    assert len(mixed_index.filter(provider="claude,notes")) == 3


def test_one_directory_is_one_project_across_providers(mixed_index):
    projects = {p.key: p for p in mixed_index.projects}
    demo = projects["/home/tester/demo"]
    assert demo.providers == {"claude": 2, "notes": 1}
    assert demo.session_count == 3
    assert len(mixed_index.filter(project="/home/tester/demo")) == 3


def test_search_covers_every_provider_and_labels_hits(mixed_index):
    result = mixed_index.search("parser")
    assert {hit["provider"] for hit in result["hits"]} >= {"notes"}
    only_notes = mixed_index.search("parser", provider="notes")
    assert {hit["provider"] for hit in only_notes["hits"]} == {"notes"}


def test_a_live_change_is_routed_to_the_owning_adapter(mixed_index, notes_dir):
    path = notes_dir / "n1.json"
    data = json.loads(path.read_text())
    data["turns"].append({"role": "user", "text": "one more", "at": "2026-09-02T09:00:00Z"})
    path.write_text(json.dumps(data), encoding="utf-8")
    refreshed = mixed_index.refresh_path(path)
    assert refreshed is not None and refreshed.provider == "notes"
    assert refreshed.message_count == 3


def test_a_cached_entry_from_another_provider_is_rescanned(fake_claude_home, notes_dir):
    """Two adapters claiming one path must not share a cache entry."""
    SessionIndex(adapters=[NotesAdapter(notes_dir)]).build(force=True)

    class Renamed(NotesAdapter):
        id = "renamed"

    index = SessionIndex(adapters=[Renamed(notes_dir)])
    index.build()
    assert index.progress.scanned == 1, "the cached 'notes' entry must not be reused"
    assert index.sessions[0].provider == "renamed"


def test_project_paths_can_be_recovered_by_an_adapter(fake_claude_home, notes_dir):
    class Pathless(NotesAdapter):
        id = "pathless"

        def scan(self, path):
            meta = super().scan(path)
            meta.project_path = ""
            meta.project_dir = "hash-of-demo"
            return meta

        def guess_project_path(self, meta, known_paths):
            return "/home/tester/demo" if "/home/tester/demo" in known_paths else ""

    index = SessionIndex(adapters=[ClaudeAdapter(), Pathless(notes_dir)])
    index.build(force=True)
    recovered = index.filter(provider="pathless")[0]
    assert recovered.project_path == "/home/tester/demo"
    assert project_key(recovered) == "/home/tester/demo"


def test_the_claude_scan_records_the_files_a_session_touched(fixtures):
    meta = scan_session(fixtures / "basic.jsonl")
    assert "/srv/router.py" in meta.files_touched
    assert len(meta.files_touched) == len(set(meta.files_touched))
