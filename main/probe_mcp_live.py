"""No-paid live acquisition probe through the production MCP stdio entry.

The paid AGH smoke test answered the question correctly, but its bytes came from
the development cache (`http_attempts == 0`). This probe closes that gap at zero
model cost: it starts the real `main/mcp_server.py` over stdio, hands it a
free-input task on a window that is not in the cache, and performs a real FDSN
download through the tools the model would call.

It decides pass/fail only on recorded facts: the candidate list, the acquisition
status, the raw files on disk, and `http_attempts` in the tool state.

Usage:
  python main/probe_mcp_live.py [--window-start ... --window-end ...] [--region orcas]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import prepare_agent  # noqa: E402
import task as taskmod  # noqa: E402
import task_input  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

PY = ROOT / ".venv/bin/python"
SERVER = ROOT / "main/mcp_server.py"
DEFAULT_WINDOW = ("2025-03-01T08:00:00", "2025-03-01T08:02:00")


def _tool_call(result):
    payload = getattr(result, "structured_content", None)
    if payload is None:
        payload = json.loads(result.content[0].text)
    return payload


async def _probe(run_id, env):
    params = StdioServerParameters(
        command=str(PY), args=[str(SERVER), run_id], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            candidates = _tool_call(await session.call_tool("list_candidates", {}))
            first = next(c for c in candidates["candidates"]
                         if c["candidate_id"] != "cand-fail")
            acquired = _tool_call(await session.call_tool(
                "acquire_station", {"candidate_id": first["candidate_id"]}))
            evidence = _tool_call(await session.call_tool("get_evidence", {}))
    return names, candidates, acquired, evidence


def main():
    argv = sys.argv[1:]
    window = list(DEFAULT_WINDOW)
    region_name = "orcas"
    for i, token in enumerate(argv):
        if token == "--window-start":
            window[0] = argv[i + 1]
        elif token == "--window-end":
            window[1] = argv[i + 1]
        elif token == "--region":
            region_name = argv[i + 1]

    core.fdsn.set_cache(core.CACHE)
    parsed = task_input.build({"window_start": window[0], "window_end": window[1],
                               "question": "这段观测能不能用于 1–12 Hz 的 P/S 拾取？",
                               "region": region_name})
    if not parsed.get("ready"):
        raise SystemExit(f"task not ready: {parsed.get('missing')}")
    frozen = taskmod.task_from_spec(parsed["structured"],
                                   user_question="这段观测能不能用于 1–12 Hz 的 P/S 拾取？")
    taskmod.freeze(frozen)
    prepared = prepare_agent.prepare_task(frozen, arm="live_probe")
    run_id = prepared["run_id"]
    folder = ROOT / "main/runlog" / run_id

    env = {**os.environ, "RUNLOG_DIR": str(ROOT / "main/runlog"),
           "EQ_CACHE_DIR": str(core.CACHE)}
    names, candidates, acquired, evidence = asyncio.run(_probe(run_id, env))

    state = json.loads((folder / "mcp_state.json").read_text())
    raw = folder / "raw" / acquired.get("candidate_id", "?") / "waveform.mseed"
    summary = {
        "run_id": run_id, "task_id": frozen["task_id"],
        "demo_case_ref": frozen.get("demo_case_ref"),
        "window": window, "region": region_name,
        "tools": names,
        "n_candidates": len(candidates["candidates"]),
        "candidate_id": acquired.get("candidate_id"),
        "station": acquired.get("station"),
        "acquisition_status": acquired.get("status"),
        "sampling_rate_hz": acquired.get("sampling_rate_hz"),
        "channel_integrity_flags": acquired.get("channel_integrity_flags"),
        "band_support": acquired.get("band_support"),
        "raw_waveform_exists": raw.exists(),
        "raw_waveform_bytes": raw.stat().st_size if raw.exists() else 0,
        "http_attempts": state.get("http_attempts"),
        "cache_hits": state.get("cache_hits"),
        "bytes": state.get("bytes"),
        "tool_errors": evidence.get("tool_errors"),
        "failed_requests": evidence.get("failed_requests"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str), flush=True)

    # Pass only if a real download happened and produced an analysable record.
    if acquired.get("status") != "analysed":
        raise SystemExit(f"not analysed: {acquired.get('status')}")
    if not raw.exists() or raw.stat().st_size == 0:
        raise SystemExit("no raw waveform on disk")
    if not state.get("http_attempts"):
        raise SystemExit("no external request was made; the cache hid the interface")


if __name__ == "__main__":
    main()