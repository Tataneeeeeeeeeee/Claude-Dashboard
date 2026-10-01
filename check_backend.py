#!/usr/bin/env python3
"""Stage 2 smoke check: start the backend and exercise every endpoint.

Run it with the project's virtualenv:

    .venv/bin/python check_backend.py

It prints a short report and exits non-zero if anything fails.  Nothing is
written to ``~/.claude``.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentboard.server import BackgroundServer  # noqa: E402


def main() -> int:
    """Start the server, call each endpoint, report timings."""
    server = BackgroundServer()
    failures = 0
    try:
        base = server.start()
        print(f"server      {base}")

        def call(path: str):
            """GET *path* and return the decoded body, timing the round trip."""
            started = time.time()
            with urllib.request.urlopen(base + path, timeout=120) as response:
                raw = response.read()
            elapsed = time.time() - started
            try:
                return json.loads(raw), elapsed
            except ValueError:
                return raw.decode("utf-8", "replace"), elapsed

        checks = [
            ("/api/health", lambda b: b["ok"] is True),
            ("/api/bootstrap", lambda b: "stats" in b and "projects" in b),
            ("/api/sessions?sort=expensive&limit=5", lambda b: "sessions" in b),
            ("/api/search?q=def%20main", lambda b: "hits" in b),
            ("/api/usage?granularity=week", lambda b: "kpis" in b),
            ("/api/usage/csv", lambda b: isinstance(b, str) and b.startswith("session_id")),
            ("/api/claude-config", lambda b: "settings_path" in b),
            ("/api/config", lambda b: "pricing" in b),
            ("/api/index/status", lambda b: "percent" in b),
        ]
        for path, predicate in checks:
            try:
                body, elapsed = call(path)
                ok = predicate(body)
            except (urllib.error.URLError, OSError, KeyError, TypeError) as exc:
                body, elapsed, ok = str(exc), 0.0, False
            failures += 0 if ok else 1
            print(f"{'ok  ' if ok else 'FAIL'}  {elapsed:6.2f}s  {path}")

        stats, _ = call("/api/bootstrap")
        info = stats["stats"]
        print()
        print(f"sessions    {info['session_count']}")
        print(f"projects    {info['project_count']}")
        print(f"messages    {info['message_count']:,}")
        print(f"tokens      {info['total_tokens']:,}")
        print(f"corrupt     {info['corrupt_lines']} lines")

        usage, _ = call("/api/usage")
        totals = usage["totals"]
        print(f"estimated   ${totals['estimated_cost']:,.2f}")
        print(f"reported    ${totals['reported_cost']:,.2f} "
              f"(from {totals['sessions_with_reported_cost']} sessions)")
    finally:
        server.stop()
        print("\nserver stopped cleanly")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
