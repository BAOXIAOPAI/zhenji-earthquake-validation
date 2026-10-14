"""C2 参照缺陷的零付费修复：重跑 R 参照行 + 按修正参照重评 M/A 行。

缺陷（C2 v2 首次完整批次暴露）：
  1. R 臂用 mode="full" 走 run_review 的“按案例预置问题”分支，忽略了任务
     声明的问题。C2-T3/T4 复用 E4/Q4 时窗问方法/仪器问题，R 却答了传播
     问题（coherent_multistation / single_station_only），M/A 答对了问题
     却被拿错误参照判成“错误交付”。
  2. 反馈指令写“不要重新取数”的任务没有声明 acquisition_allowed=false，
     照指令不取数的 A 被误判“策略不足”。

修复原则（指南“发现合同或评分错误先修无付费入口，修复后标版本”）：
  - M/A 的**运行与费用不动**（真实运行、真实花费），只按修正后的参照与
    权限声明确定性重评；
  - R 行免费重跑（R 不调用模型），绑定新版本快照 c2-locked-v3；
  - 全部改动写入 repair 记录，不回填历史、不删除原行。

用法：
    .venv/bin/python main/repair_c2.py            # 修复并写回
    .venv/bin/python main/repair_c2.py --dry-run  # 只报告不写
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import c_scorer  # noqa: E402
import core  # noqa: E402
import run_c  # noqa: E402

OUT = ROOT / "delivery/revision-5/C-RESULTS.json"
EVIDENCE = ROOT / "delivery/revision-5/evidence/C2-参照修复记录.json"
LABEL = "c2-locked-v3"


def _snapshot():
    snap = run_c.version_snapshot(LABEL)
    return snap, run_c._sha256(snap)


def repair(dry_run: bool = False) -> dict:
    tasks = json.loads((ROOT / "main/c2_tasks.json").read_text())
    by_id = {t["id"]: t for t in tasks["tasks"]}
    snap, snap_digest = _snapshot()
    core.fdsn.set_cache(core.CACHE)

    before = json.loads(OUT.read_text())
    c1_rows = [r for r in before["runs"]
               if not str(r.get("batch", "")).startswith("C2")]
    c2_rows = [r for r in before["runs"]
               if str(r.get("batch", "")).startswith("C2")]
    m_a_rows = [r for r in c2_rows if r.get("arm") in ("M", "A")]
    old_r_rows = [r for r in c2_rows if r.get("arm") == "R"]
    print(f"载入：C1 {len(c1_rows)} 行、C2 M/A {len(m_a_rows)} 行、"
          f"C2 R {len(old_r_rows)} 行（将免费重跑）")

    # --- 1) R 行免费重跑（修正问题分类） --------------------------------
    references, new_r_rows = {}, []
    for task_id, task in by_id.items():
        repeats = task.get("repeats") or tasks.get("repeats") or 1
        for repeat in range(1, repeats + 1):
            began = time.time()
            first = run_c._row_for_r(task)
            contract = {"question_id": task.get("question_id"),
                        "method_goal": task.get("method_goal"),
                        "ceiling": tasks.get("ceiling", 6),
                        "version": snap_digest}
            if repeat == 1:
                references[task_id] = first.get("verdict") \
                    if first.get("status") == "succeeded" else None
            raw = run_c._raw_row("R", first)
            ref = references.get(task_id)
            if repeat > 1 or True:
                raw["expected_verdict"] = ref
            row = {**raw, "task_id": task_id, "arm": "R",
                   "batch": "C2-locked", "repeat": repeat,
                   "version_snapshot": snap.name,
                   "version_snapshot_sha256": snap_digest,
                   "elapsed_s": round(time.time() - began, 1),
                   "repaired_at": datetime.now(timezone.utc).strftime(
                       "%Y-%m-%dT%H:%M:%SZ"),
                   "repair_note": "问题分类缺陷修复后免费重跑（R 不调用模型）",
                   "c_evaluation": c_scorer.score_row(
                       c_scorer.normalize_row(raw, contract),
                       facts=run_c._facts_for_row("R", first))}
            new_r_rows.append(row)
            for variant in run_c._feedback_variants(task):
                if not first.get("run_id"):
                    continue
                fb = run_c.run_feedback(task, "R", first["run_id"], variant)
                f_raw = run_c._raw_row("R", fb)
                f_raw["acquisition_allowed"] = variant.get(
                    "acquisition_allowed", True)
                f_contract = {**contract,
                              "acquisition_allowed": variant.get(
                                  "acquisition_allowed", True)}
                f_raw["expected_verdict"] = None  # 反馈轮无预先期望判决
                new_r_rows.append({
                    **f_raw, "task_id": task_id, "arm": "R",
                    "batch": "C2-locked-followup",
                    "repeat": repeat, "feedback_variant": variant["label"],
                    "parent_run_id": first.get("run_id"),
                    "version_snapshot": snap.name,
                    "version_snapshot_sha256": snap_digest,
                    "repaired_at": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"),
                    "repair_note": "问题分类缺陷修复后免费重跑",
                    "c_evaluation": c_scorer.score_row(
                        c_scorer.normalize_row(f_raw, f_contract),
                        facts=run_c._facts_for_row("R", fb))})
        print(f"  R 重跑：{task_id} × {repeats} 重复 → "
              f"参照 {references.get(task_id)}")

    # --- 2) M/A 行按修正参照与权限声明重评（不重跑、不动费用） ----------
    changed = 0
    for row in m_a_rows:
        task = by_id[row["task_id"]]
        contract = {"question_id": task.get("question_id"),
                    "method_goal": task.get("method_goal"),
                    "ceiling": tasks.get("ceiling", 6),
                    "acquisition_allowed": row.get("acquisition_allowed", True)}
        raw = dict(row)
        if row.get("feedback_variant"):
            variants = {v["label"]: v
                        for v in run_c._feedback_variants(task)}
            variant = variants.get(row["feedback_variant"]) or {}
            allowed = variant.get("acquisition_allowed", True)
            raw["acquisition_allowed"] = allowed
            contract["acquisition_allowed"] = allowed
            row["acquisition_allowed"] = allowed  # 记录字段与评分口径一致
            raw["expected_verdict"] = None
        else:
            raw["expected_verdict"] = references.get(row["task_id"])
        scored = c_scorer.score_row(
            c_scorer.normalize_row(raw, contract),
            facts=run_c._facts_for_row(row["arm"], row))
        if scored != row.get("c_evaluation"):
            changed += 1
        row["c_evaluation"] = scored
        row["rescored_note"] = ("按修正参照/权限声明重评；运行与费用不变"
                                "（repair_c2.py）")
    print(f"M/A 重评：{len(m_a_rows)} 行，{changed} 行评分变化")

    # --- 3) 组装并写回 ------------------------------------------------
    order = {"R": 0, "M": 1, "A": 2}
    merged = sorted(
        new_r_rows + m_a_rows,
        key=lambda r: (r["task_id"], r.get("repeat", 1),
                       order.get(r["arm"], 9),
                       0 if not r.get("feedback_variant") else 1))
    record = {
        "repaired_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "reason": "runner 参照缺陷与反馈权限声明不一致（详见文件头说明）",
        "r_reference_rows_rerun": len(new_r_rows),
        "m_a_rows_rescored": len(m_a_rows),
        "m_a_scores_changed": changed,
        "version_snapshot": snap.name,
        "version_snapshot_sha256": snap_digest,
        "references": references,
        "m_a_runs_and_cost_unchanged": True,
    }
    out = {
        "schema": before.get("schema", "c-batch-results/v1"),
        "protocol": before.get("protocol"),
        "scorer": "main/c_scorer.py",
        "note": ("C 批次结果：C1 v2 正式批次 27 行 + C2 冻结批次 72 行"
                 "（R 参照行经 repair_c2.py 免费重跑并绑定 c2-locked-v3；"
                 "M/A 运行与费用未动、按修正参照重评）。"),
        "runs": c1_rows + merged,
        "c2_repair": record,
    }
    if dry_run:
        print("[dry-run] 不写回")
    else:
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2,
                                  default=str))
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(
            {**record,
             "before_references_actual_v2": {
                 "C2-T1": ["single_station_only"],
                 "C2-T2": ["coherent_multistation"],
                 "C2-T3": ["coherent_multistation"],
                 "C2-T4": ["single_station_only"],
             },
             "defect": ("R 行 mode=full 时 run_review 用 case['question_id']"
                        "（案例预置问题），忽略任务声明的问题文本；C2-T3/T4 "
                        "复用 E4/Q4 时窗问方法/仪器问题时，R 答成传播问题，"
                        "M/A 被拿错误参照判错交付。修复：_row_for_r 传 "
                        "user_task，按所问问题分类 + full 取证。"),
             "second_feedback_contract_fix": (
                 "反馈指令含“不要重新取数”的任务补声明 "
                 "acquisition_allowed=false；_feedback_variants 单变体分支也"
                 "读取该声明，否则照指令不取数的 A 被误判策略不足。"),
             "before_references": {
                t: sorted({str(r.get("expected_verdict")) for r in old_r_rows
                           if r["task_id"] == t}) for t in by_id}},
            ensure_ascii=False, indent=2, default=str))
        print(f"写回 {OUT.relative_to(ROOT)}（{len(out['runs'])} 行）")
        print(f"修复记录 → {EVIDENCE.relative_to(ROOT)}")
    return record


if __name__ == "__main__":
    repair(dry_run="--dry-run" in sys.argv)