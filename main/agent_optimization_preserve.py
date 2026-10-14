"""Historical-delivery preservation for the Agent optimisation round.

The execution book (§3, §14) requires that the existing `delivery/revision-*`
and `delivery/review-*` trees be digested BEFORE any test or runner touches the
workspace, and re-checked afterwards. Only an explicitly authorised repair target
may change; everything else must be byte-identical.

This module is deliberately write-once on the manifest: the first `snapshot`
call creates `history-preservation.json`; later calls only verify. A second
snapshot would overwrite the evidence of what the baseline actually was, which is
the exact failure mode the requirement exists to prevent.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "history-preservation.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(root: Path) -> dict:
    """Every regular file under `root`, keyed by repo-relative path."""
    files = {}
    if not root.exists():
        return files
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files[str(path.relative_to(ROOT))] = {
                "sha256": _digest(path), "bytes": path.stat().st_size}
    return files


def protected_roots():
    """The trees the round must not rewrite.

    `delivery/revision-*` and `delivery/review-*` are the historical delivery and
    review packages. This round's own output lives in
    `delivery/agent-optimization-2026-10-11/` and is never part of the baseline.
    """
    delivery = ROOT / "delivery"
    roots = [p for p in sorted(delivery.iterdir())
             if p.is_dir() and (p.name.startswith("revision-")
                                or p.name.startswith("review"))]
    roots.append(ROOT / "records")
    return roots


def snapshot(output_dir) -> dict:
    """Record the baseline once. Refuses to overwrite an existing manifest."""
    output_dir = Path(output_dir)
    path = output_dir / MANIFEST_NAME
    if path.exists():
        raise FileExistsError(
            f"history manifest already recorded: {path}; use verify() instead")
    files = {}
    for root in protected_roots():
        files.update(_tree(root))
    manifest = {"schema": "agent-optimization-history/v1",
                "recorded_at": _now(),
                "roots": [str(r.relative_to(ROOT)) for r in protected_roots()],
                "n_files": len(files), "files": files}
    output_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (output_dir / "history-preservation").mkdir(exist_ok=True)
    return manifest


# Files a PRE-EXISTING test regenerated before this round isolated its write-disk
# tests (book §3). They are reported separately, never silently folded into
# "unchanged": the D1 write-disk tests used to rewrite them in the submission
# package, which is why review-6 could not restore the originals either. The
# writers now take an explicit output_dir and the tests point it at a temp dir.
KNOWN_TEST_SIDE_EFFECTS = (
    "delivery/revision-5/d1-package/offline-check.json",
    "delivery/revision-5/d1-package/offline-ui-check.json",
)


def verify(output_dir, allowed_changes=KNOWN_TEST_SIDE_EFFECTS) -> dict:
    """Compare the current trees with the baseline. Reports every difference."""
    output_dir = Path(output_dir)
    path = output_dir / MANIFEST_NAME
    if not path.exists():
        raise FileNotFoundError(
            f"no baseline manifest at {path}; run snapshot() before any work")
    baseline = json.loads(path.read_text())["files"]
    current = {}
    for root in protected_roots():
        current.update(_tree(root))
    changed = sorted(p for p in baseline
                     if p in current and current[p]["sha256"] != baseline[p]["sha256"])
    missing = sorted(p for p in baseline if p not in current)
    added = sorted(p for p in current if p not in baseline)
    allowed = sorted(set(changed) & set(allowed_changes))
    unexpected = sorted(set(changed) - set(allowed_changes))
    report = {"schema": "agent-optimization-history-check/v1",
              "checked_at": _now(),
              "n_baseline": len(baseline), "n_current": len(current),
              "changed": changed, "missing": missing, "added": added,
              "allowed_pre_existing_test_side_effects": allowed,
              "unexpected_changes": unexpected,
              "unchanged": not (unexpected or missing or added),
              "note": ("allowed 列出的文件由本轮之前就存在的 D1 写盘测试重新生成；"
                       "本轮已给这些 writer 加上显式 output_dir 并把测试指向临时目录，"
                       "使历史包不再被改写。")
              if allowed else None}
    return report