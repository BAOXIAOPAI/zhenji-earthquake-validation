"""Call named MCP tools in a FRESH process, then report the persisted counters.

This is the "service restart" half of review-4 A5's pass condition: after a tool
session ends, a NEW process reading `mcp_state.json` from disk must still see the
success counts. The stdio server binds a run per process, so this probe is the
honest equivalent of restarting the tool service on the same run.

Usage:
  python main/mcp_server_probe.py <run_id> <tool>[:<arg>=<value>] ...

Example:
  python main/mcp_server_probe.py r-abc acquire_station:cand-ok
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))
sys.path.insert(0, str(ROOT / "prototype"))
# core puts prototype/ ahead of main/ on sys.path; the production MCP server is
# the one in main/.
sys.path.insert(0, str(ROOT / "main"))

import mcp_server  # noqa: E402


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    run_id = sys.argv[1]
    mcp_server.bind(run_id)
    calls = []
    for token in sys.argv[2:]:
        tool, _, rest = token.partition(":")
        arguments = {}
        for pair in (rest.split(",") if rest else []):
            key, _, value = pair.partition("=")
            arguments[key] = value
        calls.append((tool, arguments))
    results = []
    for tool, arguments in calls:
        fn = getattr(mcp_server, tool)
        results.append(fn(**arguments) if arguments else fn())
    state = mcp_server._load()
    print(json.dumps({
        "run_id": run_id,
        "called": [tool for tool, _ in calls],
        "tool_attempts": state.get("tool_attempts") or {},
        "tool_errors": len(state.get("tool_errors") or []),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
