"""C0 评分器的单元测试（review-5 指南 §2.1 的无付费反例清单）。

与 delivery/revision-5/evidence/C0评分反例验证.py 共用同一组受控反例
（main/c_counterexamples.py）；这里做的是测试期断言，那里做的是证据
落盘。两者都以同一份期望为准。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import c_counterexamples as cx  # noqa: E402
import c_scorer  # noqa: E402


def _score(row, contract=None):
    norm = c_scorer.normalize_row(row, contract)
    return c_scorer.score_row(norm, facts=row.get("facts")), norm


def test_every_guide_counterexample_passes():
    for case in cx.CASES:
        result, _ = _score(case["row"])
        exp = case["expected"]
        assert result["primary"] == exp["primary"], (
            f"{case['id']}: primary {result['primary']} != {exp['primary']}\n"
            f"why={result['why']}")
        for dim, want in exp["dimensions"].items():
            got = result["dimensions"].get(dim, False)
            assert got == want, (f"{case['id']}: {dim} 应为 {want}，"
                                 f"实为 {got}；why={result['why']}")


def test_answered_object_is_not_misread_as_boolean():
    """旧缺陷 1：answered 是对象时按布尔裸查 → 合法 unknown 被判 failed。"""
    row = cx.CASES[0]["row"]
    norm = c_scorer.normalize_row(row)
    assert isinstance(row["answered"], dict)
    assert norm["field_completed"] is False
    result, _ = _score(row)
    assert result["dimensions"]["execution_failure"] is False
    assert result["primary"] == "compliant_insufficient"


def test_answered_boolean_legacy_form_normalizes():
    """旧结构的裸布尔 answered 也能规范化，不再失真。"""
    norm = c_scorer.normalize_answer(False)
    assert norm == {"answered": False, "field": None, "value": None,
                    "answer_kind": None}
    norm = c_scorer.normalize_answer({"answered": True, "field": "verdict",
                                      "value": "single_station_only"})
    assert norm["answered"] is True and norm["field"] == "verdict"


def test_missing_task_completed_defaults_to_incomplete():
    """旧缺陷 3：缺 task_completed 不得默认 True。"""
    norm = c_scorer.normalize_row({"question_id": "Q_usable",
                                   "status": "succeeded",
                                   "delivered": {"verdict": "pick_applicable",
                                                 "field":
                                                     "method_applicability"}})
    assert norm["field_completed"] is False


def test_legal_insufficiency_checks_field_first():
    """旧缺陷 2：错字段 unknown 不得计合法不足。"""
    result, _ = _score(cx.CASES[1]["row"])
    assert result["dimensions"]["compliant_insufficient"] is False
    assert result["dimensions"]["wrong_delivery"] is True


def test_claim_risk_is_separate_from_delivery_quality():
    """旧缺陷 3（主张/交付混算）：主张被拦而交付正确 → 交付维度仍正确。"""
    result, _ = _score(cx.CASES[9]["row"])  # CE-06a
    assert result["dimensions"]["field_completed"] is True
    assert result["dimensions"]["model_unfounded_claim"] is True
    assert result["dimensions"]["wrong_delivery"] is False


def test_correct_unknown_is_not_an_unfounded_claim():
    """正确未知：claim 与结构化判决同为 unknown → 不是无依据主张。"""
    row = cx.CASES[12]["row"]  # CE-08
    result, _ = _score(row)
    assert result["dimensions"]["model_unfounded_claim"] is False
    assert result["dimensions"]["compliant_insufficient"] is True


def test_explanation_facts_use_an_explicit_fact_list():
    """标签正确 + 解释与事实清单冲突 → 解释维度单独记错。"""
    row = cx.CASES[11]["row"]  # CE-07
    result, _ = _score(row)
    assert result["dimensions"]["field_completed"] is True
    assert result["dimensions"]["explanation_fact_error"] is True
    conflicts = result["explanation_conflicts"]
    assert conflicts and any("MCW" in str(c.get("contradicted_by"))
                             for c in conflicts)


def test_explanation_check_passes_when_text_matches_facts():
    text = ("Four healthy three-component stations (OLGA, ORCA, LUMI) "
            "all show stable onsets.")
    facts = [f for f in cx.E1_FACTS if f["station"] != "MCW"]
    conflicts, checked = c_scorer.check_explanation_facts(text, facts)
    assert conflicts == [] and checked >= 1


def test_interface_failure_is_not_strategy_or_science():
    """接口阻断归执行失败，不记策略不足、不记科学未知。"""
    result, _ = _score(cx.CASES[4]["row"])  # CE-03c
    d = result["dimensions"]
    assert d["execution_failure"] is True
    assert d["strategy_insufficient"] is False
    assert d["data_unavailable"] is False
    assert d["compliant_insufficient"] is False


def test_unrecovered_error_with_completed_field_is_not_execution_failure():
    """review-4 A4 形态 4：未恢复异常但字段已答 → 成功（异常另列）。"""
    row = {"question_id": "Q_usable", "status": "succeeded",
           "verdict": "pick_applicable",
           "delivered": {"verdict": "pick_applicable",
                         "field": "method_applicability",
                         "field_value": True, "task_completed": True},
           "terminated_by": "band_check",
           "answered": {"answered": True, "field": "method_applicability",
                        "value": True},
           "acquisition": {"tool_errors": 1,
                           "recovery": {"order_known": True,
                                        "recovered": [],
                                        "unrecovered": ["get_evidence(X)"]}},
           "n_eligible": 4, "stations_acquired": 4,
           "stations_analysed": 4}
    result, _ = _score(row)
    assert result["dimensions"]["field_completed"] is True
    assert result["dimensions"]["execution_failure"] is False
    assert result["primary"] == "field_completed"


def test_reuse_only_contract_does_not_count_as_strategy_gap():
    """T3 只复用合同：不取数是合规，不是策略不足。"""
    row = {"question_id": "Q_usable", "status": "succeeded",
           "verdict": "pick_unknown",
           "delivered": {"verdict": "pick_unknown",
                         "field": "method_applicability",
                         "task_completed": False},
           "terminated_by": "unanswered_no_data",
           "answered": {"answered": False, "value": "unknown"},
           "acquisition_allowed": False,
           "n_eligible": 6, "stations_acquired": 0}
    result, _ = _score(row)
    assert result["dimensions"]["strategy_insufficient"] is False
    assert result["dimensions"]["compliant_insufficient"] is True


def test_active_candidates_after_exclusion_drive_strategy_attribution():
    """排除后有效候选为空 → 不再是策略不足，而是合规不足。"""
    row = {"question_id": "Q_usable", "status": "succeeded",
           "verdict": "pick_unknown",
           "delivered": {"verdict": "pick_unknown",
                         "field": "method_applicability",
                         "task_completed": False},
           "terminated_by": "unanswered_no_data",
           "answered": {"answered": False, "value": "unknown"},
           "n_eligible": 2, "stations_acquired": 0,
           "excluded_candidates": ["A", "B"]}
    norm = c_scorer.normalize_row(row)
    assert norm["candidates"]["n_active"] == 0
    result, _ = _score(row)
    assert result["dimensions"]["strategy_insufficient"] is False
    assert result["primary"] == "compliant_insufficient"


def test_cross_vocabulary_label_is_wrong_field():
    """Q_usable 交付 coherence 标签 = 词表替换，答非所问。"""
    row = {"question_id": "Q_usable", "status": "succeeded",
           "verdict": "coherent_multistation",
           "delivered": {"verdict": "coherent_multistation",
                         "field": "method_applicability",
                         "task_completed": True},
           "terminated_by": "band_check",
           "answered": {"answered": True, "field": "method_applicability"},
           "n_eligible": 4, "stations_acquired": 4,
           "stations_analysed": 4}
    result, _ = _score(row)
    assert result["dimensions"]["wrong_delivery"] is True
    assert result["dimensions"]["field_completed"] is False


def test_nothing_delivered_is_not_wrong_field():
    """什么都没交付（无字段无标签）不叫答非所问，按执行/不足归因。"""
    row = {"question_id": "Q_usable", "status": "failed",
           "failure_kind": "timeout",
           "delivered": {},
           "terminated_by": None,
           "n_eligible": 4, "stations_acquired": 0}
    result, _ = _score(row)
    assert result["dimensions"]["wrong_delivery"] is False
    assert result["dimensions"]["execution_failure"] is True


def test_normalized_row_carries_the_guide_minimum_fields():
    """规范 row 至少带指南 §2.1 要求的字段族。"""
    row = cx.CASES[9]["row"]
    norm = c_scorer.normalize_row(row)
    for key in ("required_field", "delivered_field", "delivered_verdict",
                "field_value", "answer_kind", "field_completed", "status",
                "failure_kind", "participating", "candidates",
                "allowed_actions", "attempts", "model_claim",
                "final_delivery", "version", "cost", "method_goal",
                "terminated_by"):
        assert key in norm, f"规范 row 缺字段 {key}"
    assert norm["required_field"] == "verdict"
    assert norm["candidates"]["n_eligible"] == 6
