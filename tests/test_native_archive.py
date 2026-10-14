"""Regression: the pre-cleanup session-store archive must not be empty.

SQLite in WAL mode keeps recently committed rows in ``sessions.db-wal`` until a
checkpoint, so the old plain ``copyFile(sessions.db, native_sessions.db)``
archived an empty database. The product now snapshots through
``main/native_archive.mjs`` (``VACUUM INTO``, with a
``wal_checkpoint(FULL)+copy`` fallback).

Choice of probe: the archive helper is an ESM module that the driver imports, so
this test shells out to ``node tests/native_archive_probe.mjs`` rather than
reimplementing the snapshot in Python — exactly the pattern
``tests/test_model_delivery.py`` uses for ``tsx``. The WAL database itself is
created in Python and its writer connection is left open (committed but never
closed or checkpointed), so the rows the assertion looks for exist only in the
WAL at snapshot time.
"""
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "native_archive_probe.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node runtime not available")


def make_wal_db(path: Path, keys=("s0", "s1", "s2", "s3", "s4")):
    """Create a WAL-mode ``events`` table and leave the rows in the WAL.

    The returned connection stays open: closing it (or letting sqlite3
    checkpoint) would fold the rows back into the main file and make the test
    pass even for a plain copy.
    """
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(
        "CREATE TABLE events(session_key TEXT, seq INTEGER, ts TEXT, type TEXT, "
        "actor TEXT, origin TEXT, register TEXT, ignorable INTEGER)")
    writer.executemany(
        "INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
        [(k, i, "2025-03-03T13:02:20Z", "tool/call", "driver", "native",
          "tool_use", 0) for i, k in enumerate(keys)])
    writer.commit()
    return writer


def run_probe(source: Path, target: Path):
    proc = subprocess.run([NODE, str(PROBE), str(source), str(target)],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-400:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_wal_archive_contains_the_committed_rows(tmp_path):
    db = tmp_path / "sessions.db"
    writer = make_wal_db(db)
    try:
        # Precondition: the rows really are still in the WAL, not the main file.
        wal = Path(str(db) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0, "WAL was flushed; test is vacuous"

        archived = tmp_path / "native_sessions.db"
        result = run_probe(db, archived)

        assert result["ok"] is True, result
        assert result["method"] == "vacuum_into", result
        assert "events" in result["tables"]
        assert result["events_count"] == 5
        assert result["session_keys"] == ["s0", "s1", "s2", "s3", "s4"]
    finally:
        writer.close()


def test_snapshot_replaces_an_existing_stale_target(tmp_path):
    """VACUUM INTO refuses an existing file, so a stale archive must be removed."""
    db = tmp_path / "sessions.db"
    writer = make_wal_db(db)
    try:
        archived = tmp_path / "native_sessions.db"
        archived.write_bytes(b"stale archive, not a database")
        result = run_probe(db, archived)
        assert result["ok"] is True, result
        assert result["events_count"] == 5
    finally:
        writer.close()