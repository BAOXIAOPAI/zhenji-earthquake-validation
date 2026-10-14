"""One resolver for every execution resource limit (execution book §6.2, §15).

The round's defect was that the same limit lived in three places and disagreed:
`prototype/model-budget.json` said 12 model requests per case, `agent_run.mjs`
handed the bridge 24, and `provider_bridge.mjs` hard-coded a separate tool-call
ceiling of 24. A session could therefore die with an unexpected `Case request
limit` halfway through, or a tool budget that no document described.

`prototype/model-budget.json` is the single fact source. Python reads it here;
Node reads the same file (`provider_bridge.mjs`, `agent_run_optimization.mjs`).
Neither side invents a second number. The money guard (`guard_limit_cny`) is the
user's 20 CNY authorisation and is NOT changed by this module.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGET_FILE = ROOT / "prototype/model-budget.json"

# The book's §6.2 common ceiling. These are the values written into
# model-budget.json; the module raises if the file disagrees, so a silent edit to
# one side cannot pass unnoticed.
EXPECTED = {
    "max_unique_candidates_episode": 6,
    "max_requests_per_case": 12,
    "soft_request_line": 8,
    "max_tools_per_case": 32,
    "request_timeout_ms": 180000,
    "case_timeout_ms": 540000,
    "max_output_tokens": 8192,
    "max_output_tokens_fallback": 4096,
    "cleanup_grace_ms": 45000,
}


class LimitConfigError(RuntimeError):
    pass


def _load():
    if not BUDGET_FILE.exists():
        raise LimitConfigError(f"missing resource config: {BUDGET_FILE}")
    return json.loads(BUDGET_FILE.read_text())


def limits() -> dict:
    """The resolved common limits, cross-checked against the book's defaults."""
    cfg = _load()
    out = {}
    missing = []
    for key, expected in EXPECTED.items():
        if key not in cfg:
            missing.append(key)
            continue
        out[key] = cfg[key]
    if missing:
        raise LimitConfigError(
            "resource config is missing the agreed keys: " + ", ".join(missing))
    # The book fixes these; a different value is a protocol change, not a tuning
    # decision, so it is refused loudly rather than used.
    disagree = {k: (out[k], EXPECTED[k]) for k in EXPECTED if out[k] != EXPECTED[k]}
    if disagree:
        detail = "; ".join(f"{k}: file={a} expected={b}" for k, (a, b) in disagree.items())
        raise LimitConfigError("resource limits disagree with the execution book: " + detail)
    out["guard_limit_cny"] = cfg.get("guard_limit_cny", cfg.get("total_limit"))
    out["total_limit"] = cfg.get("total_limit")
    return out


def output_tokens(*, upstream_refused_8192: bool = False) -> int:
    """8192 for all three Agent strategies, or the disclosed 4096 fallback.

    The fallback is a single global switch applied to every arm — never a per-run
    choice made after seeing an answer.
    """
    cfg = limits()
    return (cfg["max_output_tokens_fallback"] if upstream_refused_8192
            else cfg["max_output_tokens"])


def unique_candidate_ceiling(user_requested: int | None = None) -> int:
    """The episode's independent-candidate ceiling; a user may ask for fewer."""
    cap = limits()["max_unique_candidates_episode"]
    if user_requested is None:
        return cap
    requested = int(user_requested)
    if requested < 0:
        raise ValueError("candidate ceiling cannot be negative")
    return min(cap, requested)


def remaining_limits(*, unique_candidates_used: int, model_requests_used: int,
                     tool_calls_used: int, user_requested_candidates: int | None = None,
                     model_requests_known: bool = True,
                     tool_calls_known: bool = True) -> dict:
    """Remaining allowance, `None` (unknown) rather than a fake zero when unmeasured.

    The book: "计量文件缺失／过期时返回 unknown，不能填 0". A missing counter is
    reported as `None` with `known: false`; only a measured counter becomes a
    number.
    """
    cfg = limits()
    ceiling = unique_candidate_ceiling(user_requested_candidates)
    remaining = {
        "unique_candidates": max(0, ceiling - int(unique_candidates_used)),
        "unique_candidates_ceiling": ceiling,
        "model_requests": (max(0, cfg["max_requests_per_case"] - int(model_requests_used))
                           if model_requests_known else None),
        "model_requests_soft_line": cfg["soft_request_line"],
        "tool_calls": (max(0, cfg["max_tools_per_case"] - int(tool_calls_used))
                       if tool_calls_known else None),
        "known": {"model_requests": bool(model_requests_known),
                  "tool_calls": bool(tool_calls_known)},
    }
    return remaining