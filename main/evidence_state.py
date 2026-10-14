"""Derived-evidence records with an explicit condition reference and freshness (O1).

The book §4.2 requires every derived item to carry at least:

    evidence_id, source_sha256, condition_ref, effective_parameters,
    participating_candidate_ids, freshness, check_status, tool_event_ref

so that a later condition change can be resolved by the dependency matrix rather
than by re-running everything, and so an item computed under an old condition can
never support a new determined conclusion.

The data source is the run's own state. Nothing here contacts the network or a
model.
"""
from __future__ import annotations

import execution_conditions as EC

KINDS = ("raw_waveform", "quality", "method_applicability", "onset", "stability",
         "aggregate")


def dependency_fields(kind: str, requested: dict) -> dict:
    """The subset of the current condition that this evidence kind depends on."""
    deps = EC.DEPENDENCY_MATRIX.get(kind, ("scope",))
    return {k: requested.get(k) for k in deps}


def register(state: dict, *, kind: str, evidence_id: str, requested: dict,
             effective_parameters=None, source_sha256=None,
             participating=None, check_status="ok", tool_event_ref=None,
             actor="deterministic_guard") -> dict:
    """Record one derived item and return it. Re-registering replaces in place."""
    bucket = state.setdefault("derived_evidence_checks", {})
    entry = {
        "evidence_id": evidence_id,
        "evidence_kind": kind,
        "source_sha256": source_sha256,
        "condition_ref": requested.get("condition_id"),
        "effective_parameters": dict(effective_parameters or {}),
        "participating_candidate_ids": list(participating or []),
        "dependency_fields": dependency_fields(kind, requested),
        "freshness": "valid",
        "check_status": check_status,
        "tool_event_ref": tool_event_ref,
        "actor": actor,
    }
    bucket[evidence_id] = entry
    return entry


def validity(state: dict, requested: dict) -> dict:
    """Split the registered items into valid / stale under the current conditions."""
    valid, stale = {}, {}
    for key, entry in (state.get("derived_evidence_checks") or {}).items():
        if not isinstance(entry, dict):
            continue
        if EC.evidence_is_stale(entry, requested):
            stale[key] = entry
        elif entry.get("freshness") == "stale":
            # Already marked by a previous pass; keep it out of the valid set.
            stale[key] = entry
        else:
            valid[key] = entry
    return {"valid": valid, "stale": stale,
            "stale_ids": sorted(stale), "valid_ids": sorted(valid)}


def invalidate(state: dict, requested: dict, *, reason=None) -> list:
    """Mark every item the current conditions invalidated. Preserves history."""
    stale = []
    for bucket in ("derived_evidence_checks", "stability_checks"):
        for key, entry in (state.get(bucket) or {}).items():
            if not isinstance(entry, dict):
                continue
            if EC.evidence_is_stale(entry, requested):
                entry["freshness"] = "stale"
                entry["stale_reason"] = reason or (
                    "该派生证据依赖的条件字段在本轮发生变化，需按当前条件重算")
                stale.append(f"{bucket}:{key}")
    return stale


def carryover(state: dict, requested: dict, *, evidence_ids=None) -> list:
    """Mark items that stay valid across a condition change as carried over.

    A carried item was NOT computed by this round's agent or this round's guard.
    Saying so keeps the provenance honest: the box's "what did the agent actually
    do" list must not silently adopt a parent's computation.
    """
    carried = []
    for key, entry in (state.get("derived_evidence_checks") or {}).items():
        if not isinstance(entry, dict):
            continue
        if evidence_ids is not None and key not in evidence_ids:
            continue
        if not EC.evidence_is_stale(entry, requested):
            entry["carryover"] = True
            entry["carryover_source"] = entry.get("actor")
            entry["actor"] = "carryover"
            entry["freshness"] = "cached_equivalent"
            carried.append(key)
    return carried


def source_sha(record: dict) -> str | None:
    """The waveform hash this evidence was computed from, if known."""
    return (record.get("raw_waveform_sha256") or record.get("waveform_sha256")
            or record.get("derived_waveform_sha256"))


def quality_available(record: dict) -> bool:
    """Whether a real acquisition measured quality facts for this observation.

    Quality/method-applicability questions are answerable from facts recorded at
    acquisition; the model must not be forced to compute an onset it does not need
    merely in order to cite something. The id is only citable when such a fact was
    actually measured, never when the record is a placeholder.
    """
    if not record:
        return False
    return bool(record.get("status") is not None
                or record.get("channel_integrity_flags")
                or record.get("sampling_rate_hz") is not None
                or record.get("acquisition_status") is not None)


def applicability_judged_under(record: dict, requested: dict) -> bool:
    """Whether this record's applicability was judged under the CURRENT goal/band."""
    applicability = record.get("method_applicability") or {}
    goal = requested.get("method_goal")
    if goal not in applicability:
        return False
    band = (record.get("band_support") or {}).get("target_band_hz")
    want = requested.get("target_band_hz")
    if not band or not want or len(band) != 2 or len(want) != 2:
        return False
    return EC._same_band([float(b) for b in band], [float(b) for b in want])