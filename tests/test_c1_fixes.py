"""C 批次 v1→v2 修复的回归测试（全部无付费）。

v1 排查批次（delivery/revision-5/evidence/C1-排查批次.json）暴露的三个
runner/评分缺口，修复后必须锁住：
  1. M 反馈轮的结构化变更没有应用到结转观测（T2/T3 的 M 反馈交付停留
     在变更前的判决）→ _apply_feedback_change 必须真实应用并持久化；
  2. R 反馈轮的行缺 stations_analysed/n_eligible 映射 → 唯一观测被用户
     排除被误归因为“数据不可得”而非“合规不足”（CE-08 的真实形态）；
  3. 部分采集后无依据停止（取 2/54 即停，上限 6 未取满）应归因策略不足
     而非合规不足（CE-09）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import c_scorer  # noqa: E402
import run_c  # noqa: E402


def _counterexample(case_id: str) -> dict:
    for case in c_counterexamples.CASES:
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


import c_counterexamples  # noqa: E402


def test_ce09_partial_acquisition_is_strategy_insufficient():
    case = _counterexample("CE-09_partial_acquisition_stop")
    norm = c_scorer.normalize_row(dict(case["row"]))
    scored = c_scorer.score_row(norm)
    assert scored["primary"] == case["expected"]["primary"]
    assert scored["dimensions"]["strategy_insufficient"] is True
    # 交付本身是诚实的未知，不是错误交付。
    assert scored["dimensions"]["wrong_delivery"] is False


def test_ceiling_exhaustion_is_compliant_not_strategy():
    """取到合同上限（6）仍未定 → 动作范围用尽，合规不足。"""
    row = _counterexample("CE-09_partial_acquisition_stop")["row"]
    row = dict(row)
    row["stations_acquired"] = 6
    row["stations_analysed"] = 6
    scored = c_scorer.score_row(c_scorer.normalize_row(row))
    assert scored["primary"] == "compliant_insufficient"
    assert scored["dimensions"]["strategy_insufficient"] is False


def test_ce08_real_shape_after_r_row_mapping(tmp_path, monkeypatch):
    """R 反馈轮真实形态：结转 1 条（已排除）→ 合规不足，不是数据不可得。

    这是对 v1 中 T3 R 分支行被误判为 data_unavailable 的回归锁。
    """
    res = {
        "question_id": "Q_usable", "status": "succeeded",
        "verdict": "pick_unknown", "field_answered": "method_applicability",
        "terminated_by": "unanswered_no_data",
        "answered": {"answered": False, "value": "unknown",
                     "field": "method_applicability"},
        "coincidence": None, "field_value": "unknown",
        "stations_acquired": 1,
        # 反馈轮结果不带 n_eligible；runner 从 run 的 discovery 读。
        "run_id": "r-test-ce08",
        "layers": {"excluded_candidates": ["27827bf6e7dc"]},
        # HOOD 已分析但被用户排除（状态字段见 stations 列表）。
        "stations": [{"station": "HOOD", "status": "analysed",
                      "excluded": True,
                      "candidate_id": "27827bf6e7dc"}],
        "http_attempts": 0, "logical_acquisitions": 0,
    }
    discovery_dir = tmp_path / "r-test-ce08"
    discovery_dir.mkdir()
    (discovery_dir / "discovery.json").write_text(
        json.dumps({"n_eligible": 1}))
    import runlog
    monkeypatch.setattr(runlog, "run_dir", lambda rid: discovery_dir)
    raw = run_c._raw_row("R", res)
    assert raw["n_eligible"] == 1
    assert raw["stations_analysed"] == 1
    contract = {"question_id": "Q_usable", "ceiling": 6,
                "acquisition_allowed": False}
    norm = c_scorer.normalize_row(raw, contract)
    scored = c_scorer.score_row(norm)
    assert scored["primary"] == "compliant_insufficient"
    assert scored["dimensions"]["data_unavailable"] is False


def test_r_first_row_maps_analysed_from_stations():
    res = {
        "question_id": "Q_propagating", "status": "succeeded",
        "verdict": "coherent_multistation", "field_answered": "verdict",
        "terminated_by": "coherence",
        "answered": {"answered": True, "value": "coherent_multistation",
                     "field": "verdict"},
        "coincidence": 6, "field_value": "coherent_multistation",
        "n_eligible": 54, "stations_acquired": 6,
        "stations": [{"station": s, "status": "analysed"}
                     for s in ["OLGA", "MCW", "ORCA", "LUMI", "GUEM",
                               "TURTL"]],
        "layers": {},
        "http_attempts": 0, "logical_acquisitions": 0,
    }
    raw = run_c._raw_row("R", res)
    assert raw["stations_analysed"] == 6
    scored = c_scorer.score_row(c_scorer.normalize_row(
        raw, {"question_id": "Q_propagating"}))
    assert scored["primary"] == "field_completed"


def test_expected_verdict_only_gates_completed_deliveries():
    """期望判决只对照 field_completed 的行：诚实 unknown 不算错误交付。"""
    row = _counterexample("CE-09_partial_acquisition_stop")["row"]
    row = dict(row)
    row["expected_verdict"] = "coherent_multistation"
    scored = c_scorer.score_row(c_scorer.normalize_row(row))
    # not_decidable + task_completed=False：不是 wrong_delivery。
    assert scored["dimensions"]["wrong_delivery"] is False
    assert scored["primary"] == "strategy_insufficient"
    # 换成完成了但答错 → wrong_delivery。
    row2 = dict(row)
    row2.update({
        "verdict": "single_station_only",
        "delivered": {"verdict": "single_station_only",
                      "field": "verdict", "task_completed": True},
        "answered": {"answered": True, "value": "single_station_only",
                     "field": "verdict"},
        "terminated_by": "coherence",
    })
    scored2 = c_scorer.score_row(c_scorer.normalize_row(row2))
    assert scored2["dimensions"]["wrong_delivery"] is True


def test_subset_number_claim_is_not_all_scope():
    """v2 排查发现的误报形态：“两个健康台”是子集指称。

    模型明确点名 OLGA unstable、再说“另两个健康三分量台”，事实清单只有
    OLGA 一台的稳定性记录——不能拿子集指称当全称断言推翻。
    """
    text = ("The nearest station (OLGA, 0.69 km) carries a constant/dead "
            "horizontal component that fails the three-component pick and "
            "produces a split, unstable onset, while the two healthy "
            "three-component stations support the band.")
    facts = [{"kind": "station_stability", "station": "OLGA",
              "state": "unstable"}]
    conflicts, _checked = c_scorer.check_explanation_facts(text, facts)
    assert conflicts == []
    # 数词等于清单台数时仍是全称断言（E1 形态：4 台清单说“四台健康”）。
    text2 = ("Four healthy three-component stations all show stable "
             "onsets within the window.")
    facts2 = [{"kind": "station_stability", "station": s, "state": st}
              for s, st in [("ORCA", "stable"), ("MCW", "unstable_bimodal"),
                            ("OLGA", "stable"), ("LUMI", "stable")]]
    conflicts2, _ = c_scorer.check_explanation_facts(text2, facts2)
    assert conflicts2 and conflicts2[0]["assertion"] == "all_stations_stable"


def test_unstable_word_not_matched_as_stable():
    """“OLGA ... unstable”不得命中点名稳定断言（子串误报）。"""
    facts = [{"kind": "station_stability", "station": "OLGA",
              "state": "unstable"}]
    text = "OLGA produces a split, unstable onset in this band."
    conflicts, _ = c_scorer.check_explanation_facts(text, facts)
    assert conflicts == []
    # 真正点名稳定且与清单冲突仍要命中。
    text2 = "MCW is stable and reliable in this window."
    facts2 = [{"kind": "station_stability", "station": "MCW",
               "state": "unstable_bimodal"}]
    conflicts2, _ = c_scorer.check_explanation_facts(text2, facts2)
    assert conflicts2 and conflicts2[0]["assertion"] == "station_stable"


def test_channel_health_is_not_station_stability():
    """“all channels healthy”是通道完整性（另一维度且为真），不是稳定性断言。"""
    facts = [{"kind": "station_stability", "station": "MCW",
              "state": "unstable_bimodal"}]
    text = ("Two stations both give clean 3C onsets, all channels healthy "
            "with no integrity flags.")
    conflicts, _ = c_scorer.check_explanation_facts(text, facts)
    assert conflicts == []
    # “all show stable onsets”是真稳定性全称断言，仍要命中（E1 形态）。
    text2 = "All six stations show stable onsets in the band."
    conflicts2, _ = c_scorer.check_explanation_facts(
        text2, facts + [{"kind": "station_stability", "station": "OLGA",
                         "state": "stable"}])
    assert conflicts2 and conflicts2[0]["assertion"] == "all_stations_stable"
