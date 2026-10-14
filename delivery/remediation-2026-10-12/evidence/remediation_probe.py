"""Free counterexample probe for the 2026-10-12 remediation packages A/B/C.

Read-only with respect to the product: every state is created under
TemporaryDirectory; production functions are called with saved local material and
stubbed launchers. No provider, AGH daemon or FDSN request is launched.

Run from the project root:
  .venv/bin/python -B delivery/remediation-2026-10-12/evidence/remediation_probe.py

Exit code is 0 only when every `expect` in the report is satisfied; the JSON
report is written next to this file.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).with_suffix(".json")
SRC_RUNLOG = ROOT / "main/runlog"
BASE = "r-ab5d401974765197"      # one acquired station, onset computed
V3 = "r-c8f9ca0979c35163"        # three attempts, frozen cap 3
INSTR = "r-d73eacd190d0950a"     # real run whose onset ref was unverified

report: dict = {"schema": "remediation-probe/v1", "network_or_model_calls": 0,
                "checks": {}}


def record(name, passed, detail):
    report["checks"][name] = {"pass": bool(passed), "detail": detail}


with tempfile.TemporaryDirectory(prefix="remediation-") as temp:
    temp = Path(temp)
    runlog_dir = temp / "runlog"
    runlog_dir.mkdir()
    for rid in (BASE, V3, INSTR):
        shutil.copytree(SRC_RUNLOG / rid, runlog_dir / rid)
    os.environ.update(
        RUNLOG_DIR=str(runlog_dir), EQ_CACHE_DIR=str(temp / "cache"),
        EQ_STRICT_OFFLINE="1", MPLCONFIGDIR=str(temp / "mpl"),
        PYTHONDONTWRITEBYTECODE="1")
    sys.path.insert(0, str(ROOT / "main"))
    sys.path.insert(0, str(ROOT / "explore/probes"))

    import core  # noqa: E402
    # `core` puts prototype/ first for its own legacy imports; that directory also
    # has an unrelated mcp_server.py, so the product module must be loaded by path.
    import importlib.util  # noqa: E402
    _mcp_spec = importlib.util.spec_from_file_location(
        "product_mcp_server", ROOT / "main/mcp_server.py")
    mcp_server = importlib.util.module_from_spec(_mcp_spec)
    sys.modules["product_mcp_server"] = mcp_server
    _mcp_spec.loader.exec_module(mcp_server)
    import execution_conditions as EC  # noqa: E402
    import finish_audit  # noqa: E402
    import prepare_agent  # noqa: E402
    import resource_limits  # noqa: E402
    import review  # noqa: E402
    import run_agent_arm as agent  # noqa: E402
    import runlog  # noqa: E402
    import server  # noqa: E402
    import task as taskmod  # noqa: E402

    def load(path):
        return json.loads(Path(path).read_text())

    base_folder = runlog_dir / BASE
    base_task = load(base_folder / "task.json")
    base_state = load(base_folder / "mcp_state.json")

    # ---------------------------------------------------------------- A: entry
    # Capture the execution parameters the real product entry would use, with the
    # runner itself stubbed so no model or driver is launched.
    captured = {}
    real_prepare_task = prepare_agent.prepare_task
    real_execute = agent._execute

    def fake_prepare_task(frozen, run_id=None, arm="adaptive_agent_open",
                          analysis_level="full"):
        captured["prepare_task"] = {"arm": arm, "analysis_level": analysis_level}
        return {"run_id": "stub-run", "task_id": frozen.get("task_id", "t"),
                "analysis_level": analysis_level}

    def fake_execute(case_id, prepared, question_id, question,
                     driver_script="agent_run.mjs", profile=None):
        captured["execute"] = {"driver_script": driver_script, "profile": profile}
        return {"run_id": "stub-run", "task_id": prepared.get("task_id"),
                "question": question, "question_id": question_id,
                "status": runlog.SUCCEEDED, "delivered": {}, "acquisition": {},
                "model_claim": {}, "validator_verdict": {}, "stations": [],
                "stations_acquired": 0}

    prepare_agent.prepare_task = fake_prepare_task
    agent._execute = fake_execute
    try:
        agent.run_user_task(copy.deepcopy(base_task), "这站能用吗")
        first_user = dict(captured)
        captured.clear()
        try:
            agent.run_one("S1_sparse_2003", "这个窗口可用吗")
            first_case = dict(captured)
        except Exception as exc:  # noqa: BLE001
            first_case = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        prepare_agent.prepare_task = real_prepare_task
        agent._execute = real_execute

    ok_a1 = all(
        entry.get("execute", {}).get("driver_script") == "agent_run_optimization.mjs"
        and entry.get("execute", {}).get("profile")
        and entry.get("prepare_task", {}).get("analysis_level") == "quality_only"
        for entry in (first_user, first_case) if "error" not in entry)
    record("A_first_round_uses_optimization_driver", ok_a1,
           {"run_user_task": first_user, "run_one": first_case})

    # A feedback run that inherits the parent's actual recorded configuration.
    parent_cfg_folder = runlog_dir / "parent-with-config"
    shutil.copytree(base_folder, parent_cfg_folder)
    (parent_cfg_folder / "agent_config.json").write_text(json.dumps({
        "schema": "agent-execution-config/v1", "profile": "A1",
        "driver": "agent_run_optimization.mjs", "analysis_level": "quality_only",
        "source": "first_round"}))

    fb_capture = {}
    real_prepare_followup = prepare_agent.prepare_followup

    def fake_prepare_followup(parent_run_id, instruction, **kwargs):
        fb_capture["prepare_followup"] = {"parent_run_id": parent_run_id,
                                          "kwargs": {k: v for k, v in kwargs.items()
                                                     if k != "change"}}
        return {"run_id": "stub-child", "task_id": "t-child",
                "parent_run_id": parent_run_id}

    def fake_execute_fb(case_id, prepared, question_id, question,
                        driver_script="agent_run.mjs", profile=None, **kwargs):
        fb_capture["execute"] = {"driver_script": driver_script, "profile": profile,
                                 "analysis_level": kwargs.get("analysis_level"),
                                 "decision": kwargs.get("decision")}
        return {"run_id": "stub-child", "task_id": "t-child", "question": question,
                "question_id": question_id, "status": runlog.SUCCEEDED,
                "delivered": {}, "acquisition": {}, "model_claim": {},
                "validator_verdict": {}, "stations": [], "stations_acquired": 0}

    prepare_agent.prepare_followup = fake_prepare_followup
    agent._execute = fake_execute_fb
    feedback_kinds = {
        "exclude": {"kind": "exclude",
                    "candidate_id": base_state["acquired"][0]["candidate_id"]},
        "stability_bands": {"kind": "stability_bands",
                            "target_band_hz": [3.0, 12.0]},
        "method_goal": {"kind": "method_goal", "method_goal": "vertical_onset"},
    }
    fb_capture["per_kind"] = {}
    try:
        for label, change in feedback_kinds.items():
            fb_capture["per_kind"][label] = {}
            seen_here = fb_capture["per_kind"][label]
            # Re-point the capture target for this iteration.
            def capture_prepare(parent_run_id, instruction, _seen=seen_here, **kwargs):
                _seen["prepare_followup"] = {
                    "parent_run_id": parent_run_id,
                    "analysis_level": kwargs.get("analysis_level")}
                return {"run_id": "stub-child", "task_id": "t-child",
                        "parent_run_id": parent_run_id}

            def capture_execute(case_id, prepared, question_id, question,
                                _seen=seen_here, driver_script="agent_run.mjs",
                                profile=None, **kwargs):
                _seen["execute"] = {"driver_script": driver_script,
                                    "profile": profile}
                return {"run_id": "stub-child", "task_id": "t-child",
                        "question": question, "question_id": question_id,
                        "status": runlog.SUCCEEDED, "delivered": {},
                        "acquisition": {}, "model_claim": {},
                        "validator_verdict": {}, "stations": [],
                        "stations_acquired": 0}

            prepare_agent.prepare_followup = capture_prepare
            agent._execute = capture_execute
            agent.run_followup("parent-with-config", f"改条件 {label}", change=change)
    except Exception as exc:  # noqa: BLE001
        fb_capture["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        prepare_agent.prepare_followup = real_prepare_followup
        agent._execute = real_execute

    per_kind = fb_capture.get("per_kind", {})
    ok_a2 = bool(per_kind) and all(
        entry.get("execute", {}).get("driver_script") == "agent_run_optimization.mjs"
        and entry.get("execute", {}).get("profile") == "A1"
        and entry.get("prepare_followup", {}).get("analysis_level") == "quality_only"
        for entry in per_kind.values())
    record("A_followup_inherits_parent_config", ok_a2, per_kind)

    # --------------------------------------------- B: feedback contract
    cid = base_state["acquired"][0]["candidate_id"]
    try:
        prepared = prepare_agent.prepare_followup(
            BASE, "排除这个台站",
            change={"kind": "exclude", "candidate_id": cid},
            arm="adaptive_agent_a1_followup", analysis_level="quality_only")
        child_folder = runlog_dir / prepared["run_id"]
        child_state = load(child_folder / "mcp_state.json")
        child_task = load(child_folder / "task.json")
        child_requested = child_state.get("requested_conditions") or {}
        b_detail = {
            "child_acquisition_allowed_state": child_requested.get("acquisition_allowed"),
            "child_acquisition_allowed_task": child_task.get("acquisition_allowed"),
            "child_requested_acquisition_allowed":
                (child_task.get("requested_conditions") or {}).get("acquisition_allowed"),
            "prompt_says_no": ("acquisition allowed: NO" in
                               (child_folder / "mcp_state.initial.json").read_text()),
        }
    except Exception as exc:  # noqa: BLE001
        b_detail = {"error": f"{type(exc).__name__}: {exc}"}
    ok_b1 = (b_detail.get("child_acquisition_allowed_state") is False
             and b_detail.get("child_acquisition_allowed_task") is False)
    record("B_followup_defaults_to_no_new_acquisition", ok_b1, b_detail)

    # The MCP guard must refuse a new candidate BEFORE any external request.
    gate_state = copy.deepcopy(base_state)
    gate_state["requested_conditions"]["acquisition_allowed"] = False
    allowed, refusal = mcp_server._acquisition_gate(gate_state, "some-new-candidate")
    ok_b2 = (allowed is False and refusal and refusal.get("http_issued") is False
             and refusal.get("reason") == "acquisition_not_allowed")
    record("B_guard_refuses_before_http", ok_b2,
           {"allowed": allowed, "refusal": refusal})

    # Candidate ceiling counts every attempt, including failed ones.
    lim = resource_limits.limits()
    ceil = resource_limits.unique_candidate_ceiling(3)
    gate_state2 = copy.deepcopy(base_state)
    gate_state2["requested_conditions"]["acquisition_allowed"] = True
    gate_state2["requested_conditions"]["max_unique_candidates_episode"] = 3
    gate_state2["attempted_candidate_ids_episode"] = ["a1", "a2", "a3"]
    allowed2, refusal2 = mcp_server._acquisition_gate(gate_state2, "a4")
    ok_b3 = (allowed2 is False and refusal2
             and refusal2.get("reason") == "candidate_ceiling"
             and refusal2.get("http_issued") is False)
    record("B_ceiling_refuses_extra_candidate", ok_b3,
           {"ceiling": ceil, "global": lim["max_unique_candidates_episode"],
            "allowed": allowed2, "refusal": refusal2})

    # An explicit single-station scope must refuse an outside station even when the
    # declared station did not resolve to any candidate id.
    gate_state3 = copy.deepcopy(base_state)
    gate_state3["requested_conditions"]["scope"] = {
        "scope_kind": "explicit_candidates", "stations": ["NOPE"], "candidate_ids": []}
    allowed3, refusal3 = mcp_server._acquisition_gate(gate_state3, "outside-id")
    ok_b4 = (allowed3 is False and refusal3
             and refusal3.get("reason") == "outside_scope")
    record("B_explicit_scope_refuses_outside_candidate", ok_b4,
           {"allowed": allowed3, "refusal": refusal3})

    # Chained feedback keeps the denial and accumulates the episode attempts; the
    # parent's own files are not rewritten.
    import hashlib

    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    parent_state_before = digest(base_folder / "mcp_state.json")
    child_id = None
    try:
        child = prepare_agent.prepare_followup(
            BASE, "排除这个台站",
            change={"kind": "exclude", "candidate_id": cid},
            analysis_level="quality_only")
        child_id = child["run_id"]
        grandchild = prepare_agent.prepare_followup(
            child_id, "目标频带改为 3–8 Hz",
            change={"kind": "band", "target_band_hz": [3.0, 8.0]},
            analysis_level="quality_only")
        grand_folder = runlog.run_dir(grandchild["run_id"])
        grand_state = load(grand_folder / "mcp_state.json")
        grand_requested = grand_state.get("requested_conditions") or {}
        chain_detail = {
            "grandchild_acquisition_allowed": grand_requested.get("acquisition_allowed"),
            "grandchild_attempts": grand_state.get("attempted_candidate_ids_episode"),
            "parent_state_unchanged": digest(base_folder / "mcp_state.json")
            == parent_state_before,
        }
        ok_b5 = (chain_detail["grandchild_acquisition_allowed"] is False
                 and cid in (chain_detail["grandchild_attempts"] or [])
                 and chain_detail["parent_state_unchanged"])
    except Exception as exc:  # noqa: BLE001
        chain_detail = {"error": f"{type(exc).__name__}: {exc}"}
        ok_b5 = False
    record("B_chained_feedback_keeps_permission_and_parent", ok_b5, chain_detail)

    # Stored-waveform recomputation still works under a no-fetch child: the onset
    # check is local, so a "reuse only" round is not a dead end.
    try:
        mcp_server.bind(child_id)
        onset = mcp_server.run_onset_check(cid)
        ok_b6 = onset.get("onset_computed") is True
        waveform_detail = {"onset_computed": onset.get("onset_computed"),
                           "onset_utc": onset.get("onset_utc"),
                           "onset_source": onset.get("onset_source"),
                           "condition_id": onset.get("condition_id")}
    except Exception as exc:  # noqa: BLE001
        ok_b6 = False
        waveform_detail = {"error": f"{type(exc).__name__}: {exc}"}
    record("B_stored_waveform_recompute_still_works", ok_b6, waveform_detail)

    # The HTTP layer must keep the NORMALIZED change, not merely validate and drop.
    server_state = copy.deepcopy(base_state)
    seen = {"worker": None}
    real_change = review.feedback_change
    normalized = real_change(server_state, {"kind": "exclude", "candidate_id": cid}, None)

    def fake_worker(parent_run_id, change, instruction):
        seen["worker"] = {"parent_run_id": parent_run_id, "change": change,
                          "instruction": instruction}
        return {"run_id": "stub-child", "task_id": "t-child",
                "question": "q", "question_id": "Q_usable",
                "status": runlog.SUCCEEDED, "delivered": {}, "acquisition": {},
                "model_claim": {}, "validator_verdict": {}, "stations": [],
                "stations_acquired": 0}

    class FakeHandler:
        pass

    real_agent_followup = server._agent_followup
    server._agent_followup = lambda *a, **k: fake_worker(*a, **k)
    real_feedback_change = review.feedback_change
    review.feedback_change = lambda *a, **k: normalized

    class SyncThread:
        """Run the feedback worker synchronously so the probe has no race."""

        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self.target, self.args, self.kwargs = target, args, kwargs or {}

        def start(self):
            if self.target:
                self.target(*self.args, **self.kwargs)

    real_thread = server.threading.Thread
    server.threading.Thread = SyncThread
    try:
        # Drive the do_POST followup branch through its real handler method with a
        # minimal fake request object.
        handler = server.Handler.__new__(server.Handler)
        sent = {}

        def send(status, payload):
            sent["status"] = status
            sent["payload"] = payload

        handler.send = send
        handler.followup({"parent_run_id": BASE,
                          "change": {"kind": "exclude", "candidate_id": cid},
                          "engine": "agent"})
        sent["worker_seen"] = seen["worker"]
        b5 = seen["worker"] is not None and seen["worker"]["change"] == normalized
        b5_detail = {"normalized": normalized, "sent": sent}
    except Exception as exc:  # noqa: BLE001
        b5 = False
        b5_detail = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        server._agent_followup = real_agent_followup
        review.feedback_change = real_feedback_change
        server.threading.Thread = real_thread
    record("B_server_keeps_normalized_change", b5, b5_detail)

    # --------------------------------------------- C: evidence and stop wiring
    def validate_probe(claim, state_mutator=None):
        folder = runlog_dir / "claim-probe"
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        probe_state = copy.deepcopy(base_state)
        probe_state["run_id"] = "claim-probe"
        probe_state["derived_evidence_checks"] = {}
        probe_state["stability_checks"] = {}
        for row in probe_state["acquired"]:
            row.update(onset_computed=False, onset_utc=None, stability=None,
                       stability_computed=False)
        if state_mutator:
            state_mutator(probe_state)
        (folder / "mcp_state.json").write_text(json.dumps(probe_state))
        (folder / "task.json").write_text(json.dumps(base_task))
        existing = load(folder / "mcp_state.json")
        return agent.validate("claim-probe", "", base_task,
                              existing["acquired"], {"answer": json.dumps(claim)},
                              "Q_usable"), probe_state

    vt = None
    try:
        vt = core.verdict_for_question(base_state["acquired"], "Q_usable", None,
                                       base_task)
    except Exception:  # noqa: BLE001
        vt = None
    requested = base_state["requested_conditions"]
    good_claim = {"conclusion": (vt or {}).get("verdict"),
                  "field": requested["required_field"],
                  "condition_id": requested["condition_id"],
                  "scope_candidate_ids": [cid],
                  "evidence_refs": [f"onset:{cid}"],
                  "stop_reason_code": "public_criterion_satisfied"}

    # C1: a reference to an onset that was never computed must be rejected.
    checked, _ = validate_probe(copy.deepcopy(good_claim))
    ok_c1 = checked["delivered"]["model_claim_accepted"] is False
    record("C_uncomputed_onset_ref_rejected", ok_c1,
           {"accepted": checked["delivered"]["model_claim_accepted"],
            "origin": checked["delivered"]["origin"],
            "checks": checked["acceptance_checks"],
            "evidence": checked["evidence_ref_report"]})

    # C2: a fabricated scope id must be rejected.
    bad_scope = copy.deepcopy(good_claim)
    bad_scope["scope_candidate_ids"] = ["fabricated-scope-id"]
    checked2, _ = validate_probe(bad_scope)
    ok_c2 = (checked2["delivered"]["model_claim_accepted"] is False
             and checked2["acceptance_checks"].get("scope_declared") is not True)
    record("C_fabricated_scope_rejected", ok_c2,
           {"accepted": checked2["delivered"]["model_claim_accepted"],
            "checks": checked2["acceptance_checks"]})

    # C3: an excluded observation's evidence must not be citable.
    excluded_state = copy.deepcopy(base_state)
    excluded_state["acquired"][0]["excluded"] = True
    ex_report = agent._evidence_refs([f"onset:{cid}"], excluded_state,
                                     excluded_state["requested_conditions"])
    ok_c3 = ex_report["all_valid"] is False
    record("C_excluded_ref_rejected", ok_c3, ex_report)

    # C4: omitting stop_reason_code must not be back-filled into acceptance.
    no_stop = copy.deepcopy(good_claim)
    no_stop.pop("stop_reason_code", None)
    checked4, _ = validate_probe(no_stop)
    ok_c4 = (checked4["delivered"]["model_claim_accepted"] is False
             and checked4["acceptance_checks"].get("stop_basis_legal") is not True)
    record("C_missing_stop_code_not_backfilled", ok_c4,
           {"accepted": checked4["delivered"]["model_claim_accepted"],
            "checks": checked4["acceptance_checks"]})

    # C5: a legal unknown whose facts justify it must finish; the stop basis is
    # derived from the run's real attempts/ceiling, not from the model's word.
    v3_state = load(runlog_dir / V3 / "mcp_state.json")
    v3_task = load(runlog_dir / V3 / "task.json")
    v3_val = load(runlog_dir / V3 / "validation.json")
    v3_before = finish_audit.audit_finish(v3_state, task=v3_task)
    v3_after = finish_audit.audit_finish(
        v3_state, task=v3_task,
        model_claim={"stop_reason_code": (v3_val.get("model_claim") or {})
                     .get("stop_reason_code")})
    ok_c5 = (v3_after["outcome"] == "legal_unknown" and v3_after["can_finish"]
             and v3_after.get("stop_basis"))
    record("C_stop_basis_derived_from_facts", ok_c5,
           {"attempts": len(v3_state.get("attempted_candidate_ids_episode") or []),
            "cap": v3_task.get("max_unique_candidates_episode"),
            "before": {"outcome": v3_before["outcome"],
                       "unmet": v3_before["unmet"]},
            "after": {"outcome": v3_after["outcome"],
                      "stop_basis": v3_after.get("stop_basis"),
                      "can_finish": v3_after["can_finish"]}})

    # C6: excluding the only participating station yields an empty scope that is a
    # correct legal unknown, not a fabricated success.
    empty_task = copy.deepcopy(base_task)
    empty_scope = EC.normalize_scope({"scope_kind": "explicit_candidates",
                                      "stations": ["CHIMA"],
                                      "candidate_ids": [cid]})
    empty_task["requested_conditions"] = EC.build(
        question_id="Q_usable", scope=empty_scope, method_goal="three_component_pick",
        target_band_hz=[1.0, 12.0], onset_method_band_hz=[1.0, 8.0],
        diagnostic_bands_hz=(base_state["requested_conditions"]
                             or {}).get("diagnostic_bands_hz"),
        acquisition_allowed=False, excluded_candidate_ids=[cid],
        max_unique_candidates_episode=6,
        requested_changes=[{"kind": "exclude", "candidate_id": cid}])
    empty_state = copy.deepcopy(base_state)
    empty_state["acquired"][0]["excluded"] = True
    empty_state["attempted_candidate_ids_episode"] = [cid]
    empty_state["requested_conditions"] = empty_task["requested_conditions"]
    # A real child state does not inherit the parent's tool events; clear the
    # copied ones so the probe measures THIS round's activity.
    empty_state["tool_events"] = []
    empty_state["tool_attempts"] = {}
    empty_state["contract_events"] = []
    empty_audit = finish_audit.audit_finish(empty_state, task=empty_task)
    ok_c6 = (empty_audit["outcome"] == "legal_unknown"
             and empty_audit["can_finish"]
             and not empty_audit.get("participating_candidate_ids"))
    record("C_empty_scope_correct_unknown", ok_c6,
           {"outcome": empty_audit["outcome"], "can_finish": empty_audit["can_finish"],
            "stop_basis": empty_audit.get("stop_basis"),
            "unmet": empty_audit["unmet"]})

    # C7 (review-9 blocker): a run that has acquired NOTHING, while an in-scope
    # candidate is still acquirable, may not finish as scope_exhausted.
    unstarted = prepare_agent.model_state(
        "unstarted", {**base_task,
                      "requested_conditions": base_state["requested_conditions"]},
        load(base_folder / "discovery.json"), analysis_level="quality_only")
    ufolder = runlog.run_dir("unstarted")
    ufolder.mkdir(parents=True, exist_ok=True)
    (ufolder / "mcp_state.json").write_text(json.dumps(unstarted))
    (ufolder / "task.json").write_text(json.dumps(base_task))
    uverdict = core.verdict_for_question([], "Q_usable", None, base_task)
    uclaim = {"conclusion": uverdict["verdict"], "field": "method_applicability",
              "condition_id": base_state["requested_conditions"]["condition_id"],
              "scope_candidate_ids": [], "evidence_refs": [],
              "stop_reason_code": "scope_exhausted"}
    uresult = agent.validate("unstarted", "", base_task, [],
                             {"answer": json.dumps(uclaim)}, "Q_usable")
    ok_c7 = (uresult["delivered"]["model_claim_accepted"] is False
             and uresult["finish_audit"]["can_finish"] is False
             and uresult["finish_audit"]["outcome"] == "strategy_insufficient"
             and uresult["acceptance_checks"].get("scope_valid") is not True)
    record("C_unstarted_zero_acquisition_rejected", ok_c7,
           {"accepted": uresult["delivered"]["model_claim_accepted"],
            "outcome": uresult["finish_audit"]["outcome"],
            "can_finish": uresult["finish_audit"]["can_finish"],
            "stop_bases": uresult["finish_audit"]["stop_bases"],
            "scope": uresult["scope_report"],
            "acceptance": uresult["acceptance_checks"]})

    # C8 (review-9 §5): the child TASK carries the same candidate ceiling its state
    # and the MCP guard enforce.
    cap_parent = "cap-parent"
    cap_folder = runlog.run_dir(cap_parent)
    cap_folder.mkdir(parents=True, exist_ok=True)
    capped_task = copy.deepcopy(base_task)
    capped_task["max_unique_candidates_episode"] = 3
    capped = copy.deepcopy(base_state)
    cap_requested = copy.deepcopy(capped["requested_conditions"])
    cap_requested["max_unique_candidates_episode"] = 3
    cap_requested["condition_id"] = EC.condition_id(cap_requested)
    capped["requested_conditions"] = cap_requested
    capped_task["requested_conditions"] = cap_requested
    capped["attempted_candidate_ids_episode"] = [cid, "failed-2", "failed-3"]
    (cap_folder / "task.json").write_text(json.dumps(capped_task))
    (cap_folder / "mcp_state.json").write_text(json.dumps(capped))
    (cap_folder / "discovery.json").write_text(
        (base_folder / "discovery.json").read_text())
    (cap_folder / "agent_config.json").write_text(json.dumps(
        {"profile": "A1", "analysis_level": "quality_only", "recorded": True}))
    cap_prepared = prepare_agent.prepare_followup(
        cap_parent, "只改目标",
        change={"kind": "method_goal", "method_goal": "vertical_onset"},
        analysis_level="quality_only")
    cap_child_task = load(runlog.run_dir(cap_prepared["run_id"]) / "task.json")
    cap_child_state = load(runlog.run_dir(cap_prepared["run_id"]) / "mcp_state.json")
    ok_c8 = (cap_child_task.get("max_unique_candidates_episode") == 3
             and (cap_child_state.get("requested_conditions") or {})
             .get("max_unique_candidates_episode") == 3)
    record("C_child_task_cap_matches_state", ok_c8,
           {"child_task_cap": cap_child_task.get("max_unique_candidates_episode"),
            "child_state_cap": (cap_child_state.get("requested_conditions") or {})
            .get("max_unique_candidates_episode"),
            "attempts": cap_child_state.get("attempted_candidate_ids_episode")})

    # --------------------------------------------- D: terminal state and cleanup
    # D1: the outer watchdog stops only the daemon named by THIS run's ownership
    # record, using that run's own AGH_HOME — never a process-name sweep.
    watchdog_run = "watchdog-run"
    watchdog_folder = runlog.run_dir(watchdog_run)
    watchdog_folder.mkdir(parents=True, exist_ok=True)
    owned_home = temp / "owned-home"
    owned_home.mkdir()
    (watchdog_folder / "runtime-owner.json").write_text(json.dumps({
        "run_id": watchdog_run, "agh_home": str(owned_home),
        "owner_id": "owner-xyz", "socket_path": "/tmp/agnesd.sock"}))
    captured_stop = {}

    class FakeProc:
        returncode = 0
        stdout = "stopped"
        stderr = ""

    real_subprocess_run = agent.subprocess.run

    def fake_stop_run(cmd, **kwargs):
        captured_stop["cmd"] = list(cmd)
        captured_stop["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    agent.subprocess.run = fake_stop_run
    try:
        stop_result = agent._stop_owned_daemon(watchdog_run)
    finally:
        agent.subprocess.run = real_subprocess_run
    cmd_text = " ".join(str(x) for x in captured_stop.get("cmd", []))
    ok_d1 = (stop_result.get("stopped") is True
             and "daemon stop" in cmd_text
             and "agnes.mjs" in cmd_text
             and "pkill" not in cmd_text
             and captured_stop.get("env", {}).get("AGH_HOME") == str(owned_home)
             and (watchdog_folder / "watchdog_stop.json").exists())
    record("D_watchdog_stops_only_owned_home", ok_d1,
           {"result": stop_result, "cmd": cmd_text,
            "agh_home": captured_stop.get("env", {}).get("AGH_HOME")})

    # D1b: a runtime-owner record that names a DIFFERENT run must not be stopped.
    mismatch_run = "watchdog-mismatch"
    mismatch_folder = runlog.run_dir(mismatch_run)
    mismatch_folder.mkdir(parents=True, exist_ok=True)
    (mismatch_folder / "runtime-owner.json").write_text(json.dumps({
        "run_id": "some-other-run", "agh_home": str(owned_home),
        "owner_id": "owner-other", "socket_path": "/tmp/other.sock"}))
    captured_stop.clear()
    agent.subprocess.run = fake_stop_run
    try:
        mismatch_result = agent._stop_owned_daemon(mismatch_run)
    finally:
        agent.subprocess.run = real_subprocess_run
    ok_d1b = (mismatch_result.get("stopped") is False
              and mismatch_result.get("reason") == "runtime_owner_run_mismatch"
              and not captured_stop)
    record("D_watchdog_rejects_owner_run_mismatch", ok_d1b,
           {"result": mismatch_result, "stop_invoked": bool(captured_stop)})

    # D2: a driver record whose cleanup failed must not be recorded as success;
    # the original failure and the cleanup state are both preserved.
    import core as _core  # already imported, alias for clarity  # noqa: F811
    real_discover = _core.discover

    def fake_discover(case, task=None):
        eligible = [{"candidate_id": f"cand{i:02d}", "network": "UW",
                     "station": f"S{i:02d}", "distance_km": 1.0 * i,
                     "family": "HH?", "analysis_eligible": True,
                     "three_component": True, "vertical_only": False,
                     "reason": "synthetic",
                     "band_support": {"supported": True, "nyquist_hz": 50.0,
                                      "target_band_hz": [1.0, 12.0],
                                      "sampling_rate_hz": 100.0,
                                      "required_rate_hz": 24.0, "reason": "ok"}}
                    for i in range(1, 4)]
        return {"station_query_url": "synthetic://stub", "station_rows": 3,
                "candidates": eligible, "eligible": eligible,
                "n_eligible": len(eligible), "target_band_hz": [1.0, 12.0],
                "method_band_hz": [1.0, 8.0]}

    _core.discover = fake_discover
    try:
        prepared_d = prepare_agent.prepare_task(
            {**base_task, "task_id": "t-cleanup"}, analysis_level="quality_only")
    finally:
        _core.discover = real_discover

    def fake_cleanup_run(cmd, **kwargs):
        run_id = cmd[2]
        folder = runlog.run_dir(run_id)
        (folder / f"agent_execution_A1_{run_id}.json").write_text(json.dumps({
            "RUN": run_id, "status": "failed", "failure_kind": "cleanup_failed",
            "answer": None, "fatal": None,
            "cleanup": {"stop_confirmed": False, "cleanup_ok": False,
                        "cleanup_needed": True, "root_removed": False,
                        "cleanup_errors": ["stop_daemon: injected failure"],
                        "log": []},
            "cleanup_ok": False, "cleanup_needed": True}))
        return SimpleProc()

    class SimpleProc:
        returncode = 4
        stdout = "{}"
        stderr = "cleanup failed"

    agent.subprocess.run = fake_cleanup_run
    try:
        row_d = agent._execute("E1_orcas_event", prepared_d, "Q_propagating",
                               base_task["user_question"],
                               driver_script="agent_run_optimization.mjs",
                               profile="A1")
    finally:
        agent.subprocess.run = real_subprocess_run
    cleanup_artifact = runlog.run_dir(prepared_d["run_id"]) / "resource_cleanup.json"
    artifact = (json.loads(cleanup_artifact.read_text())
                if cleanup_artifact.exists() else {})
    ok_d2 = (row_d.get("status") == "failed"
             and row_d.get("failure_kind") == "cleanup_failed"
             and (row_d.get("cleanup") or {}).get("ok") is False
             and artifact.get("cleanup_ok") is False
             and artifact.get("cleanup_needed") is True)
    record("D_cleanup_failure_not_success", ok_d2,
           {"status": row_d.get("status"), "failure_kind": row_d.get("failure_kind"),
            "cleanup": row_d.get("cleanup"), "resource_cleanup": artifact})

    report["summary"] = {
        "total": len(report["checks"]),
        "passed": sum(1 for c in report["checks"].values() if c["pass"]),
        "failed": sorted(k for k, c in report["checks"].items() if not c["pass"]),
    }

OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
print(json.dumps(report, indent=2, ensure_ascii=False))
sys.exit(0 if not report["summary"]["failed"] else 1)