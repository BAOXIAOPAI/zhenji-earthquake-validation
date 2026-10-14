"""Drive the real AGH adaptive arm, one run id at a time.

Each invocation allocates a fresh run id, freezes a task from the case plus the
caller's question, runs the AGH session against that run, and then records three
SEPARATE things (evaluation P0-2):

  acquisition   what the model chose to acquire, and whether that was enough
  model_claim   the conclusion the model stated in its own words
  delivered     the result the validator is willing to stand behind

The validator may reject the model's claim; that rejection is recorded and shown
rather than hidden behind a re-computed label. A failed session is recorded as
failed and keeps whatever evidence and cost it incurred.

Usage:
  run_agent_arm.py <case_id> [question...]
  AGENT_VARIANT=stated run_agent_arm.py ...
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import evidence_state  # noqa: E402
import execution_conditions as EC  # noqa: E402
import finish_audit  # noqa: E402
import prepare_agent
import task as taskmod  # noqa: E402
import runlog  # noqa: E402

VARIANT = os.environ.get("AGENT_VARIANT", "open")
OUT = ROOT / "main/out"

# The product's own Agent strategy. The optimisation experiment selected A1 from a
# set of candidates; for the submitted product the point is that the *same* frozen
# driver, prompt template and output contract serve the ordinary web entry, not
# that A1 out-scored anything. `quality_only` is the thin preparation split: the
# observation is fetched and hashed, and the waveform diagnostics are left to the
# agent's own tool calls instead of being pre-computed.
PRODUCT_PROFILE = (os.environ.get("AGENT_PRODUCT_PROFILE") or "A1").upper()
PRODUCT_DRIVER = "agent_run_optimization.mjs"
PRODUCT_ANALYSIS_LEVEL = "quality_only"

EXIT_TO_FAILURE = {1: "model_fatal", 2: "internal_error", 3: "timeout",
                   4: "cleanup_failed"}


def _parse_answer(answer):
    if isinstance(answer, str) and answer.strip().startswith("{"):
        try:
            return json.loads(answer)
        except json.JSONDecodeError:
            return None
    return None


def _tool_activity(run_id):
    """Interface-level tool failures for this run, read from its own tool log.

    Kept separate from `acquisition`: a tool that raised is an interface failure,
    while an acquisition that completed and returned no data is a data fact. The
    previous records merged the two and then read the result as "the model chose
    not to gather evidence".

    Recovery is judged from the EVENT ORDER, per tool and per operated object
    (review-4 A4): cumulative counts like {start:2, ok:1, error:1} cannot
    distinguish error→ok (recovered) from ok→error (NOT recovered). An error is
    recovered only by a LATER ok of the same tool on the same target; a success
    on another candidate settles nothing about the failed one by itself — whether
    that gap matters is then judged from the asked field's evidence.
    """
    path = runlog.run_dir(run_id) / "tool_calls.jsonl"
    if not path.exists():
        return {"tool_errors": 0, "tools_with_errors": [], "attempts": {},
                "recovery": {"order_known": True, "recovered": [], "unrecovered": []}}
    errors, attempts, events = [], {}, []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        tool = entry.get("tool")
        phase = entry.get("phase")
        if tool and phase:
            counts = attempts.setdefault(tool, {"start": 0, "ok": 0, "error": 0})
            # A5 three-way consistency: every tool reports all three phases
            # (zeros included), so the validation record's shape equals the
            # persisted state's shape instead of silently dropping `error: 0`.
            counts[phase] = counts.get(phase, 0) + 1
            if phase in ("ok", "error"):
                target = (entry.get("arguments") or {}).get("candidate_id")
                events.append({"tool": tool, "phase": phase, "target": target})
        if entry.get("phase") == "error":
            errors.append({"tool": tool, "error_type": entry.get("error_type"),
                           "error": entry.get("error")})
    recovered, unrecovered = [], []
    for i, event in enumerate(events):
        if event["phase"] != "error":
            continue
        healed = any(later["phase"] == "ok" and later["tool"] == event["tool"]
                     and later["target"] == event["target"]
                     for later in events[i + 1:])
        label = (event["tool"]
                 + (f"({event['target']})" if event.get("target") else ""))
        (recovered if healed else unrecovered).append(label)
    return {"tool_errors": len(errors),
            "tools_with_errors": sorted({e["tool"] for e in errors}),
            "attempts": attempts,
            "recovery": {"order_known": True,
                         "recovered": recovered, "unrecovered": unrecovered}}


def row_parent(folder) -> str | None:
    """The run's recorded parent, read from its own record rather than inferred."""
    path = Path(folder) / "run.json"
    if not path.exists():
        return None
    return json.loads(path.read_text()).get("parent_run_id")


def execution_config(run_id) -> dict:
    """The execution configuration a run ACTUALLY used, read from its own record.

    A run's profile/driver/analysis-level must be a recorded fact, not something a
    later reader guesses from the arm name. Runs written before this file existed
    report `recorded: false`, and their follow-ups are upgraded explicitly instead
    of being retro-labelled.
    """
    folder = runlog.run_dir(run_id)
    path = folder / "agent_config.json"
    if path.exists():
        try:
            config = json.loads(path.read_text())
        except ValueError:
            config = {}
        config.setdefault("recorded", True)
        return config
    # Legacy run: reconstruct what is knowable, and say plainly that it is derived.
    derived = {"schema": "agent-execution-config/v1", "recorded": False,
               "source": "derived_from_legacy_run"}
    if (folder / "run.json").exists():
        arm = (json.loads((folder / "run.json").read_text()).get("arm") or "")
        derived["arm"] = arm
        if arm.startswith("adaptive_agent_"):
            suffix = arm[len("adaptive_agent_"):]
            if suffix and suffix.upper() in ("A0", "A1", "AT"):
                derived["profile"] = suffix.upper()
    try:
        state = json.loads((folder / "mcp_state.json").read_text())
        derived["analysis_level"] = state.get("analysis_level")
    except (OSError, ValueError):
        pass
    template = (folder / "prompt_binding.json")
    if template.exists():
        try:
            binding = json.loads(template.read_text())
            derived["driver"] = ("agent_run_optimization.mjs"
                                 if binding.get("policy") else None)
            derived.setdefault("profile", binding.get("profile"))
        except ValueError:
            pass
    return derived


def record_execution_config(run_id, *, driver_script, profile, analysis_level,
                            model, inherited_from=None, decision=None) -> dict:
    """Write this run's actual execution configuration, once, before the driver.

    Written first so even a run that dies in the driver has a truthful record of
    what it was launched with.
    """
    payload = {
        "schema": "agent-execution-config/v1",
        "run_id": run_id,
        "driver": driver_script,
        "profile": (profile.upper() if profile else None),
        "analysis_level": analysis_level or "full",
        "model": model,
        "arm": (f"adaptive_agent_{(profile or PRODUCT_PROFILE).lower()}"
                if driver_script == PRODUCT_DRIVER
                else f"adaptive_agent_{VARIANT}"),
        "product_profile": PRODUCT_PROFILE,
        "recorded_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
    }
    if inherited_from is not None:
        payload["inherited_from_parent"] = inherited_from
        payload["inheritance_decision"] = decision
    runlog.write_once(runlog.run_dir(run_id) / "agent_config.json", payload)
    return payload


def _followup_config(parent_run_id) -> dict:
    """The child's execution configuration, inherited from the parent's record.

    The parent's ACTUAL profile and analysis level are reused when it has them; a
    legacy parent without a record is upgraded to the product default and the
    upgrade is named in the child, never written back onto the parent.
    """
    parent = execution_config(parent_run_id)
    profile = parent.get("profile") if parent.get("recorded") else None
    level = parent.get("analysis_level") if parent.get("recorded") else None
    decision = "inherited_from_parent_config"
    inherited = bool(profile and level)
    if not inherited:
        profile = profile or PRODUCT_PROFILE
        level = level or PRODUCT_ANALYSIS_LEVEL
        decision = ("upgraded_legacy_parent_to_product_default"
                    if not parent.get("recorded")
                    else "completed_missing_parent_config_with_product_default")
    return {"profile": profile, "analysis_level": level,
            "inherited_from_parent": inherited, "decision": decision,
            "parent_config": parent}


def validate(run_id, case_id, task, records, driver, question_id):
    """Split acquisition, model claim and delivered result; never merge them.

    O6: acceptance now checks the model's WHOLE structured delivery, not only
    `conclusion`:
      * the asked field must be the one the model names;
      * the model's `condition_id` must be this run's served condition;
      * every `evidence_refs` entry must resolve to an evidence id that exists in
        this run and is currently valid (not stale, not excluded);
      * the stop basis must be one of the declared legal bases;
      * the program-computed obligations must be satisfied.
    Only then is `model_claim_accepted` true. A missing or wrong item rejects the
    model's completion instead of being back-filled from the validator's own
    verdict.
    """
    verdict = core.verdict_for_question(records, question_id, None, task)
    layers = core.layers(records, task, question_id=question_id)
    parsed = _parse_answer(driver.get("answer")) or {}
    claim = {"conclusion": parsed.get("conclusion"),
             "stop_reason": parsed.get("stop_reason")
             or parsed.get("stop_reason_code"),
             "stations_used": parsed.get("stations_used"),
             "field": parsed.get("field"),
             "condition_id": parsed.get("condition_id"),
             "scope_candidate_ids": parsed.get("scope_candidate_ids"),
             "evidence_refs": parsed.get("evidence_refs"),
             "stop_reason_code": parsed.get("stop_reason_code"),
             "limitations": parsed.get("limitations")}
    answered = (verdict.get("answered") or {})
    label = verdict["verdict"]
    agrees = bool(claim["conclusion"]) and claim["conclusion"] == label
    try:
        state = json.loads((runlog.run_dir(run_id) / "mcp_state.json").read_text())
    except (OSError, ValueError):
        # A unit-level call may have no run directory. The acceptance checks then
        # run against the records the caller supplied, and still require the
        # structured delivery: an absent state is not a licence to skip them.
        state = {"run_id": run_id, "task_id": task.get("task_id"),
                 "acquired": list(records)}
    requested = (state.get("requested_conditions")
                 or task.get("requested_conditions")
                 or EC.from_task(task, question_id=question_id))
    audit = finish_audit.audit_finish(state, task=task, verdict=verdict,
                                      requested=requested, model_claim=claim)
    # The acceptance checklist, itemised so a rejection says exactly what failed.
    outcome = audit["outcome"]
    determined = outcome == "determined_answer"
    legal_unknown = outcome == "legal_unknown"
    scope_report = _scope_report(claim["scope_candidate_ids"], state, requested)
    evidence_report = _evidence_refs(claim["evidence_refs"], state, requested)
    declared_stop = claim["stop_reason_code"]
    checks = {
        "conclusion_matches_validator": agrees,
        # A correct legal unknown is a valid completion of an answer that the
        # evidence cannot determine; only strategy_insufficient / execution_failed
        # outcomes are unacceptable.
        "outcome_acceptable": bool(determined or legal_unknown),
        "field_matches": claim["field"] == requested.get("required_field"),
        "condition_matches": claim["condition_id"] == requested.get("condition_id"),
        # The model must declare a legal stop code. The program cross-checks it
        # against the audit's fact-derived basis; a missing code is NOT back-filled
        # from the validator's own outcome, and the model's own word never makes an
        # otherwise unfounded stop legal.
        "stop_basis_legal": bool(
            declared_stop
            and declared_stop in finish_audit.LEGAL_STOP_BASES
            and finish_audit.stop_code_consistent(
                declared_stop, audit.get("stop_bases"), determined)),
        # An explicitly declared EMPTY scope is a declaration, but only where the
        # run really has nothing participating; a fabricated or excluded id fails.
        "scope_valid": bool(scope_report["valid"]),
        "evidence_refs_resolve": evidence_report["all_valid"],
        "asked_field_settled": bool(answered.get("answered")),
    }
    model_claim_accepted = (all(v is True for k, v in checks.items()
                                if k != "asked_field_settled")
                            and audit["can_finish"])
    rejection = None
    if not model_claim_accepted:
        failed = [k for k, v in checks.items() if v is not True]
        if not claim["conclusion"]:
            rejection = "the session produced no parsable conclusion"
        elif not checks["outcome_acceptable"]:
            rejection = (f"the run's outcome is {outcome}: the evidence neither "
                         "settles the asked field nor supports a legal unknown")
        elif not answered.get("answered") and legal_unknown:
            rejection = (f"the asked field `{answered.get('field')}` was not settled by "
                         f"the acquired evidence (value: {answered.get('value')})")
        elif not agrees:
            rejection = (f"the model stated `{claim['conclusion']}` but the evidence "
                         f"supports `{label}` ({verdict['reason']})")
        else:
            rejection = ("the structured delivery was rejected: "
                         + ", ".join(failed)
                         + (f"; evidence_refs: {evidence_report['detail']}"
                            if evidence_report["detail"] else ""))
    tool_activity = _tool_activity(run_id)
    # `fallback_used` is true whenever the deterministic validator's own verdict is
    # what gets delivered instead of the model's accepted delivery. A model failure
    # must never be presented as an agent success.
    fallback_used = not model_claim_accepted
    return {
        "run_id": run_id, "task_id": task["task_id"],
        "question": task["user_question"], "question_id": question_id,
        "requested_conditions": requested,
        "finish_audit": audit,
        "acceptance_checks": checks,
        "evidence_ref_report": evidence_report,
        "scope_report": scope_report,
        "acquisition": {
            "stations_acquired": len(records),
            "stations_analysed": layers["acquisition_status"]["analysed"],
            "sufficient_for_asked_field": bool(answered.get("answered")),
            "next_action": layers["next_action"],
            "tool_errors": tool_activity["tool_errors"],
            "tools_with_errors": tool_activity["tools_with_errors"],
            "interface_failure": tool_activity["tool_errors"] > 0,
            "tool_attempts": tool_activity["attempts"],
            # Event-order recovery per tool+operated object (review-4 A4).
            # `_interface_outcome` reads this; cumulative counts alone can no
            # longer claim "其后恢复".
            "recovery": tool_activity.get("recovery"),
        },
        "model_claim": claim,
        "validator_verdict": {"verdict": label, "reason": verdict["reason"],
                              "statement": verdict["statement"],
                              # Carried through so the interface can show the
                              # Chinese boundary line for agent runs too, not only
                              # for the deterministic ones (review 3.3).
                              "statement_zh": verdict.get("statement_zh"),
                              "terminated_by": verdict.get("terminated_by"),
                              # The counting basis itself, so a safety check does not
                              # have to guess whether a certainty claim had evidence.
                              "coincidence": verdict.get("coincidence"),
                              "eligible_stations": verdict.get("eligible_stations"),
                              "integrity_flag": verdict.get("integrity_flag"),
                              "answered": answered.get("answered"),
                              "field_answered": verdict.get("field_answered"),
                              "field": answered.get("field")},
        "agreement": agrees,
        "delivered": {
            # Four separate things, never merged (evaluation P1-1):
            #   verdict         the answer label for the question that was asked
            #   field/field_value  the asked field and its value
            #   task_completed  whether that field was actually settled
            #   model_claim_accepted  whether the model's wording was accepted
            "field": verdict.get("field_answered") or answered.get("field") or "verdict",
            "value": (answered.get("value") if answered.get("field")
                      else label),
            "field_value": answered.get("value"),
            "answer_kind": answered.get("answer_kind"),
            "scope": answered.get("scope"),
            "verdict": label,
            "task_completed": bool(answered.get("answered")),
            "condition_id": requested.get("condition_id"),
            "scope_candidate_ids": (claim["scope_candidate_ids"]
                                    or [r["candidate_id"] for r in records
                                        if not r.get("excluded")]),
            "evidence_refs": (claim["evidence_refs"]
                              if evidence_report["all_valid"] else []),
            "declared_evidence_refs": claim["evidence_refs"],
            "stop_reason_code": (claim["stop_reason_code"]
                                 or audit.get("stop_basis")),
            "limitations": claim["limitations"],
            "statement": verdict["statement"],
            "statement_zh": verdict.get("statement_zh"),
            "model_claim_accepted": model_claim_accepted,
            "origin": "agent_validated" if model_claim_accepted else "fallback",
            "audit_outcome": outcome,
            "fallback_used": fallback_used,
            "rejection_reason": rejection,
        },
    }


def _evidence_refs(refs, state, requested):
    """Resolve each declared evidence id against this run's valid evidence.

    An id that does not exist, belongs to a stale item, was never actually
    computed, points at an excluded observation, or names an object outside the
    current scope is a failure — the model may not cite evidence it did not obtain
    or that the current conditions invalidated.
    """
    validity = evidence_state.validity(state, requested)
    valid = set(validity["valid_ids"])
    stale = set(validity["stale_ids"])
    records = state.get("acquired") or []
    by_id = {r.get("candidate_id"): r for r in records}
    participating = set(EC.records_by_scope(requested, records))
    active = {r.get("candidate_id") for r in records if not r.get("excluded")}

    def allowed(candidate_id):
        return (candidate_id in by_id and candidate_id in participating
                and candidate_id in active)

    # Registered evidence is citable only when every object it participated in is
    # still in scope and not excluded.
    citable = set()
    for evidence_id in valid:
        entry = (state.get("derived_evidence_checks") or {}).get(evidence_id) or {}
        participants = entry.get("participating_candidate_ids") or []
        if not participants or all(allowed(p) for p in participants):
            citable.add(evidence_id)

    # Kind-specific truth checks, because a registered id can outlive the fact it
    # describes (e.g. an onset ref kept while the record was cleared).
    for candidate_id, record in by_id.items():
        onset_id = f"onset:{candidate_id}"
        # A citable onset is one this run really computed: an explicit
        # `onset_computed: true`, or a recorded onset value on a legacy record.
        # An explicit `onset_computed: false` is never overridden by a stale value.
        computed = (record.get("onset_computed") is True
                    or (record.get("onset_utc") is not None
                        and record.get("onset_computed") is not False))
        if computed and allowed(candidate_id):
            citable.add(onset_id)
        else:
            citable.discard(onset_id)
        quality_id = f"quality:{candidate_id}"
        if allowed(candidate_id) and evidence_state.quality_available(record):
            citable.add(quality_id)
        else:
            citable.discard(quality_id)
        applicability_id = f"applicability:{candidate_id}"
        if (allowed(candidate_id)
                and evidence_state.applicability_judged_under(record, requested)):
            citable.add(applicability_id)
        else:
            citable.discard(applicability_id)
    for candidate_id, entry in (state.get("stability_checks") or {}).items():
        stability_id = f"stability:{candidate_id}"
        current = bool(entry and entry.get("freshness") != "stale"
                       and entry.get("condition_ref") == requested.get("condition_id")
                       and allowed(candidate_id))
        if current:
            citable.add(stability_id)
        else:
            citable.discard(stability_id)

    plausible = ({f"onset:{c}" for c in by_id}
                 | {f"quality:{c}" for c in by_id}
                 | {f"applicability:{c}" for c in by_id}
                 | {f"stability:{c}" for c in (state.get("stability_checks") or {})}
                 | {f"stability:{c}" for c in by_id})
    known = citable | stale | plausible
    if not refs:
        if not active & participating:
            # Nothing participates, so there is no evidence to cite. The empty
            # scope is the finding, not a missing reference.
            return {"all_valid": True, "detail": "empty scope: no evidence to cite",
                    "resolved": [], "unknown": [], "stale": [], "unverified": []}
        return {"all_valid": False, "detail": "no evidence_refs declared",
                "resolved": [], "unknown": [], "stale": [], "unverified": []}
    unknown = [r for r in refs if r not in known]
    stale_refs = [r for r in refs if r in stale]
    unverified = [r for r in refs if r in known and r not in citable
                  and r not in stale]
    resolved = [r for r in refs if r in citable]
    ok = not unknown and not stale_refs and not unverified
    detail = None
    if not ok:
        bits = []
        if unknown:
            bits.append(f"unknown={unknown}")
        if stale_refs:
            bits.append(f"stale={stale_refs}")
        if unverified:
            bits.append(f"unverified={unverified}")
        detail = "; ".join(bits)
    return {"all_valid": ok, "resolved": resolved, "unknown": unknown,
            "stale": stale_refs, "unverified": unverified, "detail": detail}


def _scope_report(declared, state, requested):
    """Whether the declared scope is a legal description of THIS run's evidence.

    A scope is not a free-text label: it must be a list of candidate ids that all
    exist in the run, are inside the requested scope and are still participating.
    An explicitly empty list is a legal declaration only when nothing participates.
    """
    records = state.get("acquired") or []
    known = {r.get("candidate_id") for r in records}
    participating = set(EC.records_by_scope(requested, records))
    active = {r.get("candidate_id") for r in records if not r.get("excluded")}
    permitted = sorted(participating & active)
    remaining, remaining_detail = EC.remaining_scope_candidates(requested, state)
    report = {"declared": declared, "permitted": permitted,
              "participating": sorted(participating),
              "excluded": sorted({r.get("candidate_id") for r in records
                                  if r.get("excluded")}),
              "remaining_acquirable": sorted(remaining),
              "remaining_detail": remaining_detail}
    if declared is None:
        report.update({"valid": False, "reason": "scope_not_declared"})
        return report
    if (not isinstance(declared, list)
            or not all(isinstance(x, str) and x for x in declared)):
        report.update({"valid": False, "reason": "scope_wrong_type"})
        return report
    unknown = sorted(set(declared) - known)
    if unknown:
        report.update({"valid": False, "reason": "scope_unknown_candidate_ids",
                       "unknown": unknown})
        return report
    excluded = sorted(set(declared) - active)
    outside = sorted(set(declared) - participating)
    if not declared:
        # An empty declaration is legal only when nothing participates AND no
        # in-scope candidate is still acquirable. Otherwise the task is unfinished,
        # not an exhausted scope.
        valid = not permitted and not remaining
        if valid:
            reason = None
        elif remaining:
            reason = "empty_scope_but_actions_remain"
        else:
            reason = "empty_scope_but_evidence_participates"
        report.update({"valid": valid, "reason": reason})
        return report
    if excluded:
        report.update({"valid": False, "reason": "scope_names_excluded_objects",
                       "excluded_named": excluded})
        return report
    if outside:
        report.update({"valid": False, "reason": "scope_outside_requested_scope",
                       "outside": outside})
        return report
    report.update({"valid": True, "reason": None})
    return report


def _interface_outcome(validation, failure_kind):
    """Is the run failed because the interface blocked it, or only under-evidenced?

    The third review's P0-3 probe: a tool raised `RuntimeError`, the driver exited
    normally, and the run still ended as `succeeded` with `tool_errors=1`. A run
    whose acquisition tool could not be used and whose tool was never successfully
    used afterwards did not fail scientifically — it failed operationally, and the
    user must see that.

    Recovery is judged from the event ORDER per tool and per operated object
    (review-4 A4), never from cumulative counts: {start:2, ok:1, error:1} can be
    error→ok (recovered) or ok→error (not recovered) and the code must be able
    to tell the two apart. When only aggregate counts are available the safe
    reading is "recovery unproven", not "其后恢复".

    The four terminal shapes (review-4 guide §2.2):
      1. key acquisition error, no valid supplement, clean driver exit
                                                   -> failed, interface reason visible
      2. error later followed by a valid ok on the same tool+target, field
         settled by evidence                       -> succeeded, history kept
      3. ok followed by a later key error, task still unsettled
                                                   -> failed; never "其后恢复"
      4. an unrecovered tool error but independently sufficient evidence
                                                   -> succeeded; completion and the
                                                      exception reported separately

    Returns (status, failure_kind, detail).
    """
    if failure_kind is not None:
        return "failed", failure_kind, None
    acquisition = validation.get("acquisition") or {}
    errors = acquisition.get("tool_errors") or 0
    if not errors:
        return runlog.SUCCEEDED, None, None
    completed = bool((validation.get("delivered") or {}).get("task_completed"))
    recovery = acquisition.get("recovery") or {}
    if recovery.get("order_known"):
        unrecovered = list(recovery.get("unrecovered") or [])
        proven = list(recovery.get("recovered") or [])
        order_note = None
    else:
        # Legacy aggregate counts cannot prove an order: every errored tool is
        # treated as unrecovered rather than claimed recovered.
        attempts = acquisition.get("tool_attempts") or {}
        unrecovered = sorted(tool for tool, phases in attempts.items()
                             if (phases.get("error") or 0) > 0)
        proven = []
        order_note = "仅有累计计数，无法分辨异常与成功的先后顺序"
    if completed:
        bits = [f"{errors} 次工具异常，但所问字段已由取得的证据回答"]
        if unrecovered:
            bits.append("未恢复异常：" + "、".join(sorted(set(unrecovered))))
        if proven:
            bits.append("其后同工具同对象已成功：" + "、".join(sorted(set(proven))))
        return runlog.SUCCEEDED, None, "；".join(bits)
    if not unrecovered:
        return (runlog.SUCCEEDED, None,
                f"{errors} 次工具异常，事件顺序显示同工具同对象其后已成功（已恢复）；"
                f"异常历史保留在所跑 run 的工具记录里；"
                f"所问字段本轮未定的原因见交付层")
    tools = "、".join(sorted(set(unrecovered)))
    detail = (f"关键工具异常且未恢复：{tools}；任务未完成。"
              f"这是接口失败，不是证据不足（tool_errors={errors}）")
    if order_note:
        detail += f"；{order_note}"
    return "failed", "tool_failure", detail


def _stop_owned_daemon(run_id, *, timeout=90) -> dict:
    """Stop only the AGH daemon this run owns, from its own recorded identity.

    The Python watchdog never sweeps process names or ports: `runtime-owner.json`
    names one AGH_HOME and one owner id, and the official CLI stops that daemon with
    that home. A concurrent session has a different home and is untouched.
    """
    folder = runlog.run_dir(run_id)
    path = folder / "runtime-owner.json"
    if not path.exists():
        return {"stopped": False, "reason": "no_runtime_owner_record"}
    try:
        owner = json.loads(path.read_text())
    except ValueError:
        return {"stopped": False, "reason": "runtime_owner_unreadable"}
    recorded_run = owner.get("run_id")
    if recorded_run and recorded_run != run_id:
        # Never stop a daemon that belongs to a different run: a stale or copied
        # ownership record must yield cleanup_needed, not a stop of someone else's
        # home.
        return {"stopped": False, "reason": "runtime_owner_run_mismatch",
                "run_id": run_id, "recorded_run_id": recorded_run}
    home = owner.get("agh_home")
    if not home or not Path(home).exists():
        return {"stopped": False, "reason": "owned_home_missing",
                "owner": owner}
    cli = ROOT / "vendor/agnes-harness/packages/cli/dist/local/agnes.mjs"
    if not cli.exists():
        return {"stopped": False, "reason": "cli_missing", "owner": owner}
    env = {**os.environ, "AGH_HOME": str(home), "AGNES_PROFILE": "local-dev",
           "TMPDIR": "/tmp"}
    try:
        proc = subprocess.run(["node", str(cli), "daemon", "stop"], cwd=ROOT,
                              capture_output=True, text=True, timeout=timeout,
                              env=env)
        stopped = proc.returncode == 0
        detail = (proc.stdout or proc.stderr or "").strip()[-200:]
    except subprocess.TimeoutExpired:
        stopped, detail = False, "daemon stop timed out"
    except Exception as exc:  # noqa: BLE001
        stopped, detail = False, f"{type(exc).__name__}: {exc}"[:200]
    result = {"run_id": run_id, "stopped": stopped, "detail": detail,
              "agh_home": str(home), "owner_id": owner.get("owner_id"),
              "scope": "this run's recorded AGH_HOME only"}
    try:
        (folder / "watchdog_stop.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2))
    except OSError:
        pass
    return result


def _execute(case_id, prepared, question_id, question, driver_script="agent_run.mjs",
             profile=None, analysis_level=None, inherited_from=None,
             decision=None):
    """Drive one already-prepared run to a recorded outcome.

    `driver` selects the decision strategy. Every arm runs through this same
    function so the validator, the cost accounting, the failure handling and the
    outcome record cannot differ between them — only the strategy does.
    """
    run_id, task_id = prepared["run_id"], prepared["task_id"]
    frozen = json.loads((runlog.run_dir(run_id) / "task.json").read_text())
    print(f"  run_id={run_id} task_id={task_id}", flush=True)

    tsx = ROOT / "vendor/agnes-harness/node_modules/.bin/tsx"
    # AGENT_MODEL selects the model version so a comparison can be tied to one
    # version (G2), and so a deliberate invalid model can exercise the failure path.
    model = os.environ.get("AGENT_MODEL", "deepseek-flash")
    level = (analysis_level or prepared.get("analysis_level") or "full")
    # The execution configuration is a recorded fact of THIS run, written before the
    # driver starts so it survives a driver crash.
    record_execution_config(run_id, driver_script=driver_script, profile=profile,
                            analysis_level=level, model=model,
                            inherited_from=inherited_from, decision=decision)
    env = {**os.environ, "AGENT_VARIANT": VARIANT,
           "RUNLOG_DIR": str(runlog.RUNLOG), "EQ_CACHE_DIR": str(core.CACHE)}
    cmd = [str(tsx), f"main/{driver_script}", run_id]
    if driver_script == "agent_run_optimization.mjs":
        cmd.append(profile or PRODUCT_PROFILE)
    cmd.append(model)
    failure_kind = None
    watchdog_stop = None
    driver_timeout = float(os.environ.get("EQ_DRIVER_TIMEOUT_S") or 900)
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              timeout=driver_timeout, env=env)
        rc = proc.returncode
        print(f"  driver rc={rc} {proc.stdout.strip()[:200]}", flush=True)
        if proc.stderr.strip():
            print(f"  stderr: {proc.stderr.strip()[:200]}", flush=True)
        if rc != 0:
            failure_kind = EXIT_TO_FAILURE.get(rc, "subprocess_exit")
            runlog.record_failure(run_id, failure_kind,
                                  proc.stderr.strip()[-300:] or f"driver exited {rc}")
    except subprocess.TimeoutExpired:
        failure_kind = "timeout"
        runlog.record_failure(run_id, "timeout", "driver exceeded its outer timeout")
        print("  driver timed out", flush=True)
        # The outer watchdog stops only the daemon this run actually owns, using
        # the identity the driver recorded for it. No process-name sweep, so a
        # concurrent session is never touched.
        watchdog_stop = _stop_owned_daemon(run_id)

    # The driver record must be this run's, not a leftover from another run.
    # The AGH driver writes into main/out; the one-shot and controlled drivers
    # write their response into the run directory itself. Both are checked
    # against this run id.
    run_folder = runlog.run_dir(run_id)
    # The optimisation driver writes its record into the run's own directory (never
    # a shared main/out), so the record path is derived from the profile.
    if driver_script == "agent_run_optimization.mjs" and profile:
        exec_path = run_folder / f"agent_execution_{profile}_{run_id}.json"
    else:
        exec_path = OUT / f"agent_execution_{VARIANT}_{run_id}.json"
    local_record = run_folder / "model_response.json"
    driver = {}
    if driver_script in ("one_shot.mjs", "controlled_driver.mjs") and local_record.exists():
        driver = json.loads(local_record.read_text())
    elif exec_path.exists():
        driver = json.loads(exec_path.read_text())
        if driver.get("RUN") != run_id:
            failure_kind = failure_kind or "record_mismatch"
            runlog.record_failure(run_id, "record_mismatch",
                                  f"driver record belongs to {driver.get('RUN')}")
    elif failure_kind is None:
        failure_kind = "no_execution_record"
        runlog.record_failure(run_id, "no_execution_record",
                              "driver exited 0 but wrote no record for this run")
    if driver.get("failure_kind") and failure_kind is None:
        failure_kind = driver["failure_kind"]
    # Resource cleanup is part of the run's outcome, not a footnote. A driver that
    # could not confirm its daemon stopped (or timed out cleaning up) must not be
    # recorded as a clean success, and the original error is preserved alongside it.
    cleanup = driver.get("cleanup") if isinstance(driver.get("cleanup"), dict) else {}
    cleanup_ok = driver.get("cleanup_ok")
    if cleanup_ok is None:
        cleanup_ok = cleanup.get("cleanup_ok")
    cleanup_needed = driver.get("cleanup_needed")
    if cleanup_needed is None:
        cleanup_needed = cleanup.get("cleanup_needed")
    if failure_kind is None and cleanup_ok is False:
        failure_kind = ("cleanup_timeout" if cleanup.get("timed_out")
                        else "cleanup_failed")
    if failure_kind in ("cleanup_failed", "cleanup_timeout") and not cleanup:
        cleanup = {"cleanup_errors": [driver.get("fatal") or "cleanup failed"],
                   "cleanup_ok": False, "cleanup_needed": True}
    runlog.write_once(run_folder / "resource_cleanup.json", {
        "schema": "run-resource-cleanup/v1", "run_id": run_id,
        "driver": driver_script, "profile": profile,
        "driver_status": driver.get("status"),
        "stop_confirmed": cleanup.get("stop_confirmed"),
        "cleanup_ok": cleanup_ok, "cleanup_needed": cleanup_needed,
        "timed_out": cleanup.get("timed_out"),
        "owned_home": driver.get("owned_home") or cleanup.get("owned_home"),
        "root_removed": driver.get("root_removed"),
        "errors": cleanup.get("cleanup_errors"),
        "log": cleanup.get("log"),
        "watchdog_stop": watchdog_stop})
    if watchdog_stop is not None and not watchdog_stop.get("stopped"):
        cleanup_needed = True

    state = json.loads((runlog.run_dir(run_id) / "mcp_state.json").read_text())
    records = state.get("acquired", [])
    n_eligible = ((state.get("discovery") or {}).get("n_eligible")
                  or len((state.get("discovery") or {}).get("eligible") or []))
    validation = validate(run_id, case_id, frozen, records, driver, question_id)
    runlog.write_once(runlog.run_dir(run_id) / "validation.json", validation)
    # The one-shot driver writes its own response into the run directory; that file
    # is already the authoritative record and write_once refuses to replace it.
    if driver and not local_record.exists():
        runlog.write_once(run_folder / "model_response.json", driver)

    model_cost = runlog.cost_for_run(run_id)
    runlog.write_cost(run_id, model_cost,
                      http_attempts=state.get("http_attempts", 0),
                      cache_hits=state.get("cache_hits", 0),
                      logical_acquisitions=state.get("logical_acquisitions", 0),
                      bytes_total=state.get("bytes", 0),
                      elapsed_ms=driver.get("elapsed_ms"))
    cost = json.loads((runlog.run_dir(run_id) / "cost.json").read_text())

    status, failure_kind, failure_detail = _interface_outcome(validation, failure_kind)
    runlog.outcome(run_id, status, failure_kind=failure_kind,
                   failure_detail=failure_detail or (driver.get("fatal") if driver else None),
                   stations_acquired=len(records),
                   model_cost_known=cost["model_cost_known"])

    acceptance = frozen.get("acceptance") or {}
    if driver_script == "one_shot.mjs":
        arm_label = "one_shot_model"
    elif driver_script == PRODUCT_DRIVER:
        arm_label = f"adaptive_agent_{(profile or PRODUCT_PROFILE).lower()}"
    else:
        arm_label = f"adaptive_agent_{VARIANT}"
    return {
        "run_id": run_id,
        # The row must state its own parent, so a feedback chain is readable from the
        # result record and not only from the run directory (review 3 §2.2).
        "parent_run_id": row_parent(run_folder),
        "case_id": case_id,
        "arm": arm_label,
        "coincidence": validation["validator_verdict"].get("coincidence"),
        "eligible_stations": validation["validator_verdict"].get("eligible_stations"),
        "reason": validation["validator_verdict"].get("reason"),
        "answered": validation["validator_verdict"].get("answered"),
        "terminated_by": validation["validator_verdict"].get("terminated_by"),
        "field_answered": validation["validator_verdict"].get("field_answered"),
        "model": model,
        "task_id": task_id,
        "question": frozen["user_question"],
        "question_id": question_id,
        "kind": acceptance.get("kind"),
        "split": acceptance.get("split"),
        "status": status,
        "failure_kind": failure_kind,
        "failure_detail": failure_detail,
        # Whether the run was blocked by the interface rather than by evidence.
        "interface_blocked": bool(status != runlog.SUCCEEDED
                                  and failure_kind == "tool_failure"),
        # The resource terminal state travels with the result so the page does not
        # have to read the driver record to see whether cleanup really finished.
        "cleanup": {"ok": cleanup_ok, "needed": cleanup_needed,
                    "stop_confirmed": cleanup.get("stop_confirmed"),
                    "timed_out": cleanup.get("timed_out"),
                    "owned_home": (driver.get("owned_home")
                                   or cleanup.get("owned_home")),
                    "watchdog_stop": watchdog_stop},
        # three separate things, never merged
        "acquisition": validation["acquisition"],
        "model_claim": validation["model_claim"],
        "delivered": validation["delivered"],
        "agreement": validation["agreement"],
        "stations_acquired": len(records),
        "n_eligible": n_eligible,
        "requests_to_verdict": state.get("requests", 0),
        "http_attempts": cost["http_attempts"],
        "cache_hits": cost["cache_hits"],
        "logical_acquisitions": cost["logical_acquisitions"],
        "model_cost_known": cost["model_cost_known"],
        "model_cost_cny": cost["model_cost_upper_cny"],
        "stations": [{"station": r["station"], "status": r.get("status"),
                      "onset_utc": r.get("onset_utc"),
                      "channel_integrity_flags": r.get("channel_integrity_flags"),
                      "band_support": r.get("band_support")} for r in records],
    }


def run_one(case_id, question=None, method_goal=None,
            operational_standard=None):
    """One fresh adaptive session on a demo case.

    The validator is bound to the question the user actually asked; using the
    case's preset question would answer something the user did not ask.

    `operational_standard` is the公开判据（review-5 §2.2：三站/三秒必须
    给默认 open 的 A，不能当隐藏参考答案）。

    The ordinary product first round now runs the SAME frozen driver, prompt
    template and output contract as the experiment arm, with the thin
    `quality_only` preparation: an agent that never computes a diagnostic itself
    cannot pass on work the preparation did for it.
    """
    case = core.case_by_id(case_id)
    prepared = prepare_agent.prepare(case_id, question=question,
                                     arm=f"adaptive_agent_{PRODUCT_PROFILE.lower()}",
                                     method_goal=method_goal,
                                     operational_standard=operational_standard,
                                     analysis_level=PRODUCT_ANALYSIS_LEVEL)
    question_id = (core.classify_question(question) if question
                   else case["question_id"])
    return _execute(case_id, prepared, question_id, question,
                    driver_script=PRODUCT_DRIVER, profile=PRODUCT_PROFILE)


def derived_evidence(folder, records, requested=None) -> dict:
    """本地派生证据：coincidence + 逐站完整诊断（M 臂的公平材料）。

    review-5 §2.2 要求 M 获得“完整专业证据，包括逐站簇、detectors、
    alignment 和限制”。这里与 A 通过工具能取到的本地计算结果同源、同量：
    一次实现，两个入口（M 首轮与 M 反馈轮）共用，避免再次分叉。

    O1：诊断网格来自本轮的实际条件。旧版固定调用 `stability(path)`，所以“按
    3–12 Hz 重算”的反馈对 M 无效——模型的 derived_evidence 又回到默认整网格。
    """
    import diagnostics as _diag  # noqa: PLC0415
    requested = requested or {}
    bands = requested.get("diagnostic_bands_hz")
    derived = {"coincidence_window_s": core.COINCIDENCE_WINDOW_S,
               "coincidence": core.coincidence(records),
               "condition_ref": requested.get("condition_id"),
               "diagnostic_bands_hz": ([list(b) for b in bands] if bands else None),
               "onsets": [{"station": r.get("station"),
                           "onset_utc": r.get("onset_utc"),
                           "distance_km": r.get("distance_km")}
                          for r in records if r.get("onset_utc") is not None],
               "stability": {}}
    for rec in records:
        if rec.get("status") != "analysed":
            continue
        path = next((folder / kind / rec["candidate_id"] / "waveform.mseed"
                     for kind in ("derived", "raw")
                     if (folder / kind / rec["candidate_id"]
                         / "waveform.mseed").exists()), None)
        if path is None:
            continue
        res = _diag.stability(path, bands_hz=bands,
                              target_band_hz=requested.get("target_band_hz"))
        derived["stability"][rec["station"]] = {
            "stability": res["stability"], "spread_s": res["spread_s"],
            "n_valid": res["n_valid"], "grid_size": res["grid_size"],
            "n_configurations": res["n_configurations"],
            "bands_evaluated": res["bands_evaluated"],
            "full_grid": res["full_grid"],
            "target_band_hz": res.get("target_band_hz"),
            "condition_ref": requested.get("condition_id"),
            "effective_parameters": {"bands_hz": res["bands_evaluated"],
                                     "thresholds": _diag.GRID["thresholds"]},
            "freshness": "valid",
            "clusters": res["clusters"],
            "detectors": res["detectors"],
            "alignment": res["alignment"],
            "onsets_min_s": res["onsets_min_s"],
            "onsets_max_s": res["onsets_max_s"],
            "onsets_median_s": res["onsets_median_s"],
            "mad_s": res["mad_s"],
            "reason": res["reason"],
            "statement": res["statement"], "next_step": res["next_step"]}
    return derived


def _one_shot_acquire(case, frozen, run_id, ceiling):
    """M 臂的采集：确定性代码一次取满许可证据，模型不参与选择。"""
    folder = runlog.run_dir(run_id)
    discovery = json.loads((folder / "discovery.json").read_text())
    state = {"run_id": run_id, "task_id": frozen["task_id"], "task": frozen,
             "records": [], "requests": 0, "http_attempts": 0,
             "logical_acquisitions": 0, "cache_hits": 0, "bytes": 0,
             "inject_first": bool(case.get("injected")),
             "inject_station": case.get("inject_station"), "on_step": None}
    limit = ceiling if ceiling is not None else core.MAX_STATIONS
    for candidate in discovery["eligible"][:limit]:
        state["records"].append(core.acquire(case, candidate, state))
    # "完整许可证据"：凡是本地可算、不需要新取数的，M 也必须拿到。
    # 否则等于故意扣留 A 能通过工具取得的信息，再把结论差异说成能力差异。
    live = json.loads((folder / "mcp_state.json").read_text())
    derived = derived_evidence(folder, state["records"],
                               requested=live.get("requested_conditions"))

    live.update({"derived_evidence": derived,
                 "acquired": state["records"], "requests": state["requests"],
                 "http_attempts": state["http_attempts"],
                 "logical_acquisitions": state["logical_acquisitions"],
                 "cache_hits": state["cache_hits"], "bytes": state["bytes"],
                 "acquired_up_front": True, "ceiling": limit})
    (folder / "mcp_state.json").write_text(
        json.dumps(live, ensure_ascii=False, indent=2, default=str))
    runlog.write_once(folder / "acquisition_plan.json",
                      {"arm": "one_shot_model", "ceiling": limit,
                       "acquired": [r["candidate_id"] for r in state["records"]],
                       "decided_by": "deterministic, before any model call"})
    return limit


def run_one_shot(case_id, question=None, ceiling=None, method_goal=None,
                 operational_standard=None):
    """M 臂：同一模型，一次取得完整许可证据，一次调用（指南 5.3）。

    采集在模型介入前由确定性代码完成，观测上限与 A 臂一致；模型只看到一次，
    也只回答一次。验证、费用与失败处理与 A 臂共用 _execute，因此差异只在决策方式。
    公开操作标准与 A 组同文（指南 §2.2）。
    """
    case = core.case_by_id(case_id)
    prepared = prepare_agent.prepare(case_id, question=question,
                                     arm="one_shot_model",
                                     method_goal=method_goal,
                                     operational_standard=operational_standard)
    frozen = json.loads((runlog.run_dir(prepared["run_id"]) / "task.json").read_text())
    _one_shot_acquire(case, frozen, prepared["run_id"], ceiling)
    question_id = (core.classify_question(question) if question
                   else case["question_id"])
    return _execute(case_id, prepared, question_id, question,
                    driver_script="one_shot.mjs")


def run_one_shot_task(frozen, question=None, ceiling=None):
    """M 臂在用户任务（含锁定任务）上的入口。"""
    case = taskmod.case_for_task(frozen)
    prepared = prepare_agent.prepare_task(frozen, arm="one_shot_model")
    _one_shot_acquire(case, frozen, prepared["run_id"], ceiling)
    asked = question or frozen["user_question"]
    return _execute(case["case_id"], prepared, core.classify_question(asked), asked,
                    driver_script="one_shot.mjs")


def run_user_task(frozen, question=None):
    """A real AGH session on a task the user typed into the app.

    The task arrives already frozen by main/task_input.py + main/task.py, so the
    window and region the model is given are the user's, not a preset event's.

    This is the ordinary web entry, so it uses the product's own frozen driver and
    the `quality_only` preparation, exactly like `run_one`.
    """
    prepared = prepare_agent.prepare_task(
        frozen, arm=f"adaptive_agent_{PRODUCT_PROFILE.lower()}",
        analysis_level=PRODUCT_ANALYSIS_LEVEL)
    asked = question or frozen["user_question"]
    case_id = taskmod.case_for_task(frozen)["case_id"]
    return _execute(case_id, prepared, core.classify_question(asked), asked,
                    driver_script=PRODUCT_DRIVER, profile=PRODUCT_PROFILE)


def run_followup(parent_run_id, instruction, method_goal=None, change=None,
                profile=None, analysis_level=None, acquisition_allowed=None):
    """The user's feedback turn: same observations, a new request, a new run id.

    The parent run's record is never touched. This is the G1 "one real feedback
    adjustment" step: the model is told the earlier observations are already in
    hand, so it can answer without any new fetch.

    `method_goal`/`change` carry a structured feedback (C 批次 T1 的 goal 变更)
    into the child's frozen task, so the validator judges under the child's
    conditions — the same wiring the deterministic feedback path uses.

    The child inherits the parent's ACTUAL profile and analysis level from the
    parent's own execution record (never from its arm name); a legacy parent is
    upgraded to the product default and the child records that decision.
    """
    config = _followup_config(parent_run_id)
    profile = profile or config["profile"]
    analysis_level = analysis_level or config["analysis_level"]
    if change and acquisition_allowed is not None:
        # The structured permission travels INSIDE the normalised change, so the
        # child task, the child state and the prompt all carry the same value.
        change = {**change, "acquisition_allowed": bool(acquisition_allowed)}
    prepared = prepare_agent.prepare_followup(
        parent_run_id, instruction, arm=f"adaptive_agent_{profile.lower()}_followup",
        method_goal=method_goal, change=change, analysis_level=analysis_level)
    parent_task = json.loads((runlog.run_dir(parent_run_id) / "task.json").read_text())
    # A free-input parent has no preset case; `case_for_task` builds the same
    # synthetic case the first round used, so the follow-up works either way.
    case_id = (parent_task.get("demo_case_ref")
               or taskmod.case_for_task(parent_task)["case_id"])
    question = parent_task["user_question"]
    # The feedback round uses the SAME driver/policy as the first round, otherwise
    # the strategy silently reverts to the old inline prompt on the second turn.
    return _execute(case_id, prepared, core.classify_question(question), question,
                    driver_script=PRODUCT_DRIVER, profile=profile,
                    analysis_level=analysis_level,
                    inherited_from=config["inherited_from_parent"],
                    decision=config["decision"])


def main():
    core.fdsn.set_cache(core.CACHE)
    args = sys.argv[1:]
    if not args:
        raise SystemExit(
            "Usage: run_agent_arm.py <case_id> [question...]\n"
            "       run_agent_arm.py --followup <parent_run_id> <instruction...>")
    followup = args[0] == "--followup"
    if followup:
        if len(args) < 3:
            raise SystemExit("--followup needs a parent run id and an instruction")
        parent_run_id, instruction = args[1], " ".join(args[2:])
        label = f"{parent_run_id} (follow-up)"
        print(f"=== 反馈重算：{label} ===", flush=True)
        try:
            row = run_followup(parent_run_id, instruction)
        except Exception as exc:  # noqa: BLE001
            row = {"run_id": None, "parent_run_id": parent_run_id,
                   "arm": f"adaptive_agent_{VARIANT}_followup",
                   "status": "failed", "failure_kind": "internal_error",
                   "error": f"{type(exc).__name__}: {exc}"[:300]}
    else:
        case_id, question = args[0], (" ".join(args[1:]) or None)
        label = case_id
        print(f"=== {case_id} ===", flush=True)
        try:
            row = run_one(case_id, question)
        except Exception as exc:  # noqa: BLE001
            row = {"case_id": case_id, "arm": f"adaptive_agent_{VARIANT}",
                   "status": "failed", "failure_kind": "internal_error",
                   "error": f"{type(exc).__name__}: {exc}"[:300]}
    print(json.dumps({k: v for k, v in row.items() if k != "stations"},
                     ensure_ascii=False, indent=2), flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"agent_results_{VARIANT}.json"
    # Append-only by run id: a re-run adds a record, it never replaces one.
    runs = []
    if path.exists():
        runs = json.loads(path.read_text()).get("runs", [])
    runs.append(row)
    path.write_text(json.dumps(
        {"provider": "deepseek", "model": "deepseek-flash", "variant": VARIANT,
         "runs": runs}, ensure_ascii=False, indent=2, default=str))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()