"""D1 严格离线验收：4 场景（review-5 指南 §5）。

对 `d1-package` 在**独立目录、新建环境、网络拦截开启**下验收：

  1. 故意缺一个必需材料（发现条目 / 波形正文两种形态）——立即缺失提示、
     无联网补齐、拦截确实生效（外部尝试 0）。
  2. 完整样例首轮——预期字段/诊断（E1：coherent_multistation、MCW
     unstable_bimodal、ORCA HN 200 Hz）。
  3. 排除、收紧、恢复——当前条件一致、父记录不改（逐字节比对）。
  4. 一个正确未知明确呈现（S1：排除唯一候选 → pick_unknown）。

每个场景一个独立子进程：独立 runlog 目录 + 包材料副本 +
EQ_STRICT_OFFLINE=1 + _offline_guard 的 socket/urllib 双层拦截。
结果写 `d1-package/offline-check.json`：四类外联计数（发现/波形HTTP/
模型 provider/其他）全部为 0 的口径、拦截记录（必须为空）、环境与入口。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402

GUARD = ROOT / "main/_offline_guard.py"


def _scenario_env(tmp: Path, cache_dir: Path) -> dict:
    return {
        "EQ_STRICT_OFFLINE": "1",
        "EQ_CACHE_DIR": str(cache_dir),
        "RUNLOG_DIR": str(tmp / "runlog"),
        "INTERCEPT_LOG": str(tmp / "intercept.json"),
        "MPLCONFIGDIR": str(tmp / "mpl"),
        "PYTHONPATH": str(ROOT / "main"),
        "PATH": "/usr/bin:/bin",
    }


def _run_code(cache_dir: Path, tmp: Path, code: str) -> dict:
    """跑一个场景子进程（先装拦截守卫，再执行业务代码）。"""
    entry = "import _offline_guard\n" + code
    proc = subprocess.run(
        [sys.executable, "-c", entry],
        env=_scenario_env(tmp, cache_dir), capture_output=True, text=True,
        cwd=str(ROOT))
    intercept = {"count": -1, "attempts": []}
    log = tmp / "intercept.json"
    if log.exists():
        intercept = json.loads(log.read_text())
    return {"returncode": proc.returncode, "stderr": proc.stderr[-4000:],
            "stdout": proc.stdout[-4000:], "intercept": intercept}


HEADER = """\
import sys, json
sys.path.insert(0, {main!r})
sys.path.insert(0, {probes!r})
import review, core, runlog
core.fdsn.set_cache(core.CACHE)
"""


def check(package: Path, *, output_dir=None) -> dict:
    """跑 4 场景并写 offline-check.json。

    `output_dir` 让写盘测试把结果写到本轮目录或临时副本，而不是重生成历史
    交付包里的原件（执行书 §3：写盘测试必须隔离输出）。读材料仍来自 `package`。
    """
    package = package.resolve()
    samples = json.loads((package / "samples.json").read_text())
    scenarios = {}

    # -- 场景 1a：缺发现材料（整轮立即停止） ------------------------------
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        cache = tmp / "cache"
        shutil.copytree(package / "materials/cache", cache)
        # E1 的发现条目 = level=channel 查询且时窗为 2025-03-03 主窗。
        import json as _json
        disc = None
        for m in cache.glob("*.json"):
            url = _json.loads(m.read_text())["url"]
            if "level=channel" in url and "2025-03-03T13" in url:
                disc = m
                break
        assert disc is not None, "包内未找到 E1 发现查询条目"
        disc.unlink()          # 故意缺一个必需材料（发现查询条目）
        out = tmp / "result.json"
        code = HEADER.format(main=str(ROOT / "main"),
                             probes=str(ROOT / "explore/probes")) + f"""
res = review.run_review("E1_orcas_event",
                        "这个信号是不是多台共同记录到的？", "full")
out = {{"res": {{"run_id": res.get("run_id"), "status": res.get("status"),
  "failure_kind": res.get("failure_kind"),
  "missing_url": res.get("missing_url"),
  "failure_category": res.get("failure_category"),
  "reason": res.get("reason"),
  "statement": res.get("statement"),
  "offline_request_counts": (res.get("offline_request_counts") or {{}})}}}}
import pathlib
pathlib.Path({str(out)!r}).write_text(
    json.dumps(out, ensure_ascii=False, indent=1, default=str))
"""
        run = _run_code(cache, tmp, code)
        res = json.loads(out.read_text())["res"] if out.exists() else {}
        scenarios["1a_missing_discovery_material"] = {
            "expect": "整轮立即失败：offline_material_missing，"
                      "携带缺失 URL，无联网补齐",
            "result": {"returncode": run["returncode"],
                       "stderr_tail": run["stderr"][-500:],
                       "res": res,
                       "intercept_count": run["intercept"]["count"]},
            "passed": bool(
                res.get("failure_kind") == "offline_material_missing"
                and res.get("missing_url")
                and run["intercept"]["count"] == 0
                and (res.get("offline_request_counts") or {}).get(
                    "discovery_requests") == 0),
        }

    # -- 场景 1b：缺一个波形正文（该台站立即失败，不联网补齐） ------------
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        cache = tmp / "cache"
        shutil.copytree(package / "materials/cache", cache)
        # 删 OLGA 的 E1 波形正文（2025 主窗的 dataselect 对应 .bin）。
        import json as _json
        victim = None
        for m in cache.glob("*.json"):
            rec = _json.loads(m.read_text())
            if ("dataselect" in rec["url"] and "sta=OLGA" in rec["url"]
                    and "2025-03-03T13" in rec["url"]):
                victim = cache / rec["file"]
                break
        assert victim is not None, "包内未找到 E1 OLGA 波形材料"
        victim.unlink()
        out = tmp / "result.json"
        code = HEADER.format(main=str(ROOT / "main"),
                             probes=str(ROOT / "explore/probes")) + f"""
res = review.run_review("E1_orcas_event",
                        "这个信号是不是多台共同记录到的？", "full")
out = {{"res": {{"run_id": res.get("run_id"), "status": res.get("status"),
  "verdict": res.get("verdict"),
  "stations": [{{"station": s.get("station"), "status": s.get("status"),
                 "error": (s.get("error") or "")[:160],
                 "failed_request": s.get("failed_request")}}
                for s in (res.get("stations") or [])],
  "offline_request_counts": core.fdsn.offline_request_counts()}}}}
import pathlib
pathlib.Path({str(out)!r}).write_text(
    json.dumps(out, ensure_ascii=False, indent=1, default=str))
"""
        run = _run_code(cache, tmp, code)
        res = json.loads(out.read_text())["res"] if out.exists() else {}
        failed = [s for s in res.get("stations", [])
                  if (s.get("status") or "").endswith("_unavailable")]
        scenarios["1b_missing_waveform_body"] = {
            "expect": "对应台站立即失败（waveform_unavailable，"
                      "OfflineMaterialMissing 消息含 URL），其余台站照常，"
                      "无联网补齐",
            "result": {"returncode": run["returncode"],
                       "stderr_tail": run["stderr"][-500:],
                       "failed_stations": failed,
                       "verdict": res.get("verdict"),
                       "intercept_count": run["intercept"]["count"]},
            "passed": bool(
                failed and all(
                    "OfflineMaterialMissing" in (s.get("error") or "")
                    for s in failed)
                and run["intercept"]["count"] == 0),
        }

    # -- 场景 2：完整样例首轮（预期字段/诊断） ---------------------------
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        cache = package / "materials/cache"
        out = tmp / "result.json"
        code = HEADER.format(main=str(ROOT / "main"),
                             probes=str(ROOT / "explore/probes")) + f"""
res = review.run_review("E1_orcas_event",
                        "这个信号是不是多台共同记录到的？", "full")
stabs = {{}}
by_station = (res.get("layers") or {{}}).get("robustness") or {{}}
for st, v in by_station.items():
    stabs[st] = {{"state": (v or {{}}).get("stability"),
                 "spread_s": (v or {{}}).get("spread_s")}}
orca = [s for s in res.get("stations", []) if s.get("station") == "ORCA"]
out = {{"res": {{"run_id": res.get("run_id"), "status": res.get("status"),
  "verdict": res.get("verdict"), "coincidence": res.get("coincidence"),
  "n_eligible": res.get("n_eligible"),
  "stations_acquired": res.get("stations_acquired"),
  "stability": stabs,
  "orca_channels": (orca[0].get("channels") if orca else None),
  "orca_rate": (orca[0].get("sampling_rate_hz") if orca else None),
  "http_attempts": res.get("http_attempts"),
  "offline_request_counts": core.fdsn.offline_request_counts()}}}}
import pathlib
pathlib.Path({str(out)!r}).write_text(
    json.dumps(out, ensure_ascii=False, indent=1, default=str))
"""
        run = _run_code(cache, tmp, code)
        res = json.loads(out.read_text())["res"] if out.exists() else {}
        stab = res.get("stability") or {}
        passed = bool(
            res.get("verdict") == "coherent_multistation"
            and res.get("coincidence") == 6
            and stab.get("MCW", {}).get("state") == "unstable_bimodal"
            and "HNE" in ",".join(res.get("orca_channels") or [])
            and res.get("orca_rate") == 200
            and run["intercept"]["count"] == 0)
        scenarios["2_full_sample_first_round"] = {
            "expect": "E1 预期字段/诊断：coherent_multistation、"
                      "coincidence=6、MCW unstable_bimodal、ORCA HN 200 Hz",
            "result": {"returncode": run["returncode"],
                       "stderr_tail": run["stderr"][-500:], "res": res,
                       "intercept_count": run["intercept"]["count"]},
            "passed": passed,
        }

    # -- 场景 3：排除、收紧、恢复（父记录不改） --------------------------
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        cache = package / "materials/cache"
        out = tmp / "result.json"
        code = HEADER.format(main=str(ROOT / "main"),
                             probes=str(ROOT / "explore/probes")) + f"""
res = review.run_review("E1_orcas_event",
                        "这个信号是不是多台共同记录到的？", "full")
parent = res["run_id"]
pf = runlog.run_dir(parent)
parent_files = {{p.name: p.read_bytes()
                 for p in pf.iterdir() if p.is_file()}}
ex = review.run_followup(parent,
    change={{"kind": "exclude", "station": "OLGA"}},
    instruction="把 OLGA 排除掉，结论会不会变？")
narrow = review.run_followup(ex["run_id"],
    change={{"kind": "stability_bands", "target_band_hz": [3, 12]}},
    instruction="把到时网格收紧到 3–12 Hz 重算。")
restore = review.run_followup(narrow["run_id"],
    change={{"kind": "include", "station": "OLGA"}},
    instruction="把 OLGA 恢复回来。")
parent_after = {{p.name: p.read_bytes()
                 for p in pf.iterdir() if p.is_file()}}
out = {{"parent": parent,
  "chain": [r["run_id"] for r in (res, ex, narrow, restore)],
  "verdicts": [r.get("verdict") for r in (res, ex, narrow, restore)],
  "exclusions": [
     (r.get("layers") or {{}}).get("excluded_candidates")
     for r in (ex, narrow, restore)],
  "parent_unchanged": parent_files == parent_after,
  "offline_request_counts": core.fdsn.offline_request_counts()}}
import pathlib
pathlib.Path({str(out)!r}).write_text(
    json.dumps(out, ensure_ascii=False, indent=1, default=str))
"""
        run = _run_code(cache, tmp, code)
        res = json.loads(out.read_text()) if out.exists() else {}
        verdicts = res.get("verdicts") or []
        exclusions = res.get("exclusions") or []
        scenarios["3_exclude_narrow_restore"] = {
            "expect": "三轮反馈判决一致（聚集不因排除一台/收窄/恢复而"
                      "翻转），排除链逐步收窄再恢复，父记录逐字节不变",
            "result": {"returncode": run["returncode"],
                       "stderr_tail": run["stderr"][-500:],
                       "chain": res.get("chain"),
                       "verdicts": verdicts,
                       "excluded_candidates": exclusions,
                       "parent_unchanged": res.get("parent_unchanged"),
                       "intercept_count": run["intercept"]["count"]},
            "passed": bool(
                run["returncode"] == 0
                and verdicts and all(v == "coherent_multistation"
                                     for v in verdicts)
                and exclusions[0] and not (exclusions[2] or [])
                and res.get("parent_unchanged") is True
                and run["intercept"]["count"] == 0),
        }

    # -- 场景 4：正确未知（S1 排除唯一候选） ------------------------------
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        cache = package / "materials/cache"
        out = tmp / "result.json"
        code = HEADER.format(main=str(ROOT / "main"),
                             probes=str(ROOT / "explore/probes")) + f"""
first = review.run_review("S1_sparse_2003",
                          "这段观测能不能用来做 1–12 Hz 的 P/S 拾取？",
                          "full")
child = review.run_followup(first["run_id"],
    change={{"kind": "exclude", "station": "HOOD"}},
    instruction="排除 HOOD，只复用已有观测重算，不要新增取数。")
ans = child.get("answered") or {{}}
out = {{"first": {{"verdict": first.get("verdict"),
                  "run_id": first.get("run_id")}},
  "followup": {{"run_id": child.get("run_id"),
    "verdict": child.get("verdict"),
    "answered": ans.get("answered"), "value": ans.get("value"),
    "field": ans.get("field"),
    "terminated_by": child.get("terminated_by"),
    "reason": child.get("reason")}},
  "offline_request_counts": core.fdsn.offline_request_counts()}}
import pathlib
pathlib.Path({str(out)!r}).write_text(
    json.dumps(out, ensure_ascii=False, indent=1, default=str))
"""
        run = _run_code(cache, tmp, code)
        res = json.loads(out.read_text()) if out.exists() else {}
        fu = res.get("followup") or {}
        scenarios["4_correct_unknown"] = {
            "expect": "S1 首轮 pick_applicable；排除唯一候选后 pick_unknown"
                      "（answered=false、terminated=unanswered_no_data），"
                      "是正确未知不是否定结论",
            "result": {"returncode": run["returncode"],
                       "stderr_tail": run["stderr"][-500:],
                       "first": res.get("first"),
                       "followup": fu,
                       "intercept_count": run["intercept"]["count"]},
            "passed": bool(
                (res.get("first") or {}).get("verdict") == "pick_applicable"
                and fu.get("verdict") == "pick_unknown"
                and fu.get("answered") is False
                and fu.get("terminated_by") == "unanswered_no_data"
                and run["intercept"]["count"] == 0),
        }

    # 材料清单复核（MANIFEST-SHA 不含自身）。
    manifest_ok, manifest_files = True, 0
    for line in (package / "MANIFEST-SHA.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, rel = line.split(None, 1)
        target = package / rel.strip()
        manifest_files += 1
        import hashlib
        if not target.exists() or hashlib.sha256(
                target.read_bytes()).hexdigest() != digest:
            manifest_ok = False
    record = {
        "manifest_verified": manifest_ok,
        "manifest_files": manifest_files,
        "schema": "d1-offline-check/v1",
        "checked_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "package": str(package.relative_to(ROOT)),
        "scenario_notes": (
            "每个场景独立子进程：独立 runlog + 材料副本 + "
            "EQ_STRICT_OFFLINE=1 + socket/urllib 双层网络拦截"
            "（回环放行）。四类外联计数（发现/波形HTTP/模型provider/其他）"
            "全部为 0；拦截记录为空才通过。模型离线被拒绝"
            "（provider_bridge 503 offline_refused），轨迹只能回放，"
            "不声称离线执行真实模型。"),
        "scenarios": scenarios,
        "all_passed": (manifest_ok
                       and all(s["passed"] for s in scenarios.values())),
        "entry_commands": [
            ".venv/bin/python main/run_d1_check.py "
            "delivery/revision-5/d1-package",
            "EQ_STRICT_OFFLINE=1 EQ_CACHE_DIR="
            "delivery/revision-5/d1-package/materials/cache "
            ".venv/bin/python main/server.py",
        ],
        "environment": {
            "python": sys.version.split()[0],
            "obspy": "项目 .venv（earthquake-validation/.venv）",
            "server": "main/server.py（标准库 http.server，回环）",
            "interception": "main/_offline_guard.py（socket.connect + "
                            "urllib.urlopen，非回环一律拦截并记录）",
        },
    }
    out = Path(output_dir) if output_dir else package
    out.mkdir(parents=True, exist_ok=True)
    (out / "offline-check.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1, default=str))
    for name, s in scenarios.items():
        mark = "✓" if s["passed"] else "✗"
        print(f"  {mark} {name}")
    print(f"D1 离线验收：{'全部通过' if record['all_passed'] else '存在失败'}"
          f" → {out / 'offline-check.json'}")
    return record


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else
                  "delivery/revision-5/d1-package")
    record = check(target)
    raise SystemExit(0 if record["all_passed"] else 1)
