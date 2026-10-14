// Consistent single-file copy of a live SQLite session store.
//
// SQLite in WAL mode keeps recently committed rows in <db>-wal until a
// checkpoint, so copying only <db> archives a stale or empty snapshot. Two
// paths produce a consistent copy:
//   * VACUUM INTO '<target>' (SQLite >= 3.27) rebuilds the database from a
//     read transaction that includes the WAL, straight into a new file;
//   * the fallback folds the WAL back into the main file with
//     PRAGMA wal_checkpoint(FULL) and then copies that single file.
// VACUUM INTO refuses to overwrite, so any previous target is removed first.
import {copyFile, rm} from 'node:fs/promises';
import {existsSync} from 'node:fs';

export async function snapshotSqliteDb(source, target) {
  if (!existsSync(source)) return {ok: false, skipped: true, error: 'source does not exist'};
  const {DatabaseSync} = await import('node:sqlite');
  await rm(target, {force: true});
  try {
    const db = new DatabaseSync(source, {readOnly: true});
    try { db.exec(`VACUUM INTO '${String(target).replace(/'/g, "''")}'`); }
    finally { db.close(); }
    return {ok: true, method: 'vacuum_into'};
  } catch (vacuumError) {
    try {
      const db = new DatabaseSync(source);
      try { db.exec('PRAGMA wal_checkpoint(FULL)'); }
      finally { db.close(); }
      await copyFile(source, target);
      return {ok: true, method: 'wal_checkpoint_full+copy',
        vacuum_error: String(vacuumError?.message ?? vacuumError).slice(0, 200)};
    } catch (copyError) {
      return {ok: false, method: 'failed',
        error: String(copyError?.message ?? copyError).slice(0, 200),
        vacuum_error: String(vacuumError?.message ?? vacuumError).slice(0, 200)};
    }
  }
}