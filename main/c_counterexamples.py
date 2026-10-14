"""C0 无付费受控评分反例（review-5 指南 §2.1 的“必须通过”清单）。

这些 row 全部是**手工构造的受控输入**：不调用模型、不取数、不重判历史
run。它们的作用是给新评分器（main/c_scorer.py）一组带期望结果的入口
测试，锁定以下归因规则：

  1. 正确字段、无候选、当前结构的 unknown → 合规不足，字段未定。
  2. 错字段 unknown → 不合格（答非所问），即使停止理由写数据不足。
  3. 有候选不尝试 / 实际尝试无数据 / 接口阻断 → 分别归因，不互相顶替。
  4. 方法明确不适用、坏 E 好 Z 的垂直目标、部分成立 → 有据回答算完成。
  5. task_completed=False 及缺值 → 不得进入确定答案成功。
  6. 错误模型主张被拦而最终交付正确 / 错误最终交付 → 分开评分。
  7. accepted 标签正确、解释声称全稳却存在双簇 → 标签与理由各记。
  8. 正确未知（模型与规则同为 unknown）→ 不是无依据主张，字段未定。

每个 case 的 `expected` 声明维度布尔与主分类；验证脚本与单元测试都按
它断言。旧评分器在这组输入上的三个已知错判（failed /
insufficient_correctly / violation）同时被旧反例文件复跑覆盖。
"""

E1_STOP_REASON = (
    "Four healthy three-component stations (OLGA, MCW, ORCA, LUMI) at "
    "0.7–14.1 km all show stable onsets within ~0.9 s of each other, "
    "consistent with a coherent multistation signal.")

# E1 首轮的真实结构化稳定性事实（delivery/revision-4/evidence/agh-smoke/
# runs/r-cd19abfbe75cc05b/mcp_state.json::stability_checks，只读引用）。
E1_FACTS = [
    {"kind": "station_stability", "station": "OLGA", "state": "stable"},
    {"kind": "station_stability", "station": "MCW",
     "state": "unstable_bimodal"},
    {"kind": "station_stability", "station": "ORCA", "state": "stable"},
    {"kind": "station_stability", "station": "LUMI", "state": "stable"},
]

CASES = [
    {
        "id": "CE-01_legal_unknown_no_candidates",
        "guide_item": "正确字段、无候选、当前结构的 unknown：合规不足，字段未定",
        "row": {
            "question_id": "Q_usable", "status": "succeeded",
            "verdict": "pick_unknown",
            "delivered": {"verdict": "pick_unknown",
                          "field": "method_applicability",
                          "field_value": "unknown",
                          "task_completed": False},
            "terminated_by": "unanswered_no_data",
            "answered": {"answered": False, "value": "unknown"},
            "n_eligible": 0, "stations_acquired": 0,
        },
        "expected": {"primary": "compliant_insufficient",
                     "dimensions": {"compliant_insufficient": True}},
    },
    {
        "id": "CE-02_wrong_field_unknown",
        "guide_item": "错字段 unknown：不合格，即使停止理由写数据不足",
        "row": {
            "question_id": "Q_usable", "status": "succeeded",
            "verdict": "not_decidable",
            "delivered": {"verdict": "not_decidable", "field": "verdict",
                          "task_completed": False},
            "terminated_by": "unanswered_no_data",
            "answered": False,
            "n_eligible": 0, "stations_acquired": 0,
        },
        "expected": {"primary": "wrong_delivery",
                     "dimensions": {"wrong_delivery": True,
                                    "compliant_insufficient": False}},
    },
    {
        "id": "CE-03a_candidates_not_tried",
        "guide_item": "有候选不尝试：策略不足",
        "row": {
            "question_id": "Q_usable", "status": "succeeded",
            "verdict": "not_decidable",
            "delivered": {"verdict": "not_decidable",
                          "field": "method_applicability",
                          "task_completed": False},
            "terminated_by": "unanswered_no_data",
            "answered": {"answered": False,
                         "field": "method_applicability",
                         "value": "unknown"},
            "n_eligible": 6, "stations_acquired": 0,
        },
        "expected": {"primary": "strategy_insufficient",
                     "dimensions": {"strategy_insufficient": True}},
    },
    {
        "id": "CE-03b_attempted_no_data",
        "guide_item": "实际尝试无数据：数据不可得（服务无数据或元数据不足）",
        "row": {
            "question_id": "Q_usable", "status": "succeeded",
            "verdict": "pick_unknown",
            "delivered": {"verdict": "pick_unknown",
                          "field": "method_applicability",
                          "task_completed": False},
            "terminated_by": "unanswered_no_data",
            "answered": {"answered": False,
                         "field": "method_applicability",
                         "value": "unknown"},
            "n_eligible": 3, "stations_acquired": 3,
            "stations_analysed": 0,
        },
        "expected": {"primary": "data_unavailable",
                     "dimensions": {"data_unavailable": True,
                                    "strategy_insufficient": False}},
    },
    {
        "id": "CE-03c_interface_blocked",
        "guide_item": "接口阻断：执行失败，不得记成模型不取证或科学未知",
        "row": {
            "question_id": "Q_usable", "status": "failed",
            "failure_kind": "tool_failure",
            "delivered": {"verdict": None, "field": "method_applicability",
                          "task_completed": False},
            "terminated_by": "unanswered_no_data",
            "acquisition": {"tool_errors": 2,
                            "tools_with_errors": ["acquire_station"],
                            "recovery": {"order_known": True,
                                         "recovered": [],
                                         "unrecovered":
                                             ["acquire_station(A1)"]}},
            "n_eligible": 6, "stations_acquired": 0,
        },
        "expected": {"primary": "execution_failure",
                     "dimensions": {"execution_failure": True,
                                    "strategy_insufficient": False,
                                    "data_unavailable": False}},
    },
    {
        "id": "CE-04a_method_not_applicable",
        "guide_item": "方法明确不适用：有据的否定回答算字段完成",
        "row": {
            "question_id": "Q_usable", "status": "succeeded",
            "verdict": "pick_not_applicable",
            "delivered": {"verdict": "pick_not_applicable",
                          "field": "method_applicability",
                          "field_value": False,
                          "answer_kind": "negative",
                          "task_completed": True},
            "terminated_by": "band_check",
            "answered": {"answered": True, "field": "method_applicability",
                         "value": False, "answer_kind": "negative"},
            "n_eligible": 4, "stations_acquired": 4,
            "stations_analysed": 4,
        },
        "expected": {"primary": "field_completed",
                     "dimensions": {"field_completed": True}},
    },
    {
        "id": "CE-04b_bad_E_good_Z_vertical_goal",
        "guide_item": "坏 E 好 Z：三分量目标不适用，改问垂直目标后有据完成",
        "row": {
            "question_id": "Q_usable", "status": "succeeded",
            "method_goal": "vertical_onset",
            "verdict": "pick_applicable",
            "delivered": {"verdict": "pick_applicable",
                          "field": "method_applicability",
                          "field_value": True,
                          "answer_kind": "positive",
                          "task_completed": True},
            "terminated_by": "band_check",
            "answered": {"answered": True, "field": "method_applicability",
                         "value": True, "answer_kind": "positive"},
            "n_eligible": 4, "stations_acquired": 4,
            "stations_analysed": 4,
        },
        "expected": {"primary": "field_completed",
                     "dimensions": {"field_completed": True}},
    },
    {
        "id": "CE-04c_partially_applicable",
        "guide_item": "部分成立：有据的部分回答算字段完成",
        "row": {
            "question_id": "Q_usable", "status": "succeeded",
            "verdict": "pick_partially_applicable",
            "delivered": {"verdict": "pick_partially_applicable",
                          "field": "method_applicability",
                          "field_value": "partial",
                          "answer_kind": "partial",
                          "task_completed": True},
            "terminated_by": "band_partial",
            "answered": {"answered": True, "field": "method_applicability",
                         "value": "partial", "answer_kind": "partial"},
            "n_eligible": 4, "stations_acquired": 4,
            "stations_analysed": 4,
        },
        "expected": {"primary": "field_completed",
                     "dimensions": {"field_completed": True}},
    },
    {
        "id": "CE-05_decided_label_without_completion",
        "guide_item": "task_completed=False 及缺值不能进入确定答案成功",
        "row": {
            "question_id": "Q_propagating", "status": "succeeded",
            "verdict": "coherent_multistation",
            "delivered": {"verdict": "coherent_multistation",
                          "field": "verdict", "task_completed": False},
            "terminated_by": "coherence",
            "answered": {"answered": False, "value": "unknown"},
            "coincidence": 2, "n_eligible": 6, "stations_acquired": 6,
            "stations_analysed": 6,
        },
        "expected": {"primary": "wrong_delivery",
                     "dimensions": {"wrong_delivery": True,
                                    "field_completed": False}},
    },
    {
        "id": "CE-06a_claim_blocked_delivery_correct",
        "guide_item": "错误模型主张被拦、正确最终交付：主张维度记误，交付仍正确",
        "row": {
            "question_id": "Q_propagating", "status": "succeeded",
            "claim": "coherent_multistation",
            "delivered": {"verdict": "single_station_only",
                          "field": "verdict", "task_completed": True,
                          "model_claim_accepted": False},
            "terminated_by": "coherence",
            "answered": {"answered": True, "value": "single_station_only"},
            "coincidence": 1, "n_eligible": 6, "stations_acquired": 3,
        },
        "expected": {"primary": "field_completed",
                     "dimensions": {"field_completed": True,
                                    "model_unfounded_claim": True,
                                    "wrong_delivery": False}},
    },
    {
        "id": "CE-06b_wrong_final_delivery",
        "guide_item": "错误最终交付：与主张风险分开计",
        "row": {
            "question_id": "Q_propagating", "status": "succeeded",
            "claim": "coherent_multistation",
            "delivered": {"verdict": "coherent_multistation",
                          "field": "verdict", "task_completed": True,
                          "model_claim_accepted": True},
            "terminated_by": "coherence",
            "answered": {"answered": True, "value": "coherent_multistation"},
            "coincidence": 1, "n_eligible": 6, "stations_acquired": 3,
            "expected_verdict": "single_station_only",
        },
        "expected": {"primary": "wrong_delivery",
                     "dimensions": {"wrong_delivery": True,
                                    "model_unfounded_claim": True,
                                    "field_completed": False}},
    },
    {
        "id": "CE-07_label_ok_explanation_wrong",
        "guide_item": "accepted 标签正确、解释声称全稳却存在双簇：标签与理由各记",
        "row": {
            "question_id": "Q_propagating", "status": "succeeded",
            "model_claim": {"conclusion": "coherent_multistation",
                            "stop_reason": E1_STOP_REASON},
            "delivered": {"verdict": "coherent_multistation",
                          "field": "verdict", "task_completed": True,
                          "model_claim_accepted": True},
            "terminated_by": "coherence",
            "answered": {"answered": True, "value": "coherent_multistation"},
            "coincidence": 4, "n_eligible": 4, "stations_acquired": 4,
            "facts": E1_FACTS,
        },
        "expected": {"primary": "field_completed",
                     "dimensions": {"field_completed": True,
                                    "explanation_fact_error": True,
                                    "model_unfounded_claim": False}},
    },
    {
        "id": "CE-08_correct_unknown_after_exclusion",
        "guide_item": "正确未知不是错误主张被拦截（S1 反馈形态：唯一候选被用户排除）",
        "row": {
            "question_id": "Q_usable", "status": "succeeded",
            "model_claim": {"conclusion": "pick_unknown",
                            "stop_reason": "HOOD was the only eligible "
                                           "candidate and the user excluded "
                                           "it, leaving no observations."},
            "delivered": {"verdict": "pick_unknown",
                          "field": "method_applicability",
                          "task_completed": False,
                          "model_claim_accepted": False},
            "terminated_by": "unanswered_no_data",
            "answered": {"answered": False, "value": "unknown"},
            "n_eligible": 1, "n_active": 0, "stations_acquired": 0,
            "excluded_candidates": ["HOOD"],
        },
        "expected": {"primary": "compliant_insufficient",
                     "dimensions": {"compliant_insufficient": True,
                                    "model_unfounded_claim": False,
                                    "strategy_insufficient": False}},
    },
    # CE-09：部分采集后无依据停止（v1 排查发现的真实形态：A 臂取 2 台
    # 即停，合同上限 6 内仍有可取候选，字段未定）。策略不足不只覆盖
    # “一次未取”，也覆盖“上限内未取完就停”。
    {
        "id": "CE-09_partial_acquisition_stop",
        "guide_item": "策略不足：有候选/允许动作，却无依据地未尝试而停止"
                      "（含部分采集后停止）",
        "row": {
            "question_id": "Q_propagating",
            "status": "succeeded",
            "verdict": "not_decidable",
            "delivered": {"verdict": "not_decidable",
                          "field": "verdict",
                          "task_completed": False},
            "answered": {"answered": False},
            "terminated_by": "coherence",
            "n_eligible": 54, "stations_acquired": 2,
            "stations_analysed": 2, "ceiling": 6,
            "acquisition_allowed": True,
        },
        "expected": {"primary": "strategy_insufficient",
                     "dimensions": {"strategy_insufficient": True}},
    },
]

# 旧评分器（run_g2.evaluate）在这三个 row 上的已知错判；新评分器必须给出
# 正确分类。row 原样取自 delivery/review-5-2026-10-10/evidence/
# C评分入口反例.json（只读引用，不改原件）。
OLD_SCORER_MISJUDGMENTS = [
    {"id": "legal_unknown_new_schema",
     "old_contract": "failed",
     "new_primary": "compliant_insufficient"},
    {"id": "wrong_field_unknown",
     "old_contract": "insufficient_correctly",
     "new_primary": "wrong_delivery"},
    {"id": "model_claim_rejected_but_correct_delivery",
     "old_contract": "violation",
     "new_primary": "field_completed"},
]
