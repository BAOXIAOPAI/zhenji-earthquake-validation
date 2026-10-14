"""C 批次 runner（review-5 指南 §2/§3）。

与旧 G2 runner（main/run_g2.py）的关系：互不改写。旧 G2 的成绩与
revision-1 输出原样保留，只作回归或评分器诊断；本 runner 只写新目录
`delivery/revision-5/C-RESULTS.json`，评分用多维口径（main/c_scorer.py），
任务合同来自 main/c_tasks.json + delivery/revision-5/C0-共同合同.md。

子命令：
  run_c.py version            写 write-once 版本快照（无付费）
  run_c.py counterexamples    跑 C0 无付费评分反例（无付费）
  run_c.py dev [TASK ...]     C1 开发比较（付费；先过 C0 出口与预算守卫）
  run_c.py plan               打印任务/合同/预算状态（无付费）

付费入口的三道守卫（缺一不开跑）：
  1. C0 出口：反例报告全部通过（delivery/revision-5/evidence/C0评分反例.json）；
  2. 版本快照：本次批次的 write-once 快照已存在且被运行行引用；
  3. 预算：台账剩余 ≥ 本批次声明的预留上界；台账 halted 时一律停止。
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import core  # noqa: E402
import c_scorer  # noqa: E402
import prepare_agent  # noqa: E402
import review  # noqa: E402
import run_agent_arm  # noqa: E402
import runlog  # noqa: E402
import task as taskmod  # noqa: E402

TASKS = json.loads((ROOT / "main/c_tasks.json").read_text())
REV5 = ROOT / "delivery/revision-5"
OUT = REV5 / "C-RESULTS.json"
VERSIONS_DIR = REV5 / "C-VERSIONS"
COUNTEREXAMPLE_REPORT = REV5 / "evidence/C0评分反例.json"
CONTRACT_DOC = REV5 / "C0-共同合同.md"

# write-once 快照覆盖的对象（指南 §4：run_agent_arm、MCP、core、task、
# prepare、review、agent/one-shot 提示构造、tools 声明、评分器、模型参数、
# 输入材料）。
VERSION_SOURCES = [
    "main/run_agent_arm.py", "main/mcp_server.py", "main/core.py",
    "main/task.py", "main/prepare_agent.py", "main/review.py",
    "main/agent_run.mjs", "main/one_shot.mjs", "main/tools.json",
    "main/c_scorer.py", "main/c_counterexamples.py", "main/run_c.py",
    "main/c_tasks.json",
]


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version_snapshot(label: str = "c-batch") -> Path:
    """写 write-once 版本快照；同 label 已存在则返回原文件（不回填）。"""
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = VERSIONS_DIR / f"{label}-{stamp}.json"
    existing = sorted(VERSIONS_DIR.glob(f"{label}-*.json"))
    if existing:
        return existing[-1]
    digest = {rel: _sha256(ROOT / rel) for rel in VERSION_SOURCES}
    digest["delivery/revision-5/C0-共同合同.md"] = _sha256(CONTRACT_DOC)
    snapshot = {
        "schema": "c-batch-version-snapshot/v1",
        "label": label,
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
        "write_once": True,
        "source_sha256": digest,
        "model": {"name": os.environ.get("AGENT_MODEL", "deepseek-flash"),
                  "provider": "deepseek",
                  "variant": os.environ.get("AGENT_VARIANT", "open")},
        "note": "执行前写入；运行行引用本文件摘要。代码变化产生新版本文件，"
                "不回填历史（事实更正表第1条：旧paid烟测的追溯缺口不在此修复）",
    }
    runlog.write_once(path, snapshot)
    return path


def c0_exit_ready() -> tuple[bool, str]:
    """C0 出口核对：反例报告全部通过。"""
    if not COUNTEREXAMPLE_REPORT.exists():
        return False, f"缺少反例报告：{COUNTEREXAMPLE_REPORT}"
    report = json.loads(COUNTEREXAMPLE_REPORT.read_text())
    summary = report.get("summary") or {}
    if not summary.get("all_pass"):
        return False, "反例报告存在未通过项"
    old = report.get("old_scorer_counterexamples_rerun") or {}
    if old.get("available") and not old.get("all_pass"):
        return False, "旧反例复跑存在未通过项"
    return True, "反例报告全部通过"


def budget_state(reserve_cny: float | None = None) -> dict:
    """台账状态（只读）；余量不足或 halted 时付费入口拒绝开跑。

    `reserve_cny` 覆盖任务文件的默认预留（C2 冻结批次用 3 元预留）。
    """
    ledger = runlog.LEDGER
    if not ledger.exists():
        return {"available": False, "halted": True, "total_cny": None,
                "remaining_cny": None,
                "why": "台账文件不存在"}
    data = json.loads(ledger.read_text())
    total = sum((e.get("cost_upper_cny")
                 or e.get("reserved_cny") or 0)
                for e in data.get("entries", []))
    cap = TASKS.get("budget", {}).get("cumulative_authorization_cny", 20.0)
    reserve = (reserve_cny if reserve_cny is not None else
               TASKS.get("budget", {}).get("c1_dev_reserve_cny", 2.0))
    remaining = round(cap - total, 6)
    return {"available": (not data.get("halted")) and remaining >= reserve,
            "halted": bool(data.get("halted")), "total_cny": round(total, 6),
            "remaining_cny": remaining, "reserve_cny": reserve,
            "entries": len(data.get("entries", []))}


# ---------------------------------------------------------------- 三个臂

def _row_for_r(task, question=None, method_goal=None):
    """R 臂：合理确定性流程。

    用 full 模式（取满许可候选再判）：Q_usable 的答案存在“部分适用”，
    第一个失败台站就停会把窗口级问题答成单站结论；coincidence 判定也
    需要完整触发集合。R 因此拿到与 M 相同的完整证据（隔离“规则判断 vs
    模型判断”），A 的差异则只剩按需取证策略——指南 §2.2 的三组定义。
    """
    case_id = task["case_id"]
    question = question or task["question"]
    goal = method_goal or task.get("method_goal")
    # 关键：必须按**任务声明的问题**回答，而不是案例的预置问题。
    # run_review 只在 mode=="question" 时按问题文本分类；mode=="full"
    # 会退回 case["question_id"]。C2 复用 E4/Q4 时窗问其它问题，若走
    # case 预置问题，R 会答错问题并成为错误参照（首次 v2 批次即此缺陷）。
    # 传 user_task 走"按所问问题分类 + full 取证"的路径。
    case = core.case_by_id(case_id)
    frozen = taskmod.task_from_case(
        case, user_question=question, method_goal=goal,
        operational_standard=task.get("operational_standard"))
    taskmod.freeze(frozen)
    return review.run_review(case_id, question, "full",
                             user_task=frozen,
                             method_goal=goal,
                             operational_standard=task.get(
                                 "operational_standard"))


def _row_for_m(task, question=None, method_goal=None):
    """M 臂：全证据单次判断（采集在模型介入前完成）。"""
    case_id = task["case_id"]
    question = question or task["question"]
    goal = method_goal or task.get("method_goal")
    return run_agent_arm.run_one_shot(
        case_id, question, method_goal=goal,
        operational_standard=task.get("operational_standard"))


def _row_for_a(task, question=None, method_goal=None):
    """A 臂：AGH 按需取证（候选/工具/上限与 R/M 相同）。"""
    case_id = task["case_id"]
    question = question or task["question"]
    goal = method_goal or task.get("method_goal")
    return run_agent_arm.run_one(
        case_id, question, method_goal=goal,
        operational_standard=task.get("operational_standard"))


ARM_RUNNERS = {"R": _row_for_r, "M": _row_for_m, "A": _row_for_a}


def _derived_evidence_for(folder: Path, records: list) -> dict:
    """M 臂的本地派生证据：委托 run_agent_arm.derived_evidence 的唯一实现。

    反馈轮与首轮必须给出同一套证据面（review-5 §2.2：逐站簇、detectors、
    alignment 与限制），否则 M 的材料会随入口不同而缩水。
    """
    return run_agent_arm.derived_evidence(folder, records)


def _analysis_waveform(folder: Path, candidate_id: str):
    for kind in ("derived", "raw"):
        path = folder / kind / candidate_id / "waveform.mseed"
        if path.exists():
            return path
    return None


def _m_followup(parent_run_id: str, instruction: str,
                method_goal=None, change=None):
    """M 臂的反馈轮：结转材料 + 单次判断（与 A 同模型同验证器）。

    与 R/A 相同的反馈语义：用户的变更（排除/恢复/网格/goal）必须**实际应用**
    到结转观测上，验证器按应用后的条件判决。v1 排查批次发现这里曾只把
    change 存进任务而没有应用到记录，导致 T2/T3 的 M 反馈交付停留在变更前
    的判决——那是 runner 合同缺口，不是模型行为，已按指南“先修无付费入口”
    修正并整批重跑（旧批次保留为 troubleshooting）。
    """
    parent_dir = runlog.run_dir(parent_run_id)
    parent_state = json.loads((parent_dir / "mcp_state.json").read_text())
    # 结构化变更先走同一套规范化（站点解析/校验/自由文字兜底）。
    change = review.feedback_change(parent_state, change, instruction)
    prepared = prepare_agent.prepare_followup(
        parent_run_id, instruction, arm="one_shot_model_followup",
        method_goal=method_goal, change=change)
    folder, persisted = _apply_feedback_change(
        prepared, change, instruction, parent_run_id)
    persisted["derived_evidence"] = _derived_evidence_for(
        folder, [r for r in persisted["acquired"]
                 if not r.get("excluded")])
    persisted["follow_up"] = instruction
    prepare_agent.write_state(folder, persisted)
    parent_task = json.loads((parent_dir / "task.json").read_text())
    question = parent_task["user_question"]
    case = taskmod.case_for_task(json.loads((folder / "task.json").read_text()))
    result = run_agent_arm._execute(
        case["case_id"], prepared, core.classify_question(question),
        question, driver_script="one_shot.mjs")
    result.setdefault("feedback", {})["applied_change"] = persisted.get(
        "change_applied")
    return result


def _apply_feedback_change(prepared: dict, change: dict, instruction: str,
                           parent_run_id: str):
    """把结构化反馈实际应用到结转观测并持久化（R/A/M 同一语义）。

    返回 (folder, persisted_state)：验证器、费用统计与模型证据读同一份
    应用后的状态。
    """
    folder = runlog.run_dir(prepared["run_id"])
    frozen = json.loads((folder / "task.json").read_text())
    state = json.loads((folder / "mcp_state.json").read_text())
    parent_state = json.loads(
        (runlog.run_dir(parent_run_id) / "mcp_state.json").read_text())
    state.update({"run_id": prepared["run_id"], "task": frozen,
                  "task_id": frozen["task_id"],
                  "records": copy.deepcopy(state.get("acquired")
                                           or parent_state["acquired"])})
    recomputed, detail = review._apply_change(change, state, folder)
    discovery = json.loads((folder / "discovery.json").read_text())
    persisted = prepare_agent.execution_state(
        prepared["run_id"], frozen, discovery,
        acquired=state["records"], parent_run_id=parent_run_id,
        extra={"follow_up": instruction, "change": change,
               "exclusions": state.get("exclusions"),
               "carried_forward": [r["candidate_id"]
                                    for r in parent_state["acquired"]],
               "requests": 0, "http_attempts": 0,
               "logical_acquisitions": 0, "cache_hits": 0, "bytes": 0})
    persisted["change_applied"] = detail
    persisted["recomputed"] = recomputed
    return folder, persisted


def _feedback_variants(task) -> list:
    """任务的反馈变体（提前固定，三组同文）。单分支或 T3 的双分支声明。"""
    fb = task.get("feedback") or {}
    if not fb:
        return []
    if "branch_a" in fb or "branch_b" in fb:
        out = []
        for key in ("branch_a", "branch_b"):
            v = fb.get(key)
            if v:
                out.append({"label": key, "instruction": v["instruction"],
                            "change": v.get("change"),
                            "acquisition_allowed":
                                v.get("acquisition_allowed", True)})
        return out
    # 单变体反馈同样读合同声明的采集许可（C2：指令写“不要重新取数”的
    # 任务必须声明 acquisition_allowed=false，否则照指令不取数会被误判）。
    return [{"label": "followup", "instruction": fb.get("instruction"),
             "change": fb.get("change"),
             "acquisition_allowed": fb.get("acquisition_allowed", True)}]


def run_feedback(task, arm: str, parent_run_id: str, variant: dict):
    """按任务合同执行一个反馈变体：三组同一用户反馈文本。

    T2 的排除对象在运行时从父 run 的交付解析（“第一个有通道异常的台站”
    ——各组排除它自己发现的那台；反馈文本三组同文）。
    """
    instruction = variant["instruction"]
    change = dict(variant.get("change") or {})
    # The contract's acquisition permission rides INSIDE the change so every arm
    # (R/M/A) applies the same user condition; the reuse path defaults to denying
    # new fetches when the task declared `acquisition_allowed: false`.
    change["acquisition_allowed"] = bool(variant.get("acquisition_allowed", True))
    if change.get("kind") == "exclude" and not change.get("station"):
        flagged = _first_flagged_station(parent_run_id)
        if flagged:
            change["station"] = flagged
    goal = (change or {}).get("method_goal") \
        if (change or {}).get("kind") == "method_goal" else None
    if arm == "R":
        return review.run_followup(parent_run_id, change=change,
                                   instruction=instruction)
    if arm == "M":
        return _m_followup(parent_run_id, instruction,
                           method_goal=goal, change=change)
    if arm == "A":
        return run_agent_arm.run_followup(parent_run_id, instruction,
                                          method_goal=goal, change=change)
    raise ValueError(f"未知组：{arm}")


def _first_flagged_station(run_id: str):
    """反馈轮排除对象的运行时解析（只读结构化记录）。

    优先：范围内第一个带通道异常标记的台站（C1-T2 语义）。
    兜底：没有异常标记时取最近的台站（距离最小；C2 真实窗的“排除最近
    一台”反馈）。两个规则都确定且三组同文——各组排除它自己交付范围内的
    那台。
    """
    try:
        state = json.loads(
            (runlog.run_dir(run_id) / "mcp_state.json").read_text())
        acquired = [r for r in (state.get("acquired") or [])
                    if not r.get("excluded")]
        for rec in acquired:
            if rec.get("channel_integrity_flags"):
                return rec.get("station")
        with_distance = [r for r in acquired
                         if r.get("distance_km") is not None]
        if with_distance:
            return min(with_distance,
                       key=lambda r: r["distance_km"]).get("station")
    except (OSError, ValueError):
        return None
    return None


# ---------------------------------------------------------------- 行规范化

def cmd_rescore() -> int:
    """对已保存的 C-RESULTS.json 行重跑确定性评分（不新增模型调用）。

    评分器是纯函数：运行与费用不变，评分可基于已修复的规则重算。v2
    批次后发现事实核对把“数词子集指称”（两个健康台 vs 1 台清单）误当
    全称断言，按指南“先修无付费入口”修正后用本命令重评（批次头记录
    重评依据）。
    """
    if not OUT.exists():
        print("没有可重评的 C-RESULTS.json", file=sys.stderr)
        return 2
    data = json.loads(OUT.read_text())
    tasks = {t["id"]: t for t in TASKS["tasks"]}
    changed = 0
    for row in data.get("runs", []):
        task = tasks.get(row.get("task_id"), {})
        contract = {
            "question_id": row.get("question_id") or task.get("question_id"),
            "method_goal": task.get("method_goal"),
            "ceiling": TASKS.get("ceiling", 6),
            "acquisition_allowed": row.get("acquisition_allowed", True),
        }
        raw = dict(row)
        if task.get("expected_first_verdict") \
                and not row.get("feedback_variant"):
            raw.setdefault("expected_verdict",
                           task["expected_first_verdict"])
        if task.get("expected_feedback_verdict") \
                and row.get("feedback_variant"):
            raw.setdefault("expected_verdict",
                           task["expected_feedback_verdict"])
        norm = c_scorer.normalize_row(raw, contract)
        facts = _facts_for_row(row.get("arm", ""), row)
        scored = c_scorer.score_row(norm, facts=facts)
        if row.get("c_evaluation") != scored:
            changed += 1
        row["c_evaluation"] = scored
    data["rescored_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    data["rescore_note"] = (
        "评分器修复后确定性重评（运行与费用不变）：事实核对的数词子集"
        "指称不再误当全称断言（tests/test_c1_fixes.py 锁定）。")
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                              default=str))
    print(f"重评完成：{len(data.get('runs', []))} 行，其中 "
          f"{changed} 行评分变化 → {OUT.relative_to(ROOT)}")
    return 0


def _raw_row(arm: str, res: dict) -> dict:
    """把臂的运行结果折成评分器可规范化的 row（评分器的统一入口）。"""
    if arm == "R":
        answered = res.get("answered") or {}
        layers = res.get("layers") or {}
        # 反馈轮的结果不带 n_eligible：从该 run 自己的 discovery 读
        # （反馈轮 discovery 复用父轮元数据，不新增请求）。
        n_eligible = res.get("n_eligible")
        if n_eligible is None and res.get("run_id"):
            try:
                n_eligible = json.loads(
                    (runlog.run_dir(res["run_id"]) / "discovery.json")
                    .read_text()).get("n_eligible")
            except (OSError, ValueError):
                n_eligible = None
        # analysed 计数包括已排除的观测：数据可得性与参与判定是两件事
        # （T3 反馈：唯一观测被用户排除 ≠ 服务无数据）。
        stations = res.get("stations") or []
        stations_analysed = sum(
            1 for s in stations if (s.get("status") == "analysed"))
        excluded = layers.get("excluded_candidates") or []
        return {
            "question_id": res.get("question_id"),
            "status": res.get("status"),
            "failure_kind": res.get("failure_kind"),
            "verdict": res.get("verdict"),
            "reason": res.get("reason"),
            "field_value": res.get("field_value"),
            "coincidence": res.get("coincidence"),
            "n_eligible": n_eligible,
            "excluded_candidates": excluded,
            "stations_acquired": res.get("stations_acquired"),
            "stations_analysed": (stations_analysed
                                  if stations else
                                  res.get("stations_analysed")),
            "terminated_by": res.get("terminated_by"),
            "field_answered": res.get("field_answered"),
            "answered": answered,
            "model_stop_reason": None,
            "claim": res.get("verdict"),  # R 的“主张”即规则自己的答案
            "delivered": {
                "verdict": res.get("verdict"),
                "field": (answered.get("field")
                          or res.get("field_answered")),
                "field_value": answered.get("value"),
                "answer_kind": answered.get("answer_kind"),
                "value": answered.get("value") if answered.get("field")
                         else res.get("verdict"),
                "task_completed": bool(answered.get("answered")),
                "included_candidates": layers.get("included_candidates"),
                "excluded_candidates": excluded,
            },
            "http_attempts": res.get("http_attempts"),
            "logical_acquisitions": res.get("logical_acquisitions"),
            "model_cost_known": res.get("model_cost_known"),
            "model_cost_cny": res.get("model_cost_cny"),
            "parent_run_id": res.get("parent_run_id"),
            "run_id": res.get("run_id"),
            "arm": arm,
        }
    # M / A：run_agent_arm 的行已经携带 acquisition/model_claim/delivered。
    return {**res, "arm": arm}


def _facts_for_row(arm: str, res: dict) -> list:
    """从运行记录提取解释事实清单（逐站稳定性）。

    R 行的 layers.robustness；M/A 行走 run 目录的 stability_checks。
    只读结构化材料，不重算。
    """
    run_id = res.get("run_id")
    if run_id:
        folder = runlog.run_dir(run_id)
        try:
            state = json.loads((folder / "mcp_state.json").read_text())
            checks = state.get("stability_checks") or {}
            return [{"kind": "station_stability",
                     "station": r.get("station"),
                     "state": r.get("stability")}
                    for r in checks.values() if r.get("station")]
        except (OSError, ValueError):
            pass
    layers = res.get("layers") or {}
    by_station = (layers.get("robustness") or {}).get("by_station") or {}
    return [{"kind": "station_stability", "station": st,
             "state": (v or {}).get("stability")}
            for st, v in by_station.items()]


# ---------------------------------------------------------------- 主入口

def _append_result(results: dict, row: dict) -> None:
    results.setdefault("runs", []).append(row)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2,
                              default=str))


def cmd_version() -> int:
    path = version_snapshot()
    print(f"版本快照：{path.relative_to(ROOT)}")
    return 0


def cmd_counterexamples() -> int:
    script = REV5 / "evidence/C0评分反例验证.py"
    proc = subprocess.run([sys.executable, str(script)], cwd=ROOT)
    return proc.returncode


def cmd_plan() -> int:
    tasks_file = Path(os.environ.get("EQ_C_TASKS") or
                      (ROOT / "main/c_tasks.json"))
    tasks_data = json.loads(tasks_file.read_text())
    label = os.environ.get("C1_LABEL", "c1-dev")
    print(f"任务合同：{tasks_file}"
          f"（{len(tasks_data.get('tasks', []))} 个任务，"
          f"批次 {label}）")
    for t in tasks_data.get("tasks", []):
        repeats = t.get("repeats") or tasks_data.get("repeats") or 1
        print(f"  {t['id']} {t['title']}（case={t['case_id']}，"
              f"question_id={t['question_id']}，重复 {repeats}）")
    ok, why = c0_exit_ready()
    print(f"C0 出口：{'就绪' if ok else '未就绪'}（{why}）")
    reserve_key = ("c2_locked_reserve_cny" if label.startswith("c2")
                   else "c1_dev_reserve_cny")
    reserve = (tasks_data.get("budget") or {}).get(reserve_key, 2.0)
    budget = budget_state(reserve_cny=reserve)
    print(f"预算：累计 {budget.get('total_cny')} 元，"
          f"剩余 {budget.get('remaining_cny')} 元，"
          f"本批预留 {budget.get('reserve_cny')} 元，"
          f"halted={budget.get('halted')}")
    print(f"输出路径：{OUT.relative_to(ROOT)}（绝不写 revision-1/G2-RESULTS）")
    return 0


def cmd_dev(only: list[str] | None = None) -> int:
    # 任务合同文件可用 EQ_C_TASKS 指定（C2 冻结批次用 main/c2_tasks.json）。
    tasks_file = Path(os.environ.get("EQ_C_TASKS") or
                      (ROOT / "main/c_tasks.json"))
    tasks_data = json.loads(tasks_file.read_text())
    # 守卫 1：C0 出口。
    ok, why = c0_exit_ready()
    if not ok:
        print(f"拒绝开跑：C0 出口未达标（{why}）", file=sys.stderr)
        return 2
    # 守卫 2：预算（余量不足或 halted 时拒绝，且不产生版本快照文件）。
    # 预留量按批次标签取：c2* 用冻结批次预留，其余用 C1 开发预留。
    label = os.environ.get("C1_LABEL", "c1-dev")
    reserve_key = ("c2_locked_reserve_cny" if label.startswith("c2")
                   else "c1_dev_reserve_cny")
    budget = budget_state(reserve_cny=(tasks_data.get("budget") or {}).get(
        reserve_key, 2.0))
    if not budget.get("available"):
        print(f"拒绝开跑：预算守卫（halted={budget.get('halted')}，"
              f"剩余 {budget.get('remaining_cny')} 元，"
              f"需要 {budget.get('reserve_cny')} 元）", file=sys.stderr)
        return 2
    # 守卫 3：版本快照（通过前两道守卫才写 write-once 快照）。
    # 批次标签可用 C1_LABEL 环境变量区分（v1 排查批次后的修复批次用
    # c1-dev-v2：write-once 语义下同标签返回旧快照，而修复后的代码
    # 必须绑定新快照）。
    snap = version_snapshot(label)
    snap_digest = _sha256(snap)
    core.fdsn.set_cache(core.CACHE)

    batch_name = ("C2-locked" if label.startswith("c2") else "C1-development")
    results = (json.loads(OUT.read_text())
               if OUT.exists() else {
                   "schema": "c-batch-results/v1",
                   "protocol": "delivery/revision-5/C0-共同合同.md",
                   "version_snapshot": snap.name,
                   "version_snapshot_sha256": snap_digest,
                   "scorer": "main/c_scorer.py",
                   "batch": batch_name,
                   "runs": []})
    tasks = [t for t in tasks_data["tasks"]
             if not only or t["id"] in only]
    # C2 参照基准：任务声明 expected_verdict_source 时，R 首轮（free、
    # 确定性 full 模式）作为该任务的期望判决来源；不看 M/A 结果再定。
    reference = {}
    for task in tasks:
        repeats = task.get("repeats") or tasks_data.get("repeats") or 1
        for repeat in range(1, repeats + 1):
            for arm in tasks_data["arms"]:
                began = time.time()
                print(f"[{task['id']}/{repeat}] {arm} …", flush=True)
                try:
                    res = ARM_RUNNERS[arm](task)
                except Exception as exc:  # noqa: BLE001
                    res = {"status": "failed",
                           "failure_kind": "runner_exception",
                           "reason": f"{type(exc).__name__}: {exc}"[:200],
                           "run_id": None}
                raw = _raw_row(arm, res)
                contract = {"question_id": task.get("question_id"),
                            "method_goal": task.get("method_goal"),
                            "ceiling": tasks_data.get("ceiling", 6),
                            "version": snap_digest}
                # 任务合同声明的期望判决（提前固定，不看结果再定）：错误
                # 交付维度的对照基准。C2 声明 r_full_evidence 时以 R 首轮
                # 确定性判决为参照（合同里先声明，M/A 之前先跑 R）。
                if task.get("expected_first_verdict"):
                    raw["expected_verdict"] = task["expected_first_verdict"]
                elif (tasks_data.get("expected_verdict_source")
                        == "r_full_evidence_first_round"):
                    if arm == "R" and task["id"] not in reference:
                        reference[task["id"]] = res.get("verdict") \
                            if res.get("status") == "succeeded" else None
                    if arm != "R" or repeat > 1:
                        raw["expected_verdict"] = reference.get(
                            task["id"])
                norm = c_scorer.normalize_row(raw, contract)
                facts = _facts_for_row(arm, res)
                scored = c_scorer.score_row(norm, facts=facts)
                row = {**raw, "task_id": task["id"], "arm": arm,
                       "batch": batch_name,
                       "repeat": repeat,
                       "version_snapshot": snap.name,
                       "version_snapshot_sha256": snap_digest,
                       "elapsed_s": round(time.time() - began, 1),
                       "c_evaluation": scored}
                _append_result(results, row)
                print(f"    → {scored['primary']}（run {res.get('run_id')}，"
                      f"{row['elapsed_s']}s）", flush=True)
                # 反馈变体（一次，不随重复放大；T3 的双分支各自成行）。
                for variant in _feedback_variants(task):
                    if not res.get("run_id"):
                        continue
                    began = time.time()
                    print(f"[{task['id']}] {arm} 反馈（{variant['label']}）…",
                          flush=True)
                    try:
                        f_res = run_feedback(task, arm, res["run_id"], variant)
                    except Exception as exc:  # noqa: BLE001
                        f_res = {"status": "failed",
                                 "failure_kind": "runner_exception",
                                 "reason": f"{type(exc).__name__}: {exc}"[:200],
                                 "parent_run_id": res.get("run_id"),
                                 "run_id": None}
                    f_raw = _raw_row(arm, f_res)
                    # 分支声明的采集许可进入规范 row（T3-A 只复用 vs T3-B 允许补取）。
                    f_raw["acquisition_allowed"] = variant.get(
                        "acquisition_allowed", True)
                    f_contract = {**contract,
                                  "acquisition_allowed": variant.get(
                                      "acquisition_allowed", True)}
                    if task.get("expected_feedback_verdict"):
                        f_raw["expected_verdict"] = task[
                            "expected_feedback_verdict"]
                    f_norm = c_scorer.normalize_row(f_raw, f_contract)
                    f_scored = c_scorer.score_row(f_norm,
                                                  facts=_facts_for_row(arm, f_res))
                    f_row = {**f_raw, "task_id": task["id"], "arm": arm,
                             "batch": batch_name + "-followup",
                         "repeat": repeat,
                             "feedback_variant": variant["label"],
                             "parent_run_id": res.get("run_id"),
                             "version_snapshot": snap.name,
                             "version_snapshot_sha256": snap_digest,
                             "elapsed_s": round(time.time() - began, 1),
                             "c_evaluation": f_scored}
                    _append_result(results, f_row)
                    print(f"    → 反馈（{variant['label']}）"
                          f"{f_scored['primary']}"
                          f"（run {f_res.get('run_id')}）", flush=True)
    try:
        shown = OUT.relative_to(ROOT)
    except ValueError:  # 测试把 OUT 指向临时目录时也要能打印
        shown = OUT
    print(f"\n写出 {shown}（{len(results['runs'])} 行）")
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "plan"
    if cmd == "version":
        return cmd_version()
    if cmd == "counterexamples":
        return cmd_counterexamples()
    if cmd == "plan":
        return cmd_plan()
    if cmd == "dev":
        return cmd_dev(args[1:] or None)
    if cmd == "rescore":
        return cmd_rescore()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
