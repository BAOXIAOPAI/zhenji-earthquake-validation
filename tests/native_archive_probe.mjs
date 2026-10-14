// Free probe for the session-store archiving helper: archives a source SQLite
// database with the product's own snapshotSqliteDb() and reports what the
// archived copy actually contains, as JSON on stdout. No model, no network.
//
//   node tests/native_archive_probe.mjs <source.db> <archived.db>
import {existsSync} from 'node:fs';
import {snapshotSqliteDb} from '../main/native_archive.mjs';

const [source, target] = process.argv.slice(2);
const out = {ok: false, method: null, tables: [], events_count: null,
  session_keys: [], error: null};

try {
  if (!source || !target) throw Error('usage: native_archive_probe.mjs <source> <target>');
  const snap = await snapshotSqliteDb(source, target);
  out.method = snap.method ?? null;
  if (!snap.ok) throw Error(snap.error ?? 'snapshot failed');
  const {DatabaseSync} = await import('node:sqlite');
  const db = new DatabaseSync(target, {readOnly: true});
  try {
    out.tables = db.prepare(
      "select name from sqlite_master where type='table' order by name").all()
      .map(r => r.name);
    if (out.tables.includes('events')) {
      out.events_count = db.prepare('select count(*) c from events').get().c;
      out.session_keys = db.prepare('select session_key from events order by seq')
        .all().map(r => r.session_key);
    }
  } finally { db.close(); }
  out.ok = true;
} catch (error) {
  out.error = String(error?.message ?? error).slice(0, 300);
}

if (!existsSync(source)) out.error = out.error ?? 'source does not exist';
console.log(JSON.stringify(out));