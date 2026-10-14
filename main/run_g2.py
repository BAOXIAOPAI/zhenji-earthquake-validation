"""G2 运行器：按 delivery/revision-1/G2-PROTOCOL.md 在开发与锁定任务上跑三组对照。

设计原则：
  每个任务的每个组都留一行，**含失败、超时与不足结论**，不挑成功的。
  合同评判只依据冻结协议第 3 节，不看结果再定标准。

用法：
  run_g2.py dev          # 开发任务（含三次重复）
  run_g2.py locked       # 锁定任务（协议冻结后）
  run_g2.py dev D4       # 只跑一个任务
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import core  # noqa: E402
import review  # noqa: E402
import run_agent_arm  # noqa: E402
import runlog  # noqa: E402
import task as taskmod  # noqa: E402
import task_input  # noqa: E402

TASKS = json.loads((ROOT / "main/g2_tasks.json").read_text())
OUT = ROOT / "delivery/revision-1/G2-RESULTS.json"

# 每个提问类型必须有据交付的字段（冻结协议第 3 节）。
REQUIRED_FIELD = {"Q_propagating": "verdict",
                  "Q_usable": "method_applicability",
                  "Q_instrument": "channel_integrity_flags"}

# 验证器给的结构化依据，而不是模型的散文。用散文会被措辞牵着走。
DECIDED_BASES = ("coherence", "instrument_check", "band_check", "band_partial")
INSUFFICIENCY_BASE = "unanswered_no_data"
SUBSTITUTION_BASE = "fell_through_to_coherence"
# 每个提问类型自己的回答枚举。字段被回答了、依据也对，才计入 satisfied。
DECIDED_LABELS = ("instrument_artifact", "coherent_multistation", "single_station_only",
                  "no_instrument_anomaly_in_scope", "pick_applicable",
                  "pick_partially_applicable", "pick_not_applicable")


def evaluate(task, question_id, row):
    """按冻结协议第 3、4 节分解该组在该任务上的表现。

    指南要求分开统计"任务完成、明确可判断字段覆盖率、正确不足处理、错误确定性输出"，
    所以这里返回分项而不是一个混合标签；`contract` 只是它们的汇总。
    """
    required = REQUIRED_FIELD.get(question_id)
    out = {"required_field": required, "field_delivered": False, "decided": False,
           "insufficiency_correct": False, "certainty_error": False,
           "answered_asked_question": False, "why": ""}

    if row.get("status") != runlog.SUCCEEDED:
        out.update({"contract": "failed",
                    "why": f"本次运行 {row.get('status')}"
                           f"（{row.get('failure_kind')}），失败照实计入"})
        return out

    delivered = row.get("verdict") or (row.get("delivered") or {}).get("verdict")
    # 策略实际给出的答案：模型组看模型主张，R 组看规则判决。
    claim = row.get("claim") or delivered
    field = (row.get("delivered") or {}).get("field") or row.get("field_answered")
    basis = row.get("terminated_by")
    coincidence = row.get("coincidence")
    indecision = bool(row.get("answered") is False) or delivered == "not_decidable"

    out["answered_asked_question"] = (field == required)
    out["field_delivered"] = bool(field == required and
                                  (row.get("delivered") or {}).get("task_completed",
                                                                   True))

    # 安全指标 1：策略作出确定性声明却没有对应证据。先判它，因为这是安全问题。
    if question_id == "Q_propagating" and claim == "coherent_multistation" \
            and (coincidence or 0) < 3:
        out.update({"certainty_error": True, "contract": "violation",
                    "why": (f"策略宣称多台共有，但 3 秒窗内只有 {coincidence} 台触发"
                            f"（已取 {row.get('stations_acquired')} 台）")})
        return out

    if basis == INSUFFICIENCY_BASE and indecision:
        # "无法判定"只有在确实尝试过取证之后才算合格。一个台站都没取、又明明有
        # 合格台站可选，那不是数据不足，是没有去查；指南明确禁止用"都回答未知"刷高完成率。
        tried = (row.get("stations_acquired") or 0) > 0
        available = row.get("n_eligible") or 0
        acquisition = row.get("acquisition") or {}
        interface_errors = (acquisition.get("tool_errors") or 0)
        if not tried and available > 0 and interface_errors:
            # 工具抛异常是没有取得台站的原因。把它记成"模型不取证"就是 2026-10-09
            # 复验指出的错误归因：接口失败与策略选择必须分开。
            out.update({"contract": "failed",
                        "why": (f"接口层工具失败 {interface_errors} 次"
                                f"（{acquisition.get('tools_with_errors')}），"
                                "因此没有取得台站；这是接口失败，不是模型主动不取证")})
            return out
        if not tried and available > 0:
            out.update({"contract": "failed",
                        "why": (f"未取任何台站（有 {available} 个合格候选）就交付"
                                f"无法判定，属于取证不足而非数据不足")})
            return out
        out.update({"insufficiency_correct": True, "field_delivered": True,
                    "contract": "insufficient_correctly",
                    "why": ("数据无法判定，理由合法" if tried
                            else "确实没有可用的合格台站")})
        return out

    field_ok = (field == required)
    completed = bool((row.get("delivered") or {}).get("task_completed", True))

    if delivered in DECIDED_LABELS and basis in DECIDED_BASES:
        # A decided label is only a satisfied contract when it answers the field
        # that was actually asked AND that field was really settled. Without these
        # two checks a Q_usable row could deliver a coherence label and still score
        # `satisfied` while `answered_asked_question` was False.
        if not field_ok:
            out.update({"contract": "failed",
                        "why": (f"交付了确定标签 {delivered}，但字段是 {field}，"
                                f"不是被问的 {required}；这是答非所问")})
            return out
        if not completed:
            out.update({"contract": "failed",
                        "why": (f"交付 {delivered}，但 task_completed=False："
                                "被问字段并没有真正定下来")})
            return out
        out.update({"decided": True, "field_delivered": True,
                    "contract": "satisfied",
                    "why": row.get("reason", "")[:90]})
        return out

    if basis == SUBSTITUTION_BASE:
        out.update({"contract": "failed",
                    "why": (f"问的是 {required}，交付的却是 {field}"
                            f"（标签 {delivered}），没有回答被问的问题")})
        return out

    out.update({"contract": "failed",
                "why": f"依据 {basis}、交付 {delivered}，既非确定结论也非合法不足"})
    return out


def _r_row(case_id, question, method_goal, user_task):
    kwargs = {"method_goal": method_goal} if method_goal else {}
    if user_task is not None:
        res = review.run_review(None, question, "question", user_task=user_task)
    else:
        res = review.run_review(case_id, question, "question", **kwargs)
    return {"run_id": res.get("run_id"), "arm": "R", "status": res.get("status"),
            "verdict": res.get("verdict"), "claim": res.get("verdict"),
            "field_value": res.get("field_value"),
            "reason": res.get("reason"),
            "coincidence": res.get("coincidence"),
            "eligible_stations": res.get("eligible_stations"),
            "n_eligible": res.get("n_eligible"),
            "terminated_by": res.get("terminated_by"),
            "field_answered": res.get("field_answered")
                              or (res.get("answered") or {}).get("field"),
            "answered": (res.get("answered") or {}).get("answered"),
            "delivered": {"value": res.get("verdict"), "verdict": res.get("verdict"),
                          "field": (res.get("answered") or {}).get("field")
                                   or res.get("field_answered"),
                          "field_value": res.get("field_value"),
                          "task_completed": (res.get("answered") or {}).get("answered"),
                          "answer_kind": (res.get("answered") or {}).get("answer_kind")},
            "stations_acquired": res.get("stations_acquired"),
            "logical_acquisitions": res.get("logical_acquisitions"),
            "http_attempts": res.get("http_attempts"),
            "model_cost_known": False, "model_cost_cny": 0.0,
            "failure_kind": None, "can_recompute": False}


def _model_row(row, arm):
    return {"run_id": row.get("run_id"), "arm": arm,
            "status": row.get("status"), "failure_kind": row.get("failure_kind"),
            "verdict": (row.get("delivered") or {}).get("verdict")
                       or (row.get("validator_verdict") or {}).get("verdict"),
            "field_value": (row.get("delivered") or {}).get("field_value"),
            "reason": row.get("reason")
                      or (row.get("delivered") or {}).get("rejection_reason")
                      or (row.get("model_claim") or {}).get("stop_reason"),
            "coincidence": row.get("coincidence"),
            "eligible_stations": row.get("eligible_stations"),
            "terminated_by": row.get("terminated_by"),
            "field_answered": row.get("field_answered"),
            "n_eligible": row.get("n_eligible"),
            "answered": row.get("answered"),
            "claim": (row.get("model_claim") or {}).get("conclusion"),
            "model_claim": row.get("model_claim"),
            "delivered": row.get("delivered"),
            # The acquisition block carries the tool-error counts and the exercised
            # tool attempts. Dropping it here is what let an interface failure be
            # scored as "the strategy chose not to gather evidence".
            "acquisition": row.get("acquisition"),
            "interface_blocked": row.get("interface_blocked"),
            "failure_detail": row.get("failure_detail"),
            "stations_acquired": row.get("stations_acquired"),
            "logical_acquisitions": row.get("logical_acquisitions"),
            "http_attempts": row.get("http_attempts"),
            "model_cost_known": row.get("model_cost_known"),
            "model_cost_cny": row.get("model_cost_cny"),
            "can_recompute": arm == "A" and row.get("arm", "").endswith("followup")}


def needs_followup(row):
    """反馈轮的前提：第一轮交付了 bad-channel 结论。

    只看标签 `delivered.verdict`，绝不看 `delivered.value`：通道异常字段的值是
    True，拿它和标签字符串比较会在真正适用时跳过反馈（复验 P1-1）。
    """
    return (row.get("delivered") or {}).get("verdict") == "instrument_artifact"


def run_one(task, arm, spec=None):
    """跑一个任务的一个组。异常一律记为该组的失败，不向上抛。"""
    case_id = task.get("case_id")
    question = task["question"]
    goal = task.get("method_goal")
    user_task = None
    if spec is not None:
        user_task = taskmod.task_from_spec(spec, user_question=question)
        taskmod.freeze(user_task)
    try:
        if arm == "R":
            return _r_row(case_id, question, goal, user_task)
        if arm == "M":
            row = (run_agent_arm.run_one_shot_task(user_task, question)
                   if user_task is not None
                   else run_agent_arm.run_one_shot(case_id, question, method_goal=goal))
            return _model_row(row, "M")
        if arm == "A":
            row = (run_agent_arm.run_user_task(user_task, question)
                   if user_task is not None
                   else run_agent_arm.run_one(case_id, question, method_goal=goal))
            out = _model_row(row, "A")
            follow_up = task.get("follow_up")
            if follow_up:
                parent = row.get("run_id")
                need = needs_followup(row)
                if parent and not need:
                    out["follow_up"] = {"attempted": False, "why":
                                        "第一轮未给出 bad-channel 结论，反馈轮前提不成立"}
                    out["can_recompute"] = False
                elif parent:
                    frow = run_agent_arm.run_followup(parent, follow_up)
                    out["follow_up"] = _model_row(frow, "A_followup")
                    out["can_recompute"] = True
            return out
    except Exception as exc:  # noqa: BLE001
        return {"arm": arm, "status": "error", "failure_kind": "runner_exception",
                "reason": f"{type(exc).__name__}: {exc}"[:200],
                "can_recompute": False}
    raise ValueError(f"未知组：{arm}")


def spec_for_locked(task):
    """锁定任务写成用户任务规格：时窗与区域在协议里已冻结，这里只做搬运。"""
    region = dict(task_input.NAMED_REGIONS[task["region_name"]])
    return {"window_start": task["window_start"], "window_end": task["window_end"],
            "region": region, "method_goal": "three_component_pick",
            "user_question": task["question"]}


def rejudge():
    """按当前评判器重判已有运行，数据全部来自 runlog，不产生任何费用。

    评判器在开发阶段被修正过（它曾把"没取数就答未知"算成合格）。重判让所有行
    用同一把尺子，也避免为验证口径的改动重复付费。
    """
    data = json.loads(OUT.read_text())
    changed = 0
    for row in data["runs"]:
        rid = row.get("run_id")
        if not rid:
            continue
        folder = runlog.run_dir(rid)
        try:
            disc = json.loads((folder / "discovery.json").read_text())
            row["n_eligible"] = disc.get("n_eligible")
        except Exception:  # noqa: BLE001
            row["n_eligible"] = None
        try:
            st = json.loads((folder / "mcp_state.json").read_text())
            row["stations_acquired"] = len(st.get("acquired") or [])
        except Exception:  # noqa: BLE001
            pass
        task = next((t for t in TASKS["development"] + TASKS["locked"]
                     if t["id"] == row["task_id"]), {})
        before = (row.get("evaluation") or {}).get("contract")
        row["evaluation"] = evaluate(task, row["question_id"], row)
        if row["evaluation"]["contract"] != before:
            changed += 1
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    print(f"重判 {len(data['runs'])} 条，其中 {changed} 条结论改变")


def main():
    core.fdsn.set_cache(core.CACHE)
    if len(sys.argv) > 1 and sys.argv[1] == "rejudge":
        rejudge()
        return
    which = sys.argv[1] if len(sys.argv) > 1 else "dev"
    only = sys.argv[2] if len(sys.argv) > 2 else None
    tasks = TASKS["development"] if which == "dev" else TASKS["locked"]

    results = json.loads(OUT.read_text()) if OUT.exists() else {
        "protocol": "delivery/revision-1/G2-PROTOCOL.md",
        "frozen_at": TASKS["frozen_at"], "model": TASKS["model"],
        "ceiling": TASKS["ceiling"], "runs": []}

    for task in tasks:
        if only and task["id"] != only:
            continue
        spec = spec_for_locked(task) if which == "locked" else None
        repeats = int(task.get("repeats", 1))
        for rep in range(1, repeats + 1):
            for arm in TASKS["arms"]:
                # 反馈轮只对 A 有意义，且只做一次，不随重复次数放大花费。
                if arm != "A" and task.get("follow_up") and rep > 1:
                    continue
                began = time.time()
                print(f"[{task['id']} r{rep}] {arm} …", flush=True)
                row = run_one(task, arm, spec)
                question_id = core.classify_question(task["question"])
                row.update({"task_id": task["id"], "repeat": rep,
                            "question_id": question_id,
                            "category": task.get("category")
                                        or task.get("category_intent"),
                            "elapsed_s": round(time.time() - began, 1)})
                row["evaluation"] = evaluate(task, question_id, row)
                results["runs"].append(row)
                OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2,
                                          default=str))
                print(f"    → {row['evaluation']['contract']}  "
                      f"({row.get('run_id')}, {row.get('elapsed_s')}s)", flush=True)

    print(f"\n写出 {OUT}")
    print(f"运行记录 {len(results['runs'])} 条")


if __name__ == "__main__":
    main()