"""Capture the three real web-feedback operations as evidence (O5).

Drives the product's own loopback HTTP entry (`POST /api/followup`) — the same
route the page calls — against real Agent parents from the frozen validation, and
records the request, the response, the child run and its resulting conditions.

This is an HTTP-level capture, not a browser screenshot: it proves the product
route reaches the real Agent engine and produces a new session, and it is honest
about the absence of a real browser.

Usage:
  python main/agent_optimization_web_evidence.py --output <dir> \
      --parent exclude=<run_id> --parent grid=<run_id> --parent goal=<run_id>
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import runlog  # noqa: E402


def _post(base, path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-EQ-Request": "1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as res:
        return json.loads(res.read() or b"{}")


CHANGES = {
    "exclude": ("把本轮实际取得的一个观测排除掉，再告诉我结论会不会变。不要新增取数。",
                {"kind": "exclude"}),
    "grid": ("把到时诊断网格收紧到 3–12 Hz 重算。不要新增取数。",
             {"kind": "stability_bands", "target_band_hz": [3, 12]}),
    "goal": ("只看垂直分量，方法适用性会变成什么？", {"kind": "method_goal",
                                                    "method_goal": "vertical_onset"}),
}


def _resolve(parent_run_id, change):
    """Resolve a runtime target the same way the product would."""
    if change.get("kind") == "exclude" and not change.get("station"):
        state = json.loads((runlog.run_dir(parent_run_id) / "mcp_state.json").read_text())
        acquired = [r for r in state.get("acquired") or [] if not r.get("excluded")]
        if acquired:
            change = {**change, "candidate_id": acquired[-1]["candidate_id"],
                      "station": acquired[-1].get("station")}
    return change


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--parent", action="append", default=[],
                        help="kind=run_id (kinds: exclude, grid, goal)")
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    parents = dict(p.split("=", 1) for p in args.parent)
    base = f"http://127.0.0.1:{args.port}"
    # The port is the first ARGV argument; and a stale server on the same port
    # would silently answer these requests with an older build, so the process is
    # checked to be alive rather than assumed.
    server = subprocess.Popen([str(ROOT / ".venv/bin/python"), str(ROOT / "main/server.py"),
                               str(args.port)],
                              cwd=ROOT, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True)
    records = []
    try:
        for _ in range(40):
            try:
                _get(base, "/api/health")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            raise SystemExit("server did not come up")
        if server.poll() is not None:
            raise SystemExit("server exited immediately (port already in use?)")
        for kind in ("exclude", "grid", "goal"):
            parent = parents.get(kind)
            if not parent:
                records.append({"kind": kind, "status": "skipped",
                                "reason": "no parent run supplied"})
                continue
            instruction, change = CHANGES[kind]
            change = _resolve(parent, change)
            # Snapshot the parent BEFORE the feedback, so "the parent was not
            # rewritten" is a real comparison rather than a tautology.
            _snapshot_parent(parent)
            status, started = _post(base, "/api/followup",
                                    {"parent_run_id": parent, "change": change,
                                     "instruction": instruction})
            entry = {"kind": kind, "parent_run_id": parent, "change": change,
                     "instruction": instruction, "http_status": status,
                     "response": started, "mode": started.get("mode"),
                     "executed_by": started.get("executed_by"),
                     "child_run_id": started.get("run_id")}
            if status == 202 and started.get("run_id"):
                child = started["run_id"]
                for _ in range(600):
                    time.sleep(1)
                    info = _get(base, f"/api/review/{child}")
                    if info.get("status") in ("done", "failed", "error") or \
                            (info.get("result") or {}).get("status"):
                        entry["settled"] = True
                        entry["child_status"] = info.get("status")
                        entry["child_result_status"] = (info.get("result") or {}).get("status")
                        entry["child_verdict"] = (info.get("result") or {}).get("verdict")
                        break
                else:
                    entry["settled"] = False
                # `/api/followup` answers with the TRACK id; the child run id is the
                # newest run whose run.json names this parent.
                folder = _child_run_dir(parent)
                if folder is not None:
                    entry["child_run_id"] = folder.name
                state_path = (folder / "mcp_state.json") if folder else Path("/nonexistent")
                if state_path.exists():
                    state = json.loads(state_path.read_text())
                    entry["child_conditions"] = state.get("requested_conditions")
                    entry["child_excluded"] = [r["candidate_id"] for r in
                                               state.get("acquired") or []
                                               if r.get("excluded")]
                    entry["child_http_attempts"] = state.get("http_attempts")
                entry["parent_files_untouched"] = _parent_unchanged(parent)
            records.append(entry)
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
    out = Path(args.output) / "evidence" / "web-feedback.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "schema": "agent-optimization-web-feedback/v1",
        "entry": "POST /api/followup (the same route main/static/app.js calls)",
        "browser_used": False,
        "browser_note": "HTTP-level capture of the product route; no real browser was "
                        "driven, so this proves the route and the engine, not pixels.",
        "records": records}, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({"n": len(records), "target": str(out),
                      "modes": [r.get("mode") for r in records]}, ensure_ascii=False))
    return 0


_PARENT_SNAPSHOT = {}


def _child_run_dir(parent_run_id):
    root = runlog.RUNLOG
    best = None
    for path in root.glob("r-*/run.json"):
        try:
            meta = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            continue
        if meta.get("parent_run_id") == parent_run_id:
            stamp = path.stat().st_mtime
            if best is None or stamp > best[0]:
                best = (stamp, path.parent)
    return best[1] if best else None


def _snapshot_parent(parent_run_id):
    folder = runlog.run_dir(parent_run_id)
    _PARENT_SNAPSHOT[parent_run_id] = {
        str(p.relative_to(folder)): runlog.sha256_file(p)
        for p in sorted(folder.rglob("*")) if p.is_file()}


def _parent_unchanged(parent_run_id):
    """Compare the parent's own record with the snapshot taken before the call."""
    folder = runlog.run_dir(parent_run_id)
    current = {str(p.relative_to(folder)): runlog.sha256_file(p)
               for p in sorted(folder.rglob("*")) if p.is_file()}
    before = _PARENT_SNAPSHOT.get(parent_run_id)
    return before == current if before is not None else None


if __name__ == "__main__":
    raise SystemExit(main())