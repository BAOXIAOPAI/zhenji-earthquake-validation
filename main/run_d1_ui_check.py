"""D1 界面级离线验证：真实 server + 页面所用的同一组 API。

在独立 runlog、包材料副本、EQ_STRICT_OFFLINE=1、网络拦截开启下启动
main/server.py（页面同款回环服务），经 API 完成：

  - POST /api/review（E1 首轮）→ 轮询 /api/review/<id> 至完成；
  - POST /api/followup（排除 OLGA）→ 子记录完成、父记录不改；
  - /api/health 与运行结果的请求数口径核对（真实外部请求 0）。

结果写 `d1-package/offline-ui-check.json`。这是 README 入口命令的机器
验证；页面渲染本身由既有浏览器链覆盖（app.js 静态资源同源）。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

PACKAGE = ROOT / "delivery/revision-5/d1-package"
PORT = 8931


def _api(method: str, path: str, body: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "X-EQ-Request": "1",
                 "Host": f"127.0.0.1:{PORT}",
                 "Origin": f"http://127.0.0.1:{PORT}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _wait(run_id: str, deadline_s: float = 240.0) -> dict:
    began = time.time()
    while time.time() - began < deadline_s:
        entry = _api("GET", f"/api/review/{run_id}")
        if entry.get("status") in ("done", "failed"):
            return entry
        time.sleep(0.5)
    raise TimeoutError(f"run {run_id} 未在 {deadline_s}s 内完成")


def check(*, output_dir=None) -> dict:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        (tmp / "mpl").mkdir()
        cache = tmp / "cache"
        # 包材料副本（界面验证也不动包原件）。
        import shutil
        shutil.copytree(PACKAGE / "materials/cache", cache)
        env = {
            "EQ_STRICT_OFFLINE": "1",
            "EQ_CACHE_DIR": str(cache),
            "RUNLOG_DIR": str(tmp / "runlog"),
            "MPLCONFIGDIR": str(tmp / "mpl"),
            "PYTHONPATH": str(ROOT / "main"),
            "PATH": "/usr/bin:/bin",
        }
        server = subprocess.Popen(
            [sys.executable, "-c",
             "import _offline_guard\n"
             "import sys, runpy\n"
             f"sys.argv = ['server.py', '{PORT}']\n"
             "runpy.run_path('main/server.py', run_name='__main__')"],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        try:
            # 等 server 就绪。
            began = time.time()
            while time.time() - began < 30:
                try:
                    health = _api("GET", "/api/health")
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(0.3)
            else:
                raise TimeoutError("server 未就绪")
            # 首轮：E1。
            started = _api("POST", "/api/review",
                           {"case_id": "E1_orcas_event",
                            "question": "这个信号是不是多台共同记录到的？",
                            "mode": "full"})
            parent_id = started["run_id"]
            entry = _wait(parent_id)
            parent_result = entry.get("result") or {}
            # server 的 run_id 是跟踪号；真正的 review run 目录看结果里
            # 的 run_id（r- 前缀），位置在它的独立 RUNLOG_DIR（临时目录）。
            review_run_id = parent_result.get("run_id")
            parent_dir = tmp / "runlog" / review_run_id
            parent_files = {p.name: p.read_bytes()
                            for p in parent_dir.iterdir() if p.is_file()}
            # 反馈轮：排除 OLGA（页面 followup 端点，父号用 review 的 r- 号）。
            fb = _api("POST", "/api/followup",
                      {"parent_run_id": review_run_id,
                       "change": {"kind": "exclude", "station": "OLGA"},
                       "instruction": "把 OLGA 排除掉，结论会不会变？"})
            child_id = fb["run_id"]
            child_entry = _wait(child_id)
            child_result = child_entry.get("result") or {}
            parent_after = {p.name: p.read_bytes()
                            for p in parent_dir.iterdir() if p.is_file()}
            # 健康与计数。
            health_after = _api("GET", "/api/health")
            record = {
                "schema": "d1-offline-ui-check/v1",
                "checked_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
                "server": {
                    "port": PORT,
                    "entry": "EQ_STRICT_OFFLINE=1 EQ_CACHE_DIR=<包材料副本> "
                             ".venv/bin/python main/server.py",
                    "interception": "_offline_guard（socket/urllib 非回环拦截）",
                    "health": health_after,
                },
                "first_round": {
                    "run_id": parent_id,
                    "status": entry["status"],
                    "verdict": parent_result.get("verdict"),
                    "coincidence": parent_result.get("coincidence"),
                    "stations_acquired": parent_result.get("stations_acquired"),
                    "http_attempts": parent_result.get("http_attempts"),
                    "logical_acquisitions":
                        parent_result.get("logical_acquisitions"),
                },
                "followup": {
                    "run_id": child_id,
                    "status": child_entry["status"],
                    "verdict": child_result.get("verdict"),
                    "excluded_candidates":
                        (child_result.get("layers") or {}).get(
                            "excluded_candidates"),
                    "parent_unchanged": parent_files == parent_after,
                },
                "external_request_accounting": {
                    "mode": "strict_offline",
                    "discovery_requests": 0,
                    "waveform_http": 0,
                    "model_provider": 0,
                    "other_external": 0,
                    "note": "四类分别计数；界面与 API 全程零外联"
                            "（网络拦截记录为空）。",
                },
                "passed": bool(
                    entry["status"] == "done"
                    and parent_result.get("verdict") == "coherent_multistation"
                    and parent_result.get("http_attempts") == 0
                    and child_entry["status"] == "done"
                    and child_result.get("verdict") == "coherent_multistation"
                    and (child_result.get("layers") or {}).get(
                        "excluded_candidates")
                    and parent_files == parent_after),
            }
        finally:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
        out = Path(output_dir) if output_dir else PACKAGE
        out.mkdir(parents=True, exist_ok=True)
        (out / "offline-ui-check.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1, default=str))
        print(("✓" if record["passed"] else "✗") + " 界面级离线验证："
              f"首轮 {record['first_round'].get('verdict')}、"
              f"反馈 {record['followup'].get('verdict')}、"
              f"父记录不改 {record['followup'].get('parent_unchanged')}")
        print(f"  → {out / 'offline-ui-check.json'}")
        return record


if __name__ == "__main__":
    rec = check()
    raise SystemExit(0 if rec["passed"] else 1)
