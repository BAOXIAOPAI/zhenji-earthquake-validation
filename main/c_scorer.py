"""C0 评分器：C 批次的多维评分（review-5 指南 §2.1）。

与旧 G2 评分器（main/run_g2.py::evaluate）的差别，全部来自 review-5 §5 的
入口缺陷与指南 §2.1 的规范：

  1. 本模块是纯函数库：不落盘、不调用模型、不重判历史。新 runner
     （main/run_c.py）负责把结果写到新目录；任何人不得用它改写
     delivery/revision-1/G2-RESULTS.json 的旧成绩。
  2. `answered` 统一按“对象或布尔”两种历史形态规范化（normalize_answer），
     不再按布尔裸查——旧评分器曾因此把正确字段、无候选的合法 unknown
     整行错判 failed。
  3. 合法不足先查字段正确性：错字段的 unknown 不合格（答非所问），
     即使停止理由写“数据不足”。
  4. 模型主张风险与交付质量分列：`model_claim_accepted=false` 不是幻觉
     定义；正确未知也可能 false；错误主张被拦而最终交付正确时，主张
     维度记误、交付维度仍正确。
  5. 缺 `task_completed` 不得默认 True：字段完成必须显式成立。
  6. 执行失败（接口/模型/内部异常）与科学未知分开归因；接口层阻断
     不得记成“模型不取证”。

评分结果是一个维度字典加一个主分类（primary），不再混成单一 contract
标签；旧 G2 的单标签只作历史成绩保留。
"""
from __future__ import annotations

import re

# 每个提问类型必须有据交付的字段（与旧协议一致，这是数据结构不是评分口径）。
REQUIRED_FIELD = {"Q_propagating": "verdict",
                  "Q_usable": "method_applicability",
                  "Q_instrument": "channel_integrity_flags"}

# 各提问类型的“确定答案”标签：字段被回答且值落在这些标签上才算完成，
# 有据的否定/部分结论（pick_not_applicable / pick_partially_applicable /
# no_instrument_anomaly_in_scope）同样是完成，不是失败。
DECIDED_LABELS = {
    "Q_propagating": {"coherent_multistation", "single_station_only"},
    "Q_usable": {"pick_applicable", "pick_partially_applicable",
                 "pick_not_applicable"},
    "Q_instrument": {"instrument_artifact", "no_instrument_anomaly_in_scope"},
}
UNKNOWN_LABELS = {"pick_unknown", "not_decidable", "instrument_unknown"}

# 字段未定时的合法终止依据（旧 INSUFFICIENCY_BASE 扩展：coherence 在字段
# 未定时也是合法不足，例如取满候选后证据仍不足以判定多台共有）。
LEGAL_INSUFFICIENCY_BASES = {"unanswered_no_data", "coherence"}

# 交付了确定标签、依据却是另一问题的规则（旧 SUBSTITUTION_BASE）。
SUBSTITUTION_BASE = "fell_through_to_coherence"

# 执行失败类（接口/模型/内部）：这些不是科学未知。凡是 run 的 status
# 是 failed 且带这些 failure_kind，或工具异常未恢复导致阻断，都归
# execution_failure，绝不与“策略不取证”混淆。
EXECUTION_FAILURE_KINDS = {
    "tool_failure", "model_fatal", "internal_error", "timeout",
    "subprocess_exit", "no_execution_record", "record_mismatch",
    "runner_exception", "interface_failure",
}

DIMENSIONS = ("field_completed", "compliant_insufficient", "data_unavailable",
              "strategy_insufficient", "execution_failure",
              "model_unfounded_claim", "wrong_delivery",
              "explanation_fact_error")


# ---------------------------------------------------------------- 规范化 row

def normalize_answer(answered):
    """`answered` 的两种历史形态 → 统一形态。

    新结构是对象 {"answered": bool, "field": ..., "value": ..., "answer_kind":
    ...}；旧结构是裸布尔。裸 True/False 一律折成对象（field 缺省 None），
    这样评分逻辑只有一份。
    """
    if isinstance(answered, dict):
        return {"answered": bool(answered.get("answered")),
                "field": answered.get("field"),
                "value": answered.get("value"),
                "answer_kind": answered.get("answer_kind")}
    if isinstance(answered, bool):
        return {"answered": answered, "field": None, "value": None,
                "answer_kind": None}
    return {"answered": False, "field": None, "value": None,
            "answer_kind": None}


def normalize_row(raw, contract=None, *, strict=True):
    """任意来源的 run 行（R/M/A 臂或受控反例）→ 规范 row。

    规范 row 至少携带指南 §2.1 要求的字段：required/delivered field、
    方法目标与频带、字段值、回答类型、字段完成、status/failure、参与集合、
    候选与允许动作、实际尝试、stop reason、模型主张、最终交付、版本、费用。
    未知一律显式 None/False，绝不默认成功。

    `strict=True`（默认）要求 `delivered` 显式给出实际交付字段、最终判决与
    `task_completed`。缺失时不回填，而是标 `invalid_evidence`；`strict=False`
    只用于显式声明的历史行迁移。
    """
    contract = contract or {}
    raw = raw or {}
    delivered = raw.get("delivered") or {}
    acquisition = raw.get("acquisition") or {}
    claim = raw.get("model_claim") or {}
    question_id = raw.get("question_id") or contract.get("question_id")
    required = (contract.get("required_field")
                or REQUIRED_FIELD.get(question_id))
    answered = normalize_answer(raw.get("answered"))
    # O1/G05：显式交付必须自己写出字段、判决和完成标志。缺 KEY（不是显式 null）
    # 就是缺项，绝不从 answered／合同／模型主张回填。
    missing = [key for key in ("field", "verdict", "task_completed")
               if key not in delivered]
    task_completed = delivered.get("task_completed")
    if task_completed is not None:
        task_completed = bool(task_completed)
    if strict:
        delivered_field = delivered.get("field")
        delivered_verdict = delivered.get("verdict")
    else:
        # Only for an explicitly declared migration of a historical row; the caller
        # records the conversion and never overwrites the original result.
        delivered_field = (delivered.get("field")
                           or raw.get("field_answered")
                           or answered["field"]
                           or contract.get("required_field"))
        delivered_verdict = (delivered.get("verdict")
                             or raw.get("verdict")
                             or claim.get("conclusion"))
        if task_completed is None:
            task_completed = (answered["answered"]
                              if raw.get("answered") is not None else False)
    stations_acquired = raw.get("stations_acquired")
    if stations_acquired is None:
        stations_acquired = acquisition.get("stations_acquired", 0) or 0
    stations_analysed = raw.get("stations_analysed")
    if stations_analysed is None:
        stations_analysed = acquisition.get("stations_analysed")
    n_eligible = raw.get("n_eligible")
    if n_eligible is None:
        n_eligible = (acquisition.get("n_eligible"))
    tool_errors = (acquisition.get("tool_errors")
                   or raw.get("tool_errors") or 0)
    recovery = acquisition.get("recovery") or {}
    unrecovered = recovery.get("unrecovered") or raw.get("unrecovered") or []
    allowed_actions = (raw.get("allowed_actions")
                       or contract.get("allowed_actions"))
    # 采集是否被允许：T3 的“只复用”合同下不取数是合规，不是策略不足。
    acquisition_allowed = raw.get("acquisition_allowed")
    if acquisition_allowed is None:
        acquisition_allowed = contract.get("acquisition_allowed", True)
    # 排除后的有效候选数：策略不足只对“仍可取而未取”的候选成立。n_active
    # 未声明时退回 n_eligible（无排除信息的保守口径）。
    n_eligible = raw.get("n_eligible")
    if n_eligible is None:
        n_eligible = (acquisition.get("n_eligible"))
    n_active = raw.get("n_active")
    if n_active is None:
        excluded = (raw.get("excluded_candidates")
                    or delivered.get("excluded_candidates"))
        if n_eligible is not None and isinstance(excluded, list):
            n_active = max(0, (n_eligible or 0) - len(excluded))
        else:
            n_active = n_eligible
    return {
        "question_id": question_id,
        "required_field": required,
        "delivered_field": delivered_field,
        "delivered_verdict": delivered_verdict,
        "field_value": (delivered.get("field_value")
                        if delivered.get("field_value") is not None
                        else raw.get("field_value", answered["value"])),
        "answer_kind": delivered.get("answer_kind") or answered["answer_kind"],
        # 缺项时即使模型给了 task_completed=True 也不算完成（G05）。
        "field_completed": bool(task_completed) and not (bool(missing) and strict),
        # O1/G05：显式交付缺项。为真时该行不得被算作字段完成，且原因是可查的。
        "invalid_evidence": bool(missing) and strict,
        "missing_delivery_fields": missing,
        "status": raw.get("status"),
        "failure_kind": raw.get("failure_kind"),
        "failure_detail": raw.get("failure_detail"),
        "terminated_by": raw.get("terminated_by"),
        "method_goal": (raw.get("method_goal")
                        or contract.get("method_goal")),
        "target_band_hz": (raw.get("target_band_hz")
                           or contract.get("target_band_hz")),
        # 接口层事实：工具异常、未恢复对象、是否阻断。
        "tool_errors": int(tool_errors or 0),
        "unrecovered": list(unrecovered),
        "interface_blocked": bool(raw.get("interface_blocked")
                                  or (tool_errors and unrecovered)),
        # 参与集合与候选。
        "participating": {
            "stations_acquired": stations_acquired,
            "stations_analysed": stations_analysed,
            "included_candidates": raw.get("included_candidates")
                                    or delivered.get("included_candidates"),
            "excluded_candidates": raw.get("excluded_candidates")
                                    or delivered.get("excluded_candidates")},
        "candidates": {"n_eligible": n_eligible, "n_active": n_active,
                       "eligible_stations": raw.get("eligible_stations")},
        "allowed_actions": allowed_actions,
        "acquisition_allowed": bool(acquisition_allowed),
        # C 批次操作合同的观测上限（core.MAX_STATIONS=6）：策略归因只对
        # “上限内仍可取而未取”的候选成立；取到上限即动作范围用尽。
        "ceiling": (raw.get("ceiling") or contract.get("ceiling") or 6),
        "attempts": {"stations_tried": stations_acquired or 0,
                     "tool_attempts": acquisition.get("tool_attempts") or {}},
        "model_claim": {"conclusion": claim.get("conclusion")
                                   or raw.get("claim"),
                        "stop_reason": claim.get("stop_reason")
                                       or raw.get("model_stop_reason")},
        "final_delivery": {
            "verdict": delivered_verdict,
            "field": delivered_field,
            "value": delivered.get("value"),
            "task_completed": bool(task_completed),
            "model_claim_accepted": delivered.get("model_claim_accepted")},
        "expected_verdict": raw.get("expected_verdict"),
        "coincidence": raw.get("coincidence"),
        "version": raw.get("version") or contract.get("version"),
        "cost": {"model_cost_known": raw.get("model_cost_known"),
                 "model_cost_cny": raw.get("model_cost_cny"),
                 "http_attempts": raw.get("http_attempts"),
                 "logical_acquisitions": raw.get("logical_acquisitions")},
    }


# ---------------------------------------------------------------- 评分

def _claim_unfounded(norm):
    """模型主张是否与结构化证据冲突（与 validator 是否拦截无关）。

    三种形态都算无依据确定性主张：
      a) 字段已定且主张 != 结构化判决（判决即 delivered verdict）；
      b) 字段未定却主张确定标签（对未定字段断言确定性）；
      c) 主张 coherence 但结构化 coincidence 不足（即使字段已定为其它值）。
    正确 unknown（主张也是 unknown）不算：那不是确定性主张。
    """
    claim = norm["model_claim"]["conclusion"]
    if not claim or claim in UNKNOWN_LABELS:
        return False
    settled = norm["field_completed"] and norm["delivered_verdict"] is not None
    if settled:
        if claim != norm["delivered_verdict"]:
            return True
        # 标签相同也要看结构化计数是否支撑（如 coherence 需 ≥3 台触发）。
        if (claim == "coherent_multistation"
                and norm["coincidence"] is not None
                and norm["coincidence"] < 3):
            return True
        return False
    # 字段未定：任何确定标签主张都无结构化证据支撑。
    return True


def check_explanation_facts(claim_text, facts):
    """按明确事实清单核对模型解释文本（指南 §2.1 维度 8）。

    facts 是结构化事实列表，每项至少 {"kind", ...}。目前实现的核对：
      - kind="station_stability"：{"station", "state"}；
      - 文本断言“所有台站稳定”（all/every/N 台 + stable/健康/一致）时，
        任一台 state != "stable" 即冲突；
      - 文本点名某台“稳定”而该台事实非 stable 即冲突。
    返回 (冲突列表, 核对过的断言数)；冲突为空即无解释事实错误。
    这是有限核对，不是万能文本验证器——指南只要求小样例事实清单核对。
    """
    if not claim_text or not facts:
        return [], 0
    text = str(claim_text)
    stability = {f.get("station"): f.get("state") for f in facts
                 if f.get("kind") == "station_stability"}
    conflicts, checked = [], 0
    # 数词断言只在与事实清单台站数一致时视为全称断言（“两个健康台”在
    # 3 台清单里是子集指称，不能拿另一台的不稳定去推翻）；all/every
    # 始终是全称。子集指称无法解析指向哪几台，保守不判。
    n_facts = len(stability)
    all_stable = re.search(
        r"(?:(all|every)\s+(?:\b(?:six|five|four|three|two|one|\d+)\s+)?"
        r"(?:three[- ]component\s+)?(?:stations?\s+)?"
        r"(?:show\s+|are\s+|appear\s+|seem\s+|look\s+|remain\s+|give\s+)?"
        r"(?:onsets?\s+)?(healthy|stable)\b"
        r"|(\b(?:four|three|five|six|two)\b|\d+)\s+"
        r"(three[- ]component\s+)?(healthy|stable)\b"
        r"|所有(台站|站).*稳定|各台.*稳定|四台.*稳定)", text, re.I)
    if all_stable:
        word = all_stable.group(3)
        if word:
            n = _number_word(word)
            if n is not None and n_facts and n != n_facts:
                all_stable = None  # 子集指称，不作全称断言核对
    if all_stable and stability:
        checked += 1
        bad = [f"{st}:{state}" for st, state in stability.items()
               if state != "stable"]
        if bad:
            conflicts.append({
                "assertion": "all_stations_stable",
                "text": all_stable.group(0),
                "contradicted_by": bad})
    # 点名断言不把“unstable”里的“stable”当肯定词。
    per_station = re.findall(
        r"\b([A-Z]{3,5})\b[^.。;；]{0,40}?(?<!un)(?<!un-)(stable|健康|稳定)",
        text)
    for station, _word in per_station:
        state = stability.get(station.upper())
        if state is not None:
            checked += 1
            if state != "stable":
                conflicts.append({
                    "assertion": "station_stable",
                    "station": station.upper(),
                    "contradicted_by": f"{station.upper()}:{state}"})
    return conflicts, checked


def _number_word(word):
    """数词 → 数值；未知词返回 None。"""
    table = {"four": 4, "three": 3, "five": 5, "six": 6, "two": 2,
             "一": 1, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6}
    text = str(word).strip().lower()
    if text.isdigit():
        return int(text)
    return table.get(text)


def score_row(norm, facts=None):
    """一个规范 row → 8 维评分 + 主分类。

    维度是布尔分项（可同时成立，如“交付正确 + 主张无依据”）；primary 是
    归因主类，优先级：执行失败 > 错误交付 > 数据不可得 > 策略不足 >
    字段完成 > 合规不足 > 未分类。维度之间不互斥，primary 只回答
    “这一行最主要发生了什么”。
    """
    dims = {d: False for d in DIMENSIONS}
    why = []
    if (norm.get("invalid_evidence") and norm.get("status") != "failed"
            and not norm.get("interface_blocked")):
        # G05：显式交付缺项。不得靠 answered／合同／模型主张补成成功。
        # 执行失败仍然优先：缺交付字段不掩盖接口/预算失败这一更主要的事实。
        why.append("实际交付缺显式字段："
                   + "、".join(norm.get("missing_delivery_fields") or [])
                   + "（不从 answered／合同／模型主张回填）")
        return {"dimensions": dims, "primary": "invalid_evidence", "why": why,
                "field_delivered": norm.get("delivered_field"),
                "required_field": norm.get("required_field"),
                "explanation_conflicts": []}
    required = norm["required_field"]
    delivered_field = norm["delivered_field"]
    field_ok = bool(required and delivered_field == required)
    decided = norm["delivered_verdict"] in (
        DECIDED_LABELS.get(norm["question_id"], set()))

    # --- 维度 6（独立于交付）：模型无依据确定性主张。
    dims["model_unfounded_claim"] = _claim_unfounded(norm)
    if dims["model_unfounded_claim"]:
        why.append("模型主张与结构化证据冲突（与是否被拦截分开计）")

    # --- 维度 8（独立于交付）：解释事实错误。
    conflicts, _checked = check_explanation_facts(
        norm["model_claim"]["stop_reason"], facts or [])
    dims["explanation_fact_error"] = bool(conflicts)
    if conflicts:
        why.append("解释与事实清单冲突：" + "; ".join(
            c["assertion"] + "←" + ",".join(c["contradicted_by"])
            if isinstance(c["contradicted_by"], list)
            else c["assertion"] + "←" + c["contradicted_by"]
            for c in conflicts))

    # --- 维度 5：执行失败（接口/模型/内部异常阻断任务，不是科学未知）。
    status_failed = norm["status"] == "failed"
    exec_kind = norm["failure_kind"] in EXECUTION_FAILURE_KINDS
    if status_failed and (exec_kind or norm["failure_kind"]):
        dims["execution_failure"] = True
        why.append(f"运行失败（{norm['failure_kind']}）：执行层阻断，"
                   "不混作科学未知")
    elif norm["interface_blocked"] and not norm["field_completed"]:
        # review-4 A4 形态 4：未恢复异常但所问字段已由独立证据回答的是
        # **成功**（异常另列）；只有异常真的阻断了任务（字段未完成）才是
        # 执行失败。
        dims["execution_failure"] = True
        why.append("工具异常未恢复且任务未完成：接口失败（非证据不足）")

    # --- 维度 7：错误最终交付（确定结果错误或答非所问）。
    # 什么都没交付（无字段、无标签）不是“答非所问”，由执行失败或不足
    # 归因处理；只有真的交付了答案才谈答非所问。
    delivered_anything = (delivered_field is not None
                          or norm["delivered_verdict"] not in (None,))
    other_vocab = any(norm["delivered_verdict"] in labels
                      for qid, labels in DECIDED_LABELS.items()
                      if qid != norm["question_id"])
    if field_ok:
        if norm["terminated_by"] == SUBSTITUTION_BASE:
            dims["wrong_delivery"] = True
            why.append(f"问的是 {required}，依据却替换成了别的规则（答非所问）")
        elif other_vocab:
            dims["wrong_delivery"] = True
            why.append(f"问的是 {required}（{norm['question_id']}），交付的标签 "
                       f"{norm['delivered_verdict']} 属于另一类问题的词表：答非所问")
        elif decided and not norm["field_completed"]:
            # 交付了确定标签但 task_completed=False/缺值：确定答案没有
            # 完成依据，不得进入成功（指南反例 5）。
            dims["wrong_delivery"] = True
            why.append(f"交付确定标签 {norm['delivered_verdict']} 但字段"
                       "task_completed=False：确定答案缺完成依据")
        elif norm["field_completed"] and norm["expected_verdict"] \
                and norm["delivered_verdict"] != norm["expected_verdict"]:
            dims["wrong_delivery"] = True
            why.append(f"交付 {norm['delivered_verdict']}，事实清单判定应为 "
                       f"{norm['expected_verdict']}")
    elif delivered_anything:
        dims["wrong_delivery"] = True
        why.append(f"交付字段是 {delivered_field}，所问是 {required}：答非所问，"
                   "即使停止理由写数据不足也不合格")

    # --- 维度 1：所问字段正确完成（含有据否定/部分结论）。
    if field_ok and norm["field_completed"] and decided \
            and norm["terminated_by"] != SUBSTITUTION_BASE \
            and not dims["wrong_delivery"]:
        dims["field_completed"] = True
        why.append(f"所问字段 {required} 正确完成"
                   f"（{norm['delivered_verdict']}）")

    # --- 维度 2/3/4：字段未定时的归因。
    if not dims["field_completed"] and not dims["wrong_delivery"] \
            and not dims["execution_failure"]:
        tried = norm["attempts"]["stations_tried"] or 0
        analysed = norm["participating"]["stations_analysed"]
        # 策略归因看“排除后仍可取”的候选（n_active）；归因描述引用发现数。
        n_active = norm["candidates"]["n_active"]
        n_eligible = norm["candidates"]["n_eligible"]
        # 合同上限内的未取余量：tried 计数包含反馈轮结转的观测。
        ceiling = norm.get("ceiling") or 6
        untried_within_ceiling = min(n_active or 0, ceiling) - tried
        if tried > 0 and (analysed or 0) == 0 and not norm["tool_errors"]:
            dims["data_unavailable"] = True
            why.append(f"尝试取数 {tried} 次但无可分析数据且无工具异常："
                       "数据不可得（服务无数据或元数据不足）")
        elif (norm["acquisition_allowed"]
              and untried_within_ceiling > 0
              and not norm["tool_errors"]):
            dims["strategy_insufficient"] = True
            why.append(f"允许取数且合同上限内仍有 "
                       f"{untried_within_ceiling} 个可取候选未取"
                       f"（发现 {n_eligible} 个、已取 {tried} 个）"
                       "就停止：策略不足（无依据不尝试）")
        elif norm["terminated_by"] in LEGAL_INSUFFICIENCY_BASES or \
                norm["terminated_by"] is None:
            dims["compliant_insufficient"] = True
            if (n_active or 0) == 0 and tried == 0:
                why.append("字段未定但动作范围内确实没有可用候选"
                           "（含用户排除后为空）：合规不足，字段未定")
            elif untried_within_ceiling <= 0:
                why.append(f"字段未定，但已取 {tried} 个观测达合同上限"
                           f"（{ceiling}）：动作范围用尽，合规不足，字段未定")
            else:
                why.append(f"字段未定，停止依据 {norm['terminated_by']} 合法、"
                           "证据与动作范围明确：合规不足，字段未定")
        else:
            why.append(f"字段未定且停止依据 {norm['terminated_by']} "
                       "不在合法不足依据内：未分类")

    primary = ("execution_failure" if dims["execution_failure"]
               else "wrong_delivery" if dims["wrong_delivery"]
               else "data_unavailable" if dims["data_unavailable"]
               else "strategy_insufficient" if dims["strategy_insufficient"]
               else "field_completed" if dims["field_completed"]
               else "compliant_insufficient" if dims["compliant_insufficient"]
               else "unclassified")
    return {"dimensions": dims, "primary": primary, "why": why,
            "field_delivered": field_ok,
            "required_field": required,
            "explanation_conflicts": conflicts}
