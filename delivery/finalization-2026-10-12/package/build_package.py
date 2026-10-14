"""Build the submission package for 震迹 (source + docs + replay excerpts).

Curated on purpose: no model key, no .venv, no node_modules/dist install
products, no full cache, no full historical runlog, no video. AGH is included as
SOURCE plus the lockfile and the exact commit, not as installed build output.

Run:
  .venv/bin/python -B delivery/finalization-2026-10-12/package/build_package.py
"""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PKG = Path(__file__).resolve().parent
SUBMISSION = {
    "group": "本科生组", "entry_number": "U105", "team": "包小派派派",
    "captain": "王新宇", "members": ["王新宇"],
    "school": "苏州大学", "major": "软件工程",
}
STAGE = PKG / "staging" / "U105-2026-10-14" / "zhenji-package"
ZIP = PKG / ("本科生组-U105-包小派派派-王新宇.zip")
LIMIT = 200 * 1024 * 1024

EXCLUDE_DIRS = {"runlog", "cache", "tasks", "out", "runs", "ui-trace-runlog",
                "__pycache__", "node_modules", "dist", ".pytest_cache",
                ".venv", "artifacts", "staging", "package", ".git",
                ".agnes-tmp", ".cache", ".agnes", "target"}


def ignore(_dir, names):
    return [n for n in names if n in EXCLUDE_DIRS or n.endswith(".pyc")
            or n == ".env" or n.startswith(".env.")]


def copy_tree(src: Path, dst_rel: str):
    if not src.exists():
        return
    shutil.copytree(src, STAGE / dst_rel, dirs_exist_ok=True, ignore=ignore)


def copy_file(src: Path, dst_rel: str):
    if not src.exists():
        return
    target = STAGE / dst_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    # 1) runnable source (no runtime data)
    copy_tree(ROOT / "main", "main")
    copy_tree(ROOT / "tests", "tests")
    copy_tree(ROOT / "configs", "configs")
    copy_tree(ROOT / "explore/probes", "explore/probes")
    copy_file(ROOT / "src/budget.mjs", "src/budget.mjs")
    copy_file(ROOT / "prototype/model-budget.json", "prototype/model-budget.json")
    for name in ("engine.py", "intake.py", "quality.py", "recovery.py"):
        copy_file(ROOT / "prototype" / name, f"prototype/{name}")
    for name in ("requirements.txt", "requirements.lock.txt", "README.md", ".gitignore"):
        copy_file(ROOT / name, name)

    # 2) AGH SOURCE + lockfile + provenance (not installed build output)
    copy_tree(ROOT / "vendor/agnes-harness", "vendor/agnes-harness")
    for name in ("agh-commit.json", "agh-install.md"):
        copy_file(ROOT / "data/manifests" / name, f"data/manifests/{name}")

    # 3) documents, UI evidence, public material, fixtures
    copy_tree(ROOT / "delivery/finalization-2026-10-12", "delivery/finalization-2026-10-12")
    copy_file(ROOT / "delivery/finalization-2026-10-12/package/build_package.py",
              "delivery/finalization-2026-10-12/package/build_package.py")
    copy_file(ROOT / "delivery/finalization-2026-10-12/package/提交包说明与打包指南.md",
              "delivery/finalization-2026-10-12/package/提交包说明与打包指南.md")
    for name in ("独立回放核验-2026-10-14.json", "package_runtime_audit.py"):
        copy_file(PKG / name, f"delivery/finalization-2026-10-12/package/{name}")
    copy_file(ROOT / "delivery/remediation-2026-10-12/HANDOFF.md",
              "delivery/remediation-2026-10-12/HANDOFF.md")
    for name in ("remediation_probe.py", "remediation_probe.json",
                 "A_B_C_probe_rerun.py", "A_B_C_probe_rerun.json",
                 "agh_resource_integration.mjs", "agh_resource_integration.json",
                 "real_page_chain.py", "real_page_chain_v2.json",
                 "real_page_chain.json", "real_page_feedback_retry.json",
                 "real_page_feedback_retry2.json",
                 "mock_provider.mjs"):
        copy_file(ROOT / "delivery/remediation-2026-10-12/evidence" / name,
                  f"delivery/remediation-2026-10-12/evidence/{name}")
    standalone = "delivery/remediation-2026-10-12/evidence/artifacts/real-first-round-standalone"
    for name in ("agent_config.json", "task.json", "tool_calls.jsonl", "mcp_state.json",
                 "cleanup_status.json", "native_agh_evidence.json"):
        copy_file(ROOT / standalone / name, f"{standalone}/{name}")
    copy_file(ROOT / "delivery/review-10-2026-10-12/参赛主体接收结论.md",
              "delivery/review-10-2026-10-12/参赛主体接收结论.md")
    for name in ("ABC_final_recheck.py", "ABC_final_recheck.json",
                 "真实最小闭环验收.md", "真实最小闭环独立核对.json",
                 "停止与上限身份复验.md", "接收版本索引.json"):
        copy_file(ROOT / "delivery/review-10-2026-10-12/evidence" / name,
                  f"delivery/review-10-2026-10-12/evidence/{name}")
    for name in ("参赛收尾执行与验收方案-2026-10-12.md",
                 "参赛收尾执行交接指令-2026-10-12.md",
                 "参赛主体最终整改方案-2026-10-12.md"):
        copy_file(ROOT / "reports" / name, f"reports/{name}")

    # 4) replay excerpt: the accepted chain + the rejected feedback attempts
    replay_runs = ["r-e2e8419bf299785d", "r-3ae9c6948c2f8967",
                   "r-2365883191ac74a8", "r-8f2eb7e2a2e7333f",
                   "r-8c01734ee9f1903a"]
    keep = ["task.json", "mcp_state.json", "mcp_state.initial.json", "validation.json",
            "result.json", "cost.json", "run.json", "tool_calls.jsonl",
            "agent_config.json", "native_agh_evidence.json", "cleanup_status.json",
            "model_response.json", "discovery.json", "evaluator.json",
            "scope_resolution.json", "bridge_request_summary.json",
            "native_host_audit.jsonl", "native_sessions.db", "prompt_binding.json"]
    for run_id in replay_runs:
        src = ROOT / "main/runlog" / run_id
        if not src.exists():
            continue
        for name in keep:
            copy_file(src / name, f"replay/runlog/{run_id}/{name}")
        for kind in ("raw", "derived"):
            copy_tree(src / kind, f"replay/runlog/{run_id}/{kind}")

    # 5) desensitized cost snapshot (NOT the live ledger)
    ledger = json.loads((ROOT / "runs/model-budget-ledger.json").read_text())
    entries = ledger.get("entries") or []
    total = sum(e.get("cost_upper_cny") or e.get("reserved_cny") or 0
                for e in entries)
    snapshot = {
        "schema": "cost-snapshot/v1",
        "note": ("脱敏费用快照：只含累计与分链上界，不含完整台账；"
                 "研发机原台账保持完整，不用本快照替换。"),
        "ledger_entries": len(entries),
        "cumulative_cost_upper_cny": round(total, 6),
        "guard_limit_cny": 20,
        "accepted_chain": {"first_round": "r-e2e8419bf299785d",
                           "first_round_cost_upper_cny": 0.100140,
                           "feedback": "r-3ae9c6948c2f8967",
                           "feedback_cost_upper_cny": 0.081032,
                           "chain_total_upper_cny": 0.181172},
    }
    (STAGE / "费用快照.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2))

    # 6) MANIFEST + zip
    files = sorted(p for p in STAGE.rglob("*") if p.is_file())
    manifest = {
        "schema": "submission-package/v1",
        "project": "震迹——基于Agnes Harness的地震候选事件智能核查Agent",
        "naming": ZIP.name,
        "submission": SUBMISSION,
        "status": "候选提交包；报名信息已确认，成员声明待本人确认，确认形式按实际提交页面，视频与公开内容链接待补",
        "excludes": [".venv", "vendor 安装产物(node_modules/dist)",
                     "main/runlog 全量历史", "main/cache", "data/ 全量数据（保留 AGH 来源记录）", "视频文件",
                     "模型 Key 与任何密码"],
        "replay": {"runlog_dir": "replay/runlog",
                   "note": "设置 RUNLOG_DIR 指向 replay/runlog 可只读回放示例链"},
        "files": [{"path": str(p.relative_to(STAGE)), "bytes": p.stat().st_size,
                   "sha256": sha256(p)} for p in files],
        "file_count": len(files),
        "uncompressed_bytes": sum(p.stat().st_size for p in files),
    }
    (STAGE / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2))
    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(STAGE.rglob("*")):
            if path.is_file():
                zf.write(path, Path("zhenji-package") / path.relative_to(STAGE))
    size = ZIP.stat().st_size
    print(json.dumps({
        "zip": str(ZIP), "zip_bytes": size, "zip_mb": round(size / 1e6, 1),
        "file_count": manifest["file_count"],
        "uncompressed_mb": round(manifest["uncompressed_bytes"] / 1e6, 1),
        "under_200mb": size <= LIMIT,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
