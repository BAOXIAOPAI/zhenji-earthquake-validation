"""Read a separately extracted submission without network, keys or source fallback."""
import hashlib
import json
import os
import sys
from pathlib import Path

stage = Path(sys.argv[1]).resolve()
original = "/Users/baoxiaopai/Project/黑客松/codex/earthquake-validation"
report = {"package_root": str(stage), "checks": {}}


def isolation_guard(event, args):
    if event in ("socket.connect", "socket.connect_ex"):
        raise RuntimeError("package audit refused network access")
    if event == "subprocess.Popen":
        command = args[1]
        # ObsPy asks git for its own installed dependency version at import.
        if (not isinstance(command, (list, tuple)) or len(command) < 2
                or command[:2] not in (["git", "rev-parse"], ["git", "describe"])):
            raise RuntimeError("package audit refused a runtime subprocess")
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        path = str(args[0])
        if path.endswith(("deepseek_model.txt", "key及通知.txt")):
            raise RuntimeError("package audit refused credential access")
        if path.startswith(original) and not path.startswith(original + "/.venv/"):
            raise RuntimeError("package audit refused original workspace fallback")


os.chdir(stage)
os.environ["RUNLOG_DIR"] = str(stage / "replay/runlog")
os.environ["EQ_STRICT_OFFLINE"] = "1"
os.environ["MPLCONFIGDIR"] = str(stage.parent / "matplotlib-cache")
sys.addaudithook(isolation_guard)
sys.path.insert(0, str(stage / "main"))
sys.path.insert(0, str(stage / "explore/probes"))

import server

report["checks"]["module_isolation"] = {
    "ok": all(Path(module.__file__).is_relative_to(stage)
              for module in (server, server.core, server.review, server.taskmod,
                             server.runlog, server.core.fdsn)),
    "modules": {module.__name__: str(Path(module.__file__).relative_to(stage))
                for module in (server, server.core, server.review, server.taskmod,
                               server.runlog, server.core.fdsn)},
}

for label, call in (("health", server.health), ("demo_guide", server.demo_guide),
                    ("recent_runs", server.recent_runs), ("samples", server.samplesmod.samples)):
    try:
        result = call()
        report["checks"][label] = {"ok": True, "size": len(result)}
        if label == "health":
            report["checks"][label].update({
                "model_available": result["model_available"],
                "cumulative_cost_upper_cny": result["cumulative_cost_upper_cny"],
                "reason": result.get("model_unavailable_reason"),
                "ledger_created": (stage / "runs/model-budget-ledger.json").exists(),
            })
            report["checks"][label]["ok"] = (
                result.get("status") == "ok" and result["model_available"] is False
                and result["cumulative_cost_upper_cny"] is None
                and not report["checks"][label]["ledger_created"])
        elif label == "demo_guide":
            report["checks"][label]["status"] = result.get("status")
            report["checks"][label]["ok"] = result.get("status") == "ok"
    except Exception as exc:
        report["checks"][label] = {"ok": False, "error": str(exc)}

for run_id in ("r-e2e8419bf299785d", "r-3ae9c6948c2f8967"):
    try:
        ev = server.evidence(run_id)
        candidate_id = ev["acquisition"][0]["candidate_id"]
        waveform = server.waveform(run_id, candidate_id)
        channels = waveform.get("channels") or []
        native = ev.get("native") or {}
        sources = {key: (stage / "replay/runlog" / run_id / native[key]).is_file()
                   for key in ("native_sessions_db", "native_host_audit") if native.get(key)}
        report["checks"][run_id] = {
            "ok": ("error" not in ev and "error" not in waveform
                   and len(channels) == 3 and all(sources.values())),
            "error": waveform.get("error") or ev.get("error"),
            "source": waveform.get("source"), "channels": len(channels),
            "status": (ev.get("outcome") or {}).get("status"),
            "model_claim_accepted": ((ev.get("validator") or {}).get("delivered") or {}).get("model_claim_accepted"),
            "parent_run_id": ev.get("parent_run_id"),
            "native_referenced_files": sources,
        }
    except Exception as exc:
        report["checks"][run_id] = {"ok": False, "error": str(exc)}

required_sources = [
    "src/budget.mjs", "vendor/agnes-harness/tools/third-party-notices.mjs",
    "vendor/agnes-harness/packages/cli/tools/build-local.ts",
    "vendor/agnes-harness/packages/cli/launch/daemon-entry.ts",
    "vendor/agnes-harness/packages/base/tools/runtime-directory.ts",
    "vendor/agnes-harness/packages/base/extensions/hooks-runner/generated/cc-hook-map.json",
    "vendor/agnes-harness/packages/host/scripts/build-native.mjs",
    "vendor/agnes-harness/packages/system-node/scripts/build-native.mjs",
    "data/manifests/agh-commit.json", "data/manifests/agh-install.md",
]
missing = [name for name in required_sources if not (stage / name).is_file()]
report["checks"]["build_source_files"] = {"ok": not missing, "missing": missing}
report["checks"]["installation_artifacts_excluded"] = {
    "ok": not any((stage / name).exists() for name in (
        ".venv", "vendor/agnes-harness/node_modules", "vendor/agnes-harness/packages/cli/dist"))}
static_files = {}
for name in ("index.html", "app.js", "style.css"):
    path = stage / "main/static" / name
    static_files[name] = {"present": path.is_file(),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
report["checks"]["static_files"] = {"ok": all(f["present"] for f in static_files.values()),
                                        "files": static_files}
report["network_requests"] = 0
report["model_calls"] = 0
report["credential_reads"] = 0
report["dependency_environment"] = "Existing Python dependency environment only; no package installation was attempted."
report["passed"] = all(check.get("ok") for check in report["checks"].values())
print(json.dumps(report, ensure_ascii=False, indent=2))
