"""Thin stdio launcher that pins one run's isolated paths (O3, book §6.3).

The driver used to launch `mcp_server.py` directly, so the MCP child inherited only
whatever the driver happened to export. RUNLOG_DIR, the cache, the task/output
directories and the strict-offline flag were not guaranteed to reach the AGH
worker: assuming the shell environment propagated was the isolation defect.

This launcher sets a frozen whitelist of environment variables from a config file
BEFORE importing `mcp_server` (and therefore before `runlog`/`core` read them),
then runs the real MCP server over stdio. It accepts no arbitrary keys, and it does
not use `shell -c` or a raw `--env` the framework does not support.

Usage:
  python main/mcp_launcher.py --run-id <run_id> --config <isolation.json>
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# The only keys the launcher will set. Anything else in the config is refused rather
# than silently injected into the child process environment.
ALLOWED = (
    "RUNLOG_DIR", "EQ_CACHE_DIR", "EQ_STRICT_OFFLINE", "EQ_TASK_DIR",
    "EQ_OUTPUT_DIR", "EQ_MCP_ISOLATION", "MPLCONFIGDIR", "XDG_CACHE_HOME",
    "EQ_RUN_ID",
)


def _paths(root: Path) -> dict:
    return {
        "MPLCONFIGDIR": str(root / ".cache/matplotlib"),
        "XDG_CACHE_HOME": str(root / ".cache"),
    }


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    run_id = None
    config = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--config":
            config = json.loads(Path(argv[i + 1]).read_text())
            i += 2
        elif token == "--run-id":
            run_id = argv[i + 1]
            i += 2
        else:
            run_id = run_id or token
            i += 1
    if not run_id:
        raise SystemExit("mcp_launcher: a run id is required")
    root = Path(__file__).resolve().parents[1]
    unknown = sorted(set(config) - set(ALLOWED))
    if unknown:
        raise SystemExit(f"mcp_launcher: refused non-whitelisted keys: {unknown}")
    for key, value in {**_paths(root), **config}.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    os.environ["EQ_RUN_ID"] = str(run_id)
    os.environ.setdefault("EQ_STRICT_OFFLINE", "0")
    sys.path.insert(0, str(root / "main"))
    sys.path.insert(0, str(root / "explore/probes"))
    import mcp_server  # noqa: PLC0415

    mcp_server.bind(run_id)
    mcp_server.server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())