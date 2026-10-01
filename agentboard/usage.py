"""Token, cost and tool aggregation over the session index.

Everything here reads the pre-computed buckets on
:class:`~agentboard.parser.SessionMeta` - daily token totals, per-model
totals, tool counts and the day/hour heatmap - so a full dashboard refresh
costs a dictionary walk rather than another pass over 183 MB of JSONL.

Costs are **local estimates** from the user-editable pricing tables, and
they are a *lower bound*: tools bill for work that never appears in a
transcript (Claude Code's background Haiku calls, retried requests).  Where a
session recorded its own cost, that figure is reported alongside.

Each provider prices its own models: wherever a ``pricing`` argument is
accepted it may be one flat table applied to every session, a callable
mapping a provider id to its table, or ``None`` for the configured tables.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Sequence, Union

from .config import cost_of, load_config, normalise_model
from .parser import SessionMeta

__all__ = [
    "pricing_resolver",
    "session_cost",
    "aggregate",
    "kpi_cards",
    "timeseries",
    "by_project",
    "by_model",
    "tool_stats",
    "heatmap",
    "disk_usage",
    "stats_csv",
]

_TOKEN_KEYS = ("input", "output", "cache_write", "cache_read")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


Resolver = Callable[[str], Dict[str, Any]]
PricingSource = Union[Dict[str, Any], Resolver, None]


def pricing_resolver(config: Dict[str, Any] | None = None) -> Resolver:
    """Map a provider id to its effective price table, memoised.

    Unknown providers (a session cached from an adapter since removed) fall
    back to the global table.
    """
    settings = config if config is not None else load_config()
    from .providers.registry import get_registry

    registry = get_registry()
    tables: Dict[str, Dict[str, Any]] = {}

    def resolve(provider: str) -> Dict[str, Any]:
        if provider not in tables:
            adapter = registry.get(provider)
            tables[provider] = (
                adapter.pricing_table(settings) if adapter is not None else settings["pricing"]
            )
        return tables[provider]

    return resolve


def _pricing(pricing: PricingSource) -> Resolver:
    """Turn any accepted ``pricing`` argument into a resolver."""
    if pricing is None:
        return pricing_resolver()
    if callable(pricing):
        return pricing
    return lambda _provider: pricing


def session_cost(meta: SessionMeta, pricing: PricingSource = None) -> float:
    """Estimated cost of one session, summed over its per-model totals."""
    table = _pricing(pricing)(meta.provider)
    total = 0.0
    for model, row in meta.models.items():
        total += cost_of(
            model,
            input_tokens=row.get("input", 0),
            output_tokens=row.get("output", 0),
            cache_write_tokens=row.get("cache_write", 0),
            cache_read_tokens=row.get("cache_read", 0),
            pricing=table,
            cache_write_5m_tokens=row.get("cache_write_5m"),
            cache_write_1h_tokens=row.get("cache_write_1h"),
        )
    return total


def _empty_bucket() -> Dict[str, float]:
    """A zeroed accumulator for one time or category bucket."""
    return {
        "input": 0,
        "output": 0,
        "cache_write": 0,
        "cache_write_5m": 0,
        "cache_write_1h": 0,
        "cache_read": 0,
        "total_tokens": 0,
        "messages": 0,
        "tool_calls": 0,
        "sessions": 0,
        "cost": 0.0,
    }


def _add_model_row(bucket: Dict[str, float], model: str, row: Dict[str, int], table: Dict[str, Any]) -> None:
    """Accumulate one per-model token row into *bucket*, including its cost."""
    for key in ("input", "output", "cache_write", "cache_write_5m", "cache_write_1h", "cache_read"):
        bucket[key] += row.get(key, 0)
    bucket["total_tokens"] += sum(row.get(key, 0) for key in _TOKEN_KEYS)
    bucket["messages"] += row.get("messages", 0)
    bucket["cost"] += cost_of(
        model,
        input_tokens=row.get("input", 0),
        output_tokens=row.get("output", 0),
        cache_write_tokens=row.get("cache_write", 0),
        cache_read_tokens=row.get("cache_read", 0),
        pricing=table,
        cache_write_5m_tokens=row.get("cache_write_5m"),
        cache_write_1h_tokens=row.get("cache_write_1h"),
    )


def _session_day_model_cost(meta: SessionMeta, table: Dict[str, Any]) -> Dict[str, float]:
    """Cost per day for one session.

    ``SessionMeta.daily`` is not split by model, so the session's overall
    model mix is used to weight each day.  With a single model in play - the
    common case - this is exact.
    """
    if not meta.daily:
        return {}
    weights: Dict[str, float] = {}
    grand = 0
    for model, row in meta.models.items():
        subtotal = sum(row.get(key, 0) for key in _TOKEN_KEYS)
        weights[model] = subtotal
        grand += subtotal
    if grand <= 0:
        return {day: 0.0 for day in meta.daily}
    out: Dict[str, float] = {}
    for day, row in meta.daily.items():
        total = 0.0
        for model, weight in weights.items():
            share = weight / grand
            total += cost_of(
                model,
                input_tokens=row.get("input", 0) * share,
                output_tokens=row.get("output", 0) * share,
                cache_write_tokens=row.get("cache_write", 0) * share,
                cache_read_tokens=row.get("cache_read", 0) * share,
                pricing=table,
                cache_write_5m_tokens=row.get("cache_write_5m", 0) * share,
                cache_write_1h_tokens=row.get("cache_write_1h", 0) * share,
            )
        out[day] = total
    return out


def timeseries(
    metas: Sequence[SessionMeta],
    granularity: str = "day",
    pricing: PricingSource = None,
) -> List[Dict[str, Any]]:
    """Tokens, cost, messages and sessions bucketed by day, week or month."""
    resolve = _pricing(pricing)
    buckets: Dict[str, Dict[str, float]] = defaultdict(_empty_bucket)
    session_days: Dict[str, set] = defaultdict(set)

    for meta in metas:
        day_costs = _session_day_model_cost(meta, resolve(meta.provider))
        for day, row in meta.daily.items():
            key = _bucket_key(day, granularity)
            if key is None:
                continue
            bucket = buckets[key]
            for token_key in ("input", "output", "cache_write", "cache_write_5m",
                              "cache_write_1h", "cache_read"):
                bucket[token_key] += row.get(token_key, 0)
            bucket["total_tokens"] += sum(row.get(k, 0) for k in _TOKEN_KEYS)
            bucket["messages"] += row.get("messages", 0)
            bucket["tool_calls"] += row.get("tool_calls", 0)
            bucket["cost"] += day_costs.get(day, 0.0)
            session_days[key].add(meta.session_id)

    out = []
    for key in sorted(buckets):
        bucket = buckets[key]
        bucket["sessions"] = len(session_days[key])
        bucket["cost"] = round(bucket["cost"], 6)
        out.append({"period": key, **bucket})
    return out


def _bucket_key(day: str, granularity: str) -> str | None:
    """Map an ``YYYY-MM-DD`` string to its day, ISO week or month bucket."""
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return None
    if granularity == "month":
        return parsed.strftime("%Y-%m")
    if granularity == "week":
        year, week, _ = parsed.isocalendar()
        return f"{year}-W{week:02d}"
    return parsed.isoformat()


def by_project(
    metas: Sequence[SessionMeta],
    pricing: PricingSource = None,
) -> List[Dict[str, Any]]:
    """Per-project rollup of tokens, cost, messages and disk footprint."""
    resolve = _pricing(pricing)
    buckets: Dict[str, Dict[str, Any]] = {}
    for meta in metas:
        key = meta.project_path or meta.project_dir
        bucket = buckets.setdefault(key, {**_empty_bucket(), "project": key,
                                          "project_dir": meta.project_dir, "file_size": 0,
                                          "providers": {}})
        bucket["providers"][meta.provider] = bucket["providers"].get(meta.provider, 0) + 1
        for model, row in meta.models.items():
            _add_model_row(bucket, model, row, resolve(meta.provider))
        bucket["tool_calls"] += meta.tool_calls
        bucket["sessions"] += 1
        bucket["file_size"] += meta.file_size
    out = sorted(buckets.values(), key=lambda b: b["cost"], reverse=True)
    for bucket in out:
        bucket["cost"] = round(bucket["cost"], 6)
    return out


def by_model(
    metas: Sequence[SessionMeta],
    pricing: PricingSource = None,
) -> List[Dict[str, Any]]:
    """Per-model rollup, keyed on the provider and the normalised model id."""
    resolve = _pricing(pricing)
    buckets: Dict[tuple, Dict[str, Any]] = {}
    for meta in metas:
        for model, row in meta.models.items():
            name = normalise_model(model)
            bucket = buckets.setdefault(
                (meta.provider, name), {**_empty_bucket(), "model": name, "provider": meta.provider}
            )
            _add_model_row(bucket, model, row, resolve(meta.provider))
            bucket["sessions"] += 1
    out = sorted(buckets.values(), key=lambda b: b["total_tokens"], reverse=True)
    for bucket in out:
        bucket["cost"] = round(bucket["cost"], 6)
    return out


def tool_stats(metas: Sequence[SessionMeta]) -> Dict[str, Any]:
    """Tool-call counts overall, per project, and grouped by MCP server."""
    overall: Dict[str, int] = defaultdict(int)
    per_project: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    mcp_servers: Dict[str, int] = defaultdict(int)

    for meta in metas:
        project = meta.project_path or meta.project_dir
        for name, count in meta.tools.items():
            overall[name] += count
            per_project[project][name] += count
            if name.startswith("mcp__"):
                parts = name.split("__")
                if len(parts) >= 2:
                    mcp_servers[parts[1]] += count

    return {
        "tools": [
            {"name": name, "count": count}
            for name, count in sorted(overall.items(), key=lambda kv: -kv[1])
        ],
        "by_project": {
            project: [
                {"name": name, "count": count}
                for name, count in sorted(tools.items(), key=lambda kv: -kv[1])
            ]
            for project, tools in per_project.items()
        },
        "mcp_servers": [
            {"name": name, "count": count}
            for name, count in sorted(mcp_servers.items(), key=lambda kv: -kv[1])
        ],
        "total_calls": sum(overall.values()),
        "distinct_tools": len(overall),
    }


def heatmap(metas: Sequence[SessionMeta]) -> Dict[str, Any]:
    """Activity counts by weekday and hour, ready for a 7x24 grid."""
    grid = [[0 for _ in range(24)] for _ in range(7)]
    peak = 0
    total = 0
    for meta in metas:
        for slot, count in meta.heatmap.items():
            try:
                weekday_text, hour_text = slot.split("-", 1)
                weekday, hour = int(weekday_text), int(hour_text)
            except (ValueError, AttributeError):
                continue
            if 0 <= weekday < 7 and 0 <= hour < 24:
                grid[weekday][hour] += count
                total += count
                peak = max(peak, grid[weekday][hour])
    return {"weekdays": list(_WEEKDAYS), "grid": grid, "peak": peak, "total": total}


def kpi_cards(
    metas: Sequence[SessionMeta],
    pricing: PricingSource = None,
    today: date | None = None,
) -> List[Dict[str, Any]]:
    """Totals for today, 7 days, 30 days and all time, each vs the period before.

    ``change_percent`` is ``None`` when the previous period had no activity,
    which the UI renders as a dash rather than an infinite increase.
    """
    resolve = _pricing(pricing)
    anchor = today or datetime.now(timezone.utc).date()

    daily: Dict[str, Dict[str, float]] = defaultdict(_empty_bucket)
    session_days: Dict[str, set] = defaultdict(set)
    for meta in metas:
        day_costs = _session_day_model_cost(meta, resolve(meta.provider))
        for day, row in meta.daily.items():
            bucket = daily[day]
            bucket["total_tokens"] += sum(row.get(k, 0) for k in _TOKEN_KEYS)
            bucket["messages"] += row.get("messages", 0)
            bucket["tool_calls"] += row.get("tool_calls", 0)
            bucket["cost"] += day_costs.get(day, 0.0)
            session_days[day].add(meta.session_id)

    def window(start: date, end: date) -> Dict[str, float]:
        """Inclusive-start, inclusive-end aggregate over *daily*."""
        out = _empty_bucket()
        sessions: set = set()
        cursor = start
        while cursor <= end:
            key = cursor.isoformat()
            if key in daily:
                for field, value in daily[key].items():
                    out[field] += value
                sessions |= session_days[key]
            cursor += timedelta(days=1)
        out["sessions"] = len(sessions)
        return out

    def card(label: str, days: int | None) -> Dict[str, Any]:
        """Build one KPI card, with the previous equal-length period."""
        if days is None:
            current = _empty_bucket()
            for bucket in daily.values():
                for field, value in bucket.items():
                    current[field] += value
            # Count every indexed session, including ones with no assistant
            # turn at all (aborted runs contribute no daily bucket).
            current["sessions"] = len(metas)
            previous = None
        else:
            end = anchor
            start = anchor - timedelta(days=days - 1)
            current = window(start, end)
            previous = window(start - timedelta(days=days), start - timedelta(days=1))

        entry: Dict[str, Any] = {
            "label": label,
            "days": days,
            "cost": round(current["cost"], 4),
            "total_tokens": int(current["total_tokens"]),
            "messages": int(current["messages"]),
            "tool_calls": int(current["tool_calls"]),
            "sessions": int(current["sessions"]),
        }
        if previous is not None:
            entry["previous"] = {
                "cost": round(previous["cost"], 4),
                "total_tokens": int(previous["total_tokens"]),
                "messages": int(previous["messages"]),
                "sessions": int(previous["sessions"]),
            }
            entry["change_percent"] = {
                key: (
                    round(100.0 * (entry[key] - previous[key]) / previous[key], 1)
                    if previous[key]
                    else None
                )
                for key in ("cost", "total_tokens", "messages", "sessions")
            }
        return entry

    return [
        card("Today", 1),
        card("Last 7 days", 7),
        card("Last 30 days", 30),
        card("All time", None),
    ]


def disk_usage(metas: Sequence[SessionMeta], claude_home_size: int | None = None) -> Dict[str, Any]:
    """Disk footprint: total, biggest sessions, and growth over time."""
    ordered = sorted(metas, key=lambda m: m.file_size, reverse=True)
    by_day: Dict[str, int] = defaultdict(int)
    for meta in metas:
        day = (meta.last_timestamp or "")[:10]
        if day:
            by_day[day] += meta.file_size
    cumulative = 0
    growth = []
    for day in sorted(by_day):
        cumulative += by_day[day]
        growth.append({"period": day, "bytes": by_day[day], "cumulative_bytes": cumulative})
    return {
        "transcript_bytes": sum(m.file_size for m in metas),
        "claude_home_bytes": claude_home_size,
        "session_count": len(metas),
        "largest": [
            {
                "session_id": m.session_id,
                "title": m.title,
                "project_path": m.project_path,
                "file_size": m.file_size,
                "message_count": m.message_count,
            }
            for m in ordered[:20]
        ],
        "growth": growth,
    }


def aggregate(
    metas: Sequence[SessionMeta],
    granularity: str = "day",
    pricing: PricingSource = None,
    claude_home_size: int | None = None,
) -> Dict[str, Any]:
    """One call producing everything the dashboard view renders."""
    resolve = _pricing(pricing)
    reported = sum(m.reported_cost_usd or 0.0 for m in metas)
    estimated = sum(session_cost(m, resolve) for m in metas)
    return {
        "kpis": kpi_cards(metas, resolve),
        "timeseries": timeseries(metas, granularity, resolve),
        "granularity": granularity,
        "projects": by_project(metas, resolve),
        "models": by_model(metas, resolve),
        "tools": tool_stats(metas),
        "heatmap": heatmap(metas),
        "disk": disk_usage(metas, claude_home_size),
        "totals": {
            "sessions": len(metas),
            "messages": sum(m.message_count for m in metas),
            "tool_calls": sum(m.tool_calls for m in metas),
            "total_tokens": sum(m.total_tokens for m in metas),
            "estimated_cost": round(estimated, 4),
            "reported_cost": round(reported, 4),
            "sessions_with_reported_cost": sum(1 for m in metas if m.reported_cost_usd is not None),
        },
    }


def stats_csv(metas: Sequence[SessionMeta], pricing: PricingSource = None) -> str:
    """Aggregated per-session statistics as CSV text, for the save dialog."""
    resolve = _pricing(pricing)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "session_id", "provider", "title", "project_path", "git_branch",
            "first_message", "last_message", "duration_seconds",
            "messages", "user_messages", "assistant_messages", "tool_calls",
            "input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens",
            "total_tokens", "estimated_cost_usd", "reported_cost_usd",
            "models", "file_size_bytes",
        ]
    )
    for meta in sorted(metas, key=lambda m: m.last_timestamp or ""):
        writer.writerow(
            [
                meta.session_id,
                meta.provider,
                meta.title,
                meta.project_path,
                meta.git_branch,
                meta.first_timestamp or "",
                meta.last_timestamp or "",
                round(meta.duration_seconds, 1),
                meta.message_count,
                meta.user_messages,
                meta.assistant_messages,
                meta.tool_calls,
                meta.tokens.get("input", 0),
                meta.tokens.get("output", 0),
                meta.tokens.get("cache_write", 0),
                meta.tokens.get("cache_read", 0),
                meta.total_tokens,
                round(session_cost(meta, resolve), 6),
                "" if meta.reported_cost_usd is None else round(meta.reported_cost_usd, 6),
                " ".join(sorted(meta.models)),
                meta.file_size,
            ]
        )
    return buffer.getvalue()
