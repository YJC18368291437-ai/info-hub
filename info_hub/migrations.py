"""Offline, transactional schema upgrade. No migration deletes historical rows."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

VERSION = 4

TABLES = [
    """CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY, identity_key TEXT UNIQUE NOT NULL, version INTEGER NOT NULL,
      category TEXT NOT NULL, mandatory INTEGER NOT NULL DEFAULT 0,
      confidence REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open',
      negative INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
      first_seen INTEGER NOT NULL, updated_ts INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS event_sources (
      block_id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, version INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS event_versions (
      event_id INTEGER, version INTEGER, payload TEXT, changes TEXT, ts INTEGER,
      PRIMARY KEY(event_id,version))""",
    """CREATE TABLE IF NOT EXISTS deliveries (
      key TEXT PRIMARY KEY, message_id TEXT UNIQUE NOT NULL, event_id INTEGER,
      version INTEGER, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
      owner TEXT, lease_until INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
      next_retry INTEGER DEFAULT 0, stage TEXT, error TEXT, sent_ts INTEGER,
      uid TEXT, uidvalidity TEXT, payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS delivery_events (
      delivery_key TEXT, event_id INTEGER, version INTEGER,
      PRIMARY KEY(delivery_key,event_id))""",
    """CREATE TABLE IF NOT EXISTS audit (
      id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, entity TEXT, entity_id TEXT,
      action TEXT, detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS mail_observations (
      message_id TEXT PRIMARY KEY, kind TEXT, flagged INTEGER, ts INTEGER)""",
    """CREATE TABLE IF NOT EXISTS imap_folders (
      name TEXT PRIMARY KEY, uidvalidity TEXT, high_uid INTEGER DEFAULT 0,
      sweep_uid INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS imap_messages (
      folder TEXT, uidvalidity TEXT, uid INTEGER, message_id TEXT, event_id INTEGER,
      version INTEGER, flagged INTEGER, kind TEXT,
      PRIMARY KEY(folder,uidvalidity,uid))""",
    """CREATE TABLE IF NOT EXISTS newspaper_items (
      kind TEXT NOT NULL, item_id INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
      paper_key TEXT, sent_ts INTEGER, PRIMARY KEY(kind,item_id))""",
    """CREATE TABLE IF NOT EXISTS newspaper_delivery_manifest (
      delivery_key TEXT PRIMARY KEY, kind TEXT NOT NULL, ids_json TEXT NOT NULL,
      cursor_json TEXT NOT NULL, finalized INTEGER NOT NULL DEFAULT 0)""",
]


def backup(conn: sqlite3.Connection, path: Path) -> Path:
    folder = path.parent / 'backups'
    folder.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = folder / f'{path.name}.pre-v{VERSION}.{stamp}.sqlite'
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
        if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Database backup integrity check failed')
    finally:
        dest.close()
    return target


def upgrade(conn: sqlite3.Connection, path: Path) -> None:
    if conn.execute('PRAGMA user_version').fetchone()[0] >= VERSION:
        return
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
    if exists:
        backup(conn, path)  # Fail closed before ANY schema mutation.
    conn.execute('BEGIN IMMEDIATE')
    try:
        if exists:
            cols = {r[1] for r in conn.execute('PRAGMA table_info(messages)')}
            if 'id' not in cols:
                rows = [dict(r) for r in conn.execute('SELECT * FROM messages')]
                conn.execute('ALTER TABLE messages RENAME TO messages_legacy_v1')
                conn.execute("""CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, account_id TEXT NOT NULL DEFAULT 'legacy',
                    msg_id INTEGER NOT NULL, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    role TEXT DEFAULT '', nickname TEXT, ts INTEGER NOT NULL, text TEXT, links TEXT DEFAULT '[]',
                    images TEXT DEFAULT '[]', files TEXT DEFAULT '[]', at_me INTEGER DEFAULT 0,
                    at_all INTEGER DEFAULT 0, reply_to INTEGER, block_id INTEGER, raw TEXT,
                    UNIQUE(account_id,group_id,msg_id))""")
                for row in rows:
                    raw = json.loads(row.get('raw') or '{}')
                    row['account_id'] = str(raw.get('self_id') or 'legacy')
                    keys = list(row)
                    conn.execute(f"INSERT INTO messages ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", list(row.values()))
                # Legacy table intentionally retained for forensic recovery; never read by pipeline.
                conn.execute('DROP INDEX IF EXISTS idx_msg_pending')
                conn.execute('DROP INDEX IF EXISTS idx_msg_group_ts')
        for sql in TABLES:
            conn.execute(sql)
        # Historical optional columns are part of the SAME transaction and backup.
        for table, col, definition in (
            ('messages','role','TEXT DEFAULT \'\''),('blocks','images','TEXT'),('blocks','files','TEXT'),
            ('blocks','title','TEXT'),('blocks','mail_pushed','INTEGER DEFAULT 0'),
            ('digests','topic','TEXT'),('digests','html','TEXT')):
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
            if cols and col not in cols:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {definition}')
        conn.execute(f'PRAGMA user_version={VERSION}')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
