"""Unit tests for cost estimation and usage aggregation."""

from __future__ import annotations

from datetime import date

import pytest

from agentboard.config import DEFAULT_PRICING, cost_of, normalise_model, price_for_model
from agentboard.parser import scan_session
from agentboard.usage import (
    aggregate,
    by_model,
    by_project,
    disk_usage,
    heatmap,
    kpi_cards,
    session_cost,
    stats_csv,
    timeseries,
    tool_stats,
)

PRICING = DEFAULT_PRICING


# ------------------------------------------------------------------ pricing

def test_normalise_model_strips_the_context_suffix():
    assert normalise_model("claude-opus-5[1m]") == "claude-opus-5"
    assert normalise_model("claude-opus-5") == "claude-opus-5"
    assert normalise_model(None) == "unknown"


def test_price_lookup_falls_back_progressively():
    assert price_for_model("claude-opus-5", PRICING)["input"] == 5.00
    # Suffixed ids resolve through normalisation.
    assert price_for_model("claude-opus-5[1m]", PRICING)["output"] == 25.00
    # Dated ids fall back to the undated row.
    assert price_for_model("claude-haiku-4-5-20251001", PRICING)["input"] == 1.00
    # Anything unknown lands on the default row.
    assert price_for_model("some-future-model", PRICING) == PRICING["default"]


def test_cost_charges_cache_writes_by_ttl():
    """The 1-hour tier costs 2x input, the 5-minute tier 1.25x."""
    five_minute = cost_of("claude-opus-5", cache_write_5m_tokens=1_000_000, pricing=PRICING)
    one_hour = cost_of("claude-opus-5", cache_write_1h_tokens=1_000_000, pricing=PRICING)
    assert five_minute == pytest.approx(6.25)
    assert one_hour == pytest.approx(10.00)
    assert one_hour > five_minute


def test_cost_without_a_ttl_split_uses_the_cheaper_rate():
    assert cost_of("claude-opus-5", cache_write_tokens=1_000_000, pricing=PRICING) == pytest.approx(6.25)


def test_cost_reproduces_a_real_cost_state_record():
    """Values lifted from a `cost-state` entry on the reference install.

    Claude Code recorded $2.4256665 for this usage; the estimate must land
    within a cent of that.
    """
    estimate = cost_of(
        "claude-opus-5[1m]",
        input_tokens=174,
        output_tokens=15_712,
        cache_read_tokens=2_415_017,
        cache_write_5m_tokens=0,
        cache_write_1h_tokens=82_240,
        pricing=PRICING,
    )
    assert estimate == pytest.approx(2.4256665, abs=0.01)


def test_synthetic_model_is_free():
    assert cost_of("<synthetic>", input_tokens=10**9, pricing=PRICING) == 0.0


def test_legacy_pricing_rows_without_a_ttl_split_still_work():
    legacy = {"default": {"input": 3.0, "output": 15.0, "cache_write": 4.0, "cache_read": 0.3}}
    assert cost_of("x", cache_write_5m_tokens=1_000_000, pricing=legacy) == pytest.approx(4.0)
    assert cost_of("x", cache_write_1h_tokens=1_000_000, pricing=legacy) == pytest.approx(6.4)


# -------------------------------------------------------------- aggregation

@pytest.fixture
def metas(fixtures):
    """The two fixture transcripts, scanned."""
    return [scan_session(fixtures / "basic.jsonl"), scan_session(fixtures / "messy.jsonl")]


def test_session_cost_sums_the_per_model_rows(fixtures):
    meta = scan_session(fixtures / "basic.jsonl")
    expected = cost_of(
        "claude-opus-5",
        input_tokens=15,
        output_tokens=150,
        cache_read_tokens=12_000,
        cache_write_5m_tokens=800,
        cache_write_1h_tokens=2_000,
        pricing=PRICING,
    )
    assert session_cost(meta, PRICING) == pytest.approx(expected)
    # And it lands close to what Claude Code itself recorded for the session.
    assert session_cost(meta, PRICING) == pytest.approx(meta.reported_cost_usd, abs=0.01)


def test_timeseries_buckets_by_day_week_and_month(metas):
    days = timeseries(metas, "day", PRICING)
    assert [d["period"] for d in days] == ["2026-03-02", "2026-03-03"]
    assert days[0]["output"] == 150
    assert days[0]["messages"] == 2
    assert days[0]["tool_calls"] == 1
    assert days[0]["sessions"] == 1

    weeks = timeseries(metas, "week", PRICING)
    assert len(weeks) == 1                       # both days fall in ISO week 10
    assert weeks[0]["period"] == "2026-W10"
    assert weeks[0]["sessions"] == 2

    months = timeseries(metas, "month", PRICING)
    assert [m["period"] for m in months] == ["2026-03"]


def test_timeseries_cost_matches_the_session_total(metas):
    total = sum(point["cost"] for point in timeseries(metas, "day", PRICING))
    assert total == pytest.approx(sum(session_cost(m, PRICING) for m in metas), abs=1e-6)


def test_by_project_and_by_model_rollups(metas):
    projects = by_project(metas, PRICING)
    assert len(projects) == 1
    assert projects[0]["project"] == "/home/tester/demo"
    assert projects[0]["sessions"] == 2
    assert projects[0]["tool_calls"] == 1

    models = by_model(metas, PRICING)
    names = [m["model"] for m in models]
    assert "claude-opus-5" in names and "<synthetic>" in names
    opus = next(m for m in models if m["model"] == "claude-opus-5")
    assert opus["total_tokens"] == 15 + 150 + 2800 + 12000
    assert opus["cost"] > 0
    synthetic = next(m for m in models if m["model"] == "<synthetic>")
    assert synthetic["cost"] == 0.0


def test_tool_stats_counts_and_groups_mcp_servers():
    from agentboard.parser import SessionMeta

    meta = SessionMeta(
        session_id="s", path="p", project_dir="d", project_path="/proj",
        tools={"Bash": 12, "Read": 3, "mcp__gmail__send_message": 2, "mcp__gmail__reply": 1},
    )
    stats = tool_stats([meta])
    assert stats["tools"][0] == {"name": "Bash", "count": 12}
    assert stats["total_calls"] == 18
    assert stats["distinct_tools"] == 4
    assert stats["mcp_servers"] == [{"name": "gmail", "count": 3}]
    assert stats["by_project"]["/proj"][0]["name"] == "Bash"


def test_heatmap_is_a_seven_by_twentyfour_grid(metas):
    grid = heatmap(metas)
    assert len(grid["grid"]) == 7
    assert all(len(row) == 24 for row in grid["grid"])
    assert grid["grid"][0][9] == 8            # Monday 09:00 UTC, basic.jsonl
    assert grid["total"] == sum(sum(row) for row in grid["grid"])
    assert grid["peak"] == max(max(row) for row in grid["grid"])


def test_heatmap_ignores_malformed_slots():
    from agentboard.parser import SessionMeta

    meta = SessionMeta(
        session_id="s", path="p", project_dir="d",
        heatmap={"0-9": 2, "bad": 5, "9-99": 7, "": 1},
    )
    grid = heatmap([meta])
    assert grid["total"] == 2


def test_kpi_cards_compare_against_the_previous_period(metas):
    cards = kpi_cards(metas, PRICING, today=date(2026, 3, 3))
    labels = [c["label"] for c in cards]
    assert labels == ["Today", "Last 7 days", "Last 30 days", "All time"]

    today = cards[0]
    assert today["messages"] == 1             # only messy.jsonl is on 03-03
    # The previous day held basic.jsonl's two assistant responses.
    assert today["previous"]["messages"] == 2
    assert today["change_percent"]["messages"] == pytest.approx(-50.0)

    all_time = cards[3]
    assert all_time["sessions"] == 2          # counts sessions, not active days
    assert "change_percent" not in all_time


def test_kpi_change_is_none_when_the_previous_period_was_empty(metas):
    cards = kpi_cards(metas, PRICING, today=date(2026, 6, 1))
    assert cards[0]["cost"] == 0
    assert cards[0]["change_percent"]["cost"] is None


def test_disk_usage_reports_largest_sessions_and_growth(metas):
    disk = disk_usage(metas, claude_home_size=999)
    assert disk["session_count"] == 2
    assert disk["claude_home_bytes"] == 999
    assert disk["transcript_bytes"] == sum(m.file_size for m in metas)
    sizes = [entry["file_size"] for entry in disk["largest"]]
    assert sizes == sorted(sizes, reverse=True)
    cumulative = [g["cumulative_bytes"] for g in disk["growth"]]
    assert cumulative == sorted(cumulative)


def test_aggregate_bundles_every_section(metas):
    result = aggregate(metas, "day", PRICING)
    assert set(result) >= {
        "kpis", "timeseries", "projects", "models", "tools", "heatmap", "disk", "totals"
    }
    totals = result["totals"]
    assert totals["sessions"] == 2
    assert totals["estimated_cost"] == pytest.approx(
        sum(session_cost(m, PRICING) for m in metas), abs=1e-4
    )
    assert totals["sessions_with_reported_cost"] == 1


def test_aggregate_on_an_empty_corpus_does_not_crash():
    result = aggregate([], "day", PRICING)
    assert result["totals"]["sessions"] == 0
    assert result["timeseries"] == []
    assert result["heatmap"]["total"] == 0
    assert all(card["cost"] == 0 for card in result["kpis"])


def test_stats_csv_has_a_header_and_one_row_per_session(metas):
    import csv
    import io

    rows = list(csv.reader(io.StringIO(stats_csv(metas, PRICING))))
    assert rows[0][0] == "session_id"
    assert len(rows) == 3
    header = rows[0]
    row = dict(zip(header, rows[1]))
    assert row["project_path"] == "/home/tester/demo"
    assert float(row["estimated_cost_usd"]) >= 0
