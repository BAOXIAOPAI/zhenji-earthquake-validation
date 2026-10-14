"""Per-run records bound to one unique run id.

Every run gets its own directory under `main/runlog/<run_id>/`. Historical runs
under `main/runs/` are never touched. Files that describe what happened are
write-once: a re-run creates a new directory instead of overwriting a record.

Failure is a first-class outcome. A run that failed keeps its acquired evidence
and its incurred cost, and is never presented as a live success.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Relocatable run store. `RUNLOG_DIR` lets an independent environment (clean-env
# reproduction) or a test put its run records somewhere else. core.py reads the
# same variable, so the raw/derived files and the run records can never diverge.
RUNLOG = Path(os.environ.get("RUNLOG_DIR") or (ROOT / "main/runlog"))
LEDGER = ROOT / "runs/model-budget-ledger.json"

# Failure kinds for. `succeeded` is the only non-failure status.
SUCCEEDED = "succeeded"
FAILURE_KINDS = (
    "discovery_failure",  # station discovery failed before the model was started
    "subprocess_exit",     # the AGH driver exited non-zero
    "timeout",             # the driver exceeded its wall-clock budget
    "no_execution_record",  # driver exited 0 but produced no record for this run
    "record_mismatch",      # a record exists but belongs to a different run/task
    "model_fatal",          # the session itself raised
    "tool_failure",         # an MCP tool call failed
    "task_mismatch",        # the task the driver used is not the frozen one
    # Resource terminal states (package D): a run whose cleanup failed or timed out
    # is not a full success, and the distinction is preserved in the record.
    "cleanup_failed",       # the owned daemon/session resources were not reclaimed
    "cleanup_timeout",      # cleanup exceeded its bounded grace period
    "internal_error",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    """Unique per execution. Re-running a task yields a new id, never a rewrite."""
    return "r-" + secrets.token_hex(8)


def run_dir(run_id: str) -> Path:
    return RUNLOG / run_id


def init_run(run_id: str, task: dict, *, arm: str, variant: str | None = None,
             model: str | None = None, provider: str | None = None,
             extra: dict | None = None) -> Path:
    """Create the run directory and freeze its identity."""
    folder = run_dir(run_id)
    if folder.exists():
        raise FileExistsError(f"run already exists: {run_id}")
    (folder / "raw").mkdir(parents=True)
    (folder / "derived").mkdir(parents=True)
    write_once(folder / "task.json", task)
    write_once(folder / "run.json", {
        "run_id": run_id, "task_id": task["task_id"], "arm": arm,
        "variant": variant, "model": model, "provider": provider,
        "started_at": _now(), "task_sha256": task["task_sha256"],
        **(extra or {}),
    })
    write_once(folder / "errors.json", {"failures": []})
    return folder


def write_once(path: Path, obj) -> Path:
    """Write a file only if it does not exist. Records are never rewritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"record already written: {path}")
    if path.suffix == ".json":
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    elif isinstance(obj, bytes):
        path.write_bytes(obj)
    else:
        path.write_text(str(obj))
    return path


def write_text_once(path: Path, text: str) -> Path:
    return write_once(path, text)


def append_jsonl(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_failure(run_id: str, kind: str, detail: str, **extra) -> dict:
    """Append a failure to the run's error record. Never clears prior failures."""
    if kind not in FAILURE_KINDS:
        raise ValueError(f"unknown failure kind: {kind}")
    folder = run_dir(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "errors.json"
    state = json.loads(path.read_text()) if path.exists() else {"failures": []}
    entry = {"kind": kind, "detail": str(detail)[:500], "at": _now(), **extra}
    state["failures"].append(entry)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str))
    return entry


def outcome(run_id: str, status: str, *, failure_kind: str | None = None,
            failure_detail: str | None = None, **extra) -> dict:
    """The run's terminal outcome. Failure keeps evidence and cost attached."""
    if status == SUCCEEDED and failure_kind:
        raise ValueError("a successful run cannot carry a failure kind")
    if status != SUCCEEDED and failure_kind is None:
        raise ValueError("a failed run must carry a failure kind")
    if failure_kind is not None and failure_kind not in FAILURE_KINDS:
        raise ValueError(f"unknown failure kind: {failure_kind}")
    record = {"run_id": run_id, "status": status, "failure_kind": failure_kind,
              "failure_detail": (str(failure_detail)[:500] if failure_detail else None),
              "recorded_at": _now(), **extra}
    path = run_dir(run_id) / "result.json"
    if path.exists():  # never overwrite a terminal outcome
        raise FileExistsError(f"outcome already recorded for {run_id}")
    write_once(path, record)
    return record


def read_outcome(run_id: str) -> dict | None:
    path = run_dir(run_id) / "result.json"
    return json.loads(path.read_text()) if path.exists() else None


def record_task_binding(run_id: str, task_id: str, prompt_hash: str,
                        mcp_state_hash: str | None = None) -> dict:
    """Assert that prompt, MCP state and result belong to the same task.

    This is what makes the acceptance instance checkable: the question in the
    prompt and the question in the MCP state must both hash to the frozen task.
    """
    binding = {
        "run_id": run_id, "task_id": task_id,
        "prompt_sha256": prompt_hash, "mcp_state_sha256": mcp_state_hash,
        "bound_at": _now(),
    }
    write_once(run_dir(run_id) / "binding.json", binding)
    return binding


def cost_for_run(run_id: str) -> dict:
    """Model cost for exactly this run, read from the shared ledger.

    Returns known=False when the ledger holds no entry for the run, so the
    caller can display "unknown" rather than a fake zero.
    """
    if not LEDGER.exists():
        return {"known": False, "reason": "ledger_missing", "cost_upper_cny": None}
    entries = json.loads(LEDGER.read_text()).get("entries", [])
    mine = [e for e in entries if e.get("case_id") == run_id]
    if not mine:
        return {"known": False, "reason": "no_ledger_entries_for_run",
                "cost_upper_cny": None, "entries": 0}
    total = sum(e.get("cost_upper_cny") or e.get("reserved_cny") or 0.0 for e in mine)
    return {"known": True, "cost_upper_cny": round(total, 6), "entries": len(mine),
            "settled": all(e.get("status") == "settled" for e in mine)}


def write_cost(run_id: str, model_cost: dict, *, http_attempts: int = 0,
               cache_hits: int = 0, logical_acquisitions: int = 0,
               bytes_total: int = 0, elapsed_ms: int | None = None) -> dict:
    """Cost/effort record that separates real attempts from logical operations."""
    record = {
        "run_id": run_id,
        "model_cost_known": bool(model_cost.get("known")),
        "model_cost_upper_cny": model_cost.get("cost_upper_cny"),
        "model_cost_entries": model_cost.get("entries", 0),
        "model_cost_reason": model_cost.get("reason"),
        "logical_acquisitions": logical_acquisitions,
        "http_attempts": http_attempts,
        "cache_hits": cache_hits,
        "bytes_total": bytes_total,
        "elapsed_ms": elapsed_ms,
        "recorded_at": _now(),
    }
    write_once(run_dir(run_id) / "cost.json", record)
    return record
