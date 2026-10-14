"""Read-only end audit shared by `task_status(finish)` and the runner (§10).

`audit_finish` makes NO request, calls NO model and performs NONE of the checks the
agent has not performed. It answers one question: given only what this run has
actually done, may it finish, and as what kind of outcome?

Four terminal shapes are kept distinct, because the previous scorer merged them:

  determined_answer      the asked field is settled by valid, in-scope evidence
  legal_unknown          the field is not settled, but the scope and a legal
                         insufficiency basis are both stated
  strategy_insufficient  allowed actions remained and the run stopped without a
                         basis (no candidates / reaching the ceiling is NOT this)
  execution_failed       interface/budget/cleanup failure, not a science result

The audit is deliberately narrow: it never invents a missing delivered field, and
it never lets an absent `task_completed` become a success.
"""
from __future__ import annotations

import core
import execution_conditions as EC
import resource_limits

LEGAL_INSUFFICIENCY_BASES = ("unanswered_no_data", "coherence",
                            "public_criterion_not_met", "scope_exhausted",
                            "material_missing", "resource_limited")
# A DETERMINED answer stops because a public criterion is satisfied; an
# insufficiency basis is for the unknown outcome. Both are legal stop bases, and
# conflating them would reject every correct determined answer.
LEGAL_STOP_BASES = tuple(LEGAL_INSUFFICIENCY_BASES) + ("public_criterion_satisfied",)


def _supported_stop_bases(state, requested, actual, settled) -> set:
    """The stop bases the run's OWN facts actually support.

    The program decides this from real permissions, accumulated attempts, the
    candidate ceiling, the remaining permitted candidates and the participating
    set. A model's declared code is only ever cross-checked against this set — it
    can never enlarge it, because a stop code the facts do not support is exactly
    the "no legal basis" case the audit exists to catch.
    """
    if settled:
        # A settled field stops because a public criterion is met; a scoped
        # negative ("no anomaly in the checked scope") additionally rests on the
        # scope having actually been covered.
        return {"public_criterion_satisfied", "scope_exhausted"}
    records = state.get("acquired") or []
    participating = list(actual.get("participating_candidate_ids") or [])
    attempts = set(actual.get("attempted_candidate_ids_episode") or [])
    ceiling = resource_limits.unique_candidate_ceiling(
        requested.get("max_unique_candidates_episode"))
    # `scope_exhausted` is licensed ONLY when no in-scope candidate is still
    # acquirable. "Nothing was checked yet but the user's station is available" is
    # not an exhausted scope — it is an unfinished task.
    remaining_scope, remaining_detail = EC.remaining_scope_candidates(requested, state)
    supported = set()
    if len(attempts) >= ceiling:
        supported.add("resource_limited")
    if not remaining_scope:
        supported.add("scope_exhausted")
    participated_records = [r for r in records
                            if r.get("candidate_id") in set(participating)]
    if participated_records and all(r.get("status") not in ("analysed",)
                                    for r in participated_records):
        supported.add("unanswered_no_data")
    return supported


def _primary_basis(supported: set) -> str | None:
    for candidate in ("resource_limited", "scope_exhausted", "unanswered_no_data",
                      "coherence", "public_criterion_not_met", "material_missing"):
        if candidate in supported:
            return candidate
    return None


def stop_code_consistent(declared, supported, determined: bool) -> bool:
    """Is the model's declared stop code one the run's facts support?

    Returns False for a missing code: the validator's own outcome must never be
    copied into the model's delivery to make an incomplete claim pass.
    """
    if not declared or declared not in LEGAL_STOP_BASES:
        return False
    return declared in set(supported or ())


def _tool_events(state, tool_events=None):
    if tool_events is not None:
        return list(tool_events)
    return list(state.get("tool_events") or [])


def _event_counts(events):
    tools = {}
    for event in events:
        if event.get("phase"):
            counts = tools.setdefault(event.get("tool"), {"start": 0, "ok": 0,
                                                          "error": 0})
            counts[event["phase"]] = counts.get(event["phase"], 0) + 1
    return tools


def audit_finish(state, *, task=None, verdict=None, tool_events=None,
                 model_claim=None, obligations=None, requested=None) -> dict:
    """The finish decision and its reasons. Pure: state in, decision out."""
    import task as taskmod  # noqa: PLC0415

    task = task or taskmod.task_from_state_view(state)
    # The caller may already hold the authoritative condition object (validate
    # builds it from the explicit question id). Prefer it over a fresh
    # re-classification, which could name a different criterion.
    requested = (requested or state.get("requested_conditions")
                 or task.get("requested_conditions")
                 or taskmod.requested_conditions_for(task))
    records = state.get("acquired") or []
    active = core.active_records(records)
    events = _tool_events(state, tool_events)
    question_id = requested.get("question_id") or task.get("question_id") \
        or "Q_propagating"

    if verdict is None:
        verdict = core.verdict_for_question(active, question_id, None, task)
    answered = verdict.get("answered") or {}
    settled = bool(answered.get("answered"))
    delivered_field = answered.get("field") or verdict.get("field_answered")
    required_field = requested.get("required_field") or EC.QUESTION_REQUIRED_FIELD.get(
        question_id, "verdict")

    actual = EC.actual_from_state(state, requested=requested)
    match = EC.matches(requested, actual)
    obligation_list = obligations if obligations is not None \
        else EC.obligations(requested, state, tool_events=events)
    summary = EC.obligation_summary(obligation_list)

    counts = _event_counts(events)
    unique_attempts = actual.get("attempted_candidate_ids_episode") or []
    limits = resource_limits.limits()
    model_requests = state.get("runtime_metrics", {}).get("model_requests")
    model_known = model_requests is not None
    tool_calls = sum((c.get("start") or 0) for c in counts.values()) or None

    unmet = []
    if not match.get("all"):
        unmet.append({"kind": "conditions_not_executed", "detail": match})
    if summary["pending"]:
        unmet.append({"kind": "obligations_pending", "detail": summary,
                      "pending": [o for o in obligation_list
                                  if o["state"] == "pending"]})
    if required_field and delivered_field and delivered_field != required_field:
        unmet.append({"kind": "wrong_field", "delivered_field": delivered_field,
                      "required_field": required_field})
    if len(unique_attempts) > limits["max_unique_candidates_episode"]:
        unmet.append({"kind": "candidate_ceiling_exceeded",
                      "attempted": len(unique_attempts),
                      "ceiling": limits["max_unique_candidates_episode"]})
    if not requested.get("acquisition_allowed", True):
        # A REFUSED proposal issued no request, so it is compliance evidence, not a
        # violation. Only an acquisition that actually ran counts against the ban.
        refused = [e for e in events if e.get("tool") == "acquire_station"
                   and (e.get("result") or {}).get("status") == "refused"]
        new_acq = [e for e in events if e.get("tool") == "acquire_station"
                   and e.get("phase") == "ok"
                   and (e.get("result") or {}).get("status") not in
                   ("already_acquired", "tool_error", "refused", None)]
        audit_extra = {"refused_proposals": len(refused)}
        if new_acq:
            unmet.append({"kind": "forbidden_acquisition", "count": len(new_acq)})

    stop_basis = state.get("stop_basis")
    scope = requested.get("scope") or {}
    scope_kind = scope.get("scope_kind") or scope.get("kind")
    scope_stated = bool(actual.get("participating_candidate_ids")) or \
        scope_kind in ("observed_scope", "explicit_candidates")
    # The program derives the bases its own facts support, and only then judges.
    supported_bases = _supported_stop_bases(state, requested, actual, settled)
    if stop_basis in LEGAL_INSUFFICIENCY_BASES:
        supported_bases.add(stop_basis)
    derived_basis = stop_basis or ("public_criterion_satisfied" if settled
                                   else _primary_basis(supported_bases))
    model_declared = None
    if isinstance(model_claim, dict):
        model_declared = (model_claim.get("stop_reason_code")
                          or model_claim.get("stop_reason"))
    model_basis_supported = (None if model_declared is None
                             else stop_code_consistent(model_declared,
                                                       supported_bases, settled))
    if settled and not unmet:
        outcome = "determined_answer"
    elif not settled and unmet and any(u["kind"] in
                                       ("obligations_pending", "wrong_field",
                                        "conditions_not_executed") for u in unmet):
        outcome = "strategy_insufficient"
    elif not settled:
        if not scope_stated:
            unmet.append({"kind": "scope_not_stated"})
            outcome = "strategy_insufficient"
        elif supported_bases:
            outcome = "legal_unknown"
        else:
            unmet.append({"kind": "no_legal_stop_basis"})
            outcome = "strategy_insufficient"
    else:
        outcome = "strategy_insufficient"

    can_finish = outcome in ("determined_answer", "legal_unknown")
    return {
        "schema": "agent-finish-audit/v1",
        "can_finish": can_finish,
        "outcome": outcome,
        "required_field": required_field,
        "delivered_field": delivered_field,
        "field_settled": settled,
        "verdict": verdict.get("verdict"),
        "reason": verdict.get("reason"),
        "condition_id": requested.get("condition_id"),
        "conditions_match": match,
        "scope": scope,
        "scope_kind": scope_kind,
        "participating_candidate_ids": actual.get("participating_candidate_ids"),
        "excluded_candidate_ids": actual.get("excluded_candidate_ids"),
        "obligations": obligation_list,
        "obligation_summary": summary,
        "unmet": unmet,
        "stop_basis": derived_basis,
        "stop_bases": sorted(supported_bases),
        "model_declared_stop_basis": model_declared,
        "model_stop_basis_supported": model_basis_supported,
        "legal_stop_bases": list(LEGAL_STOP_BASES),
        "resources": {
            "unique_candidates_attempted": len(unique_attempts),
            "unique_candidates_ceiling": limits["max_unique_candidates_episode"],
            "model_requests": model_requests,
            "model_requests_known": model_known,
            "model_requests_limit": limits["max_requests_per_case"],
            "tool_calls_executed": tool_calls,
            "tool_calls_limit": limits["max_tools_per_case"],
        },
        "note": ("本审计只读取本轮实际状态；它不补做检查、不发请求、不调用模型，"
                 "也不会把缺失的完成标志补成成功。"),
    }