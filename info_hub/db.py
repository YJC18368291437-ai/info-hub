"""独立 SQLite 存储。messages 使用内部 ID 与 account/group/msg 唯一键；升级前在线备份，迁移见 migrations.py；历史投递与反馈保留。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL DEFAULT 'legacy',
    msg_id    INTEGER NOT NULL,
    group_id  TEXT NOT NULL,
    user_id   TEXT NOT NULL,
    role      TEXT DEFAULT '',
    nickname  TEXT,
    ts        INTEGER NOT NULL,
    text      TEXT,
    links     TEXT DEFAULT '[]',
    images    TEXT DEFAULT '[]',
    files     TEXT DEFAULT '[]',
    at_me     INTEGER DEFAULT 0,
    at_all    INTEGER DEFAULT 0,
    reply_to  INTEGER,
    block_id  INTEGER,
    raw       TEXT,
    UNIQUE(account_id, group_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_pending ON messages(block_id);
CREATE INDEX IF NOT EXISTS idx_msg_group_ts ON messages(group_id, ts);

CREATE TABLE IF NOT EXISTS blocks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT NOT NULL,
    start_ts    INTEGER,
    end_ts      INTEGER,
    msg_ids     TEXT DEFAULT '[]',
    text        TEXT,
    images      TEXT DEFAULT '[]',
    files       TEXT DEFAULT '[]',
    topic       TEXT,
    decided_by  TEXT,
    confident   INTEGER DEFAULT 1,
    pushed      INTEGER DEFAULT 0,
    mail_pushed INTEGER DEFAULT 0,
    title       TEXT,
    created_ts  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_blocks_pending ON blocks(pushed, group_id);

CREATE TABLE IF NOT EXISTS pushed (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id      INTEGER,
    chat_id       INTEGER,
    thread_id     INTEGER,
    tg_message_id INTEGER,
    ts            INTEGER
);

CREATE TABLE IF NOT EXISTS feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER,
    group_id        TEXT,
    user_id         TEXT,
    text            TEXT,
    ai_topic        TEXT,
    verdict         TEXT,
    corrected_topic TEXT
);

CREATE TABLE IF NOT EXISTS memo (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER,
    block_id      INTEGER,
    source_group  TEXT,
    text          TEXT,
    tg_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS digests (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER,
    group_id  TEXT,
    topic     TEXT,
    quotes    TEXT,
    html      TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS newspaper_items (
    kind    TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    state   TEXT NOT NULL DEFAULT 'pending',
    paper_key TEXT,
    sent_ts INTEGER,
    PRIMARY KEY(kind, item_id)
);
CREATE INDEX IF NOT EXISTS idx_newspaper_state ON newspaper_items(kind, state);

CREATE TABLE IF NOT EXISTS newspaper_delivery_manifest (
    delivery_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ids_json TEXT NOT NULL,
    cursor_json TEXT NOT NULL,
    finalized INTEGER NOT NULL DEFAULT 0
);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                from info_hub.migrations import upgrade
                self._conn.execute('PRAGMA busy_timeout=10000')
                upgrade(self._conn, path)
                self._conn.execute('PRAGMA journal_mode=WAL')
                self._conn.executescript(_SCHEMA)
                with self._conn:
                    self._migrate()
        except BaseException:
            self._conn.close()
            raise

    def _migrate(self) -> None:
        """给已存在的表补列。"""
        for table, col in (("messages", "role"), ("messages", "images"), ("messages", "files"),
                           ("blocks", "images"), ("blocks", "files"),
                           ("digests", "topic"), ("digests", "html"),
                           ("blocks", "title")):
            cols = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(blocks)")}
        if "mail_pushed" not in cols:
            self._conn.execute("ALTER TABLE blocks ADD COLUMN mail_pushed INTEGER DEFAULT 0")

    # ---------- 基础 ----------
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            with self._conn:
                cur = self._conn.execute(sql, tuple(params))
            return cur

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    # ---------- messages ----------
    def add_message(self, m: dict) -> None:
        self._exec(
            """INSERT INTO messages
               (account_id, msg_id, group_id, user_id, role, nickname, ts, text, links, images, files, at_me, at_all, reply_to, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,group_id,msg_id) DO NOTHING""",
            (
                str(m.get('account_id') or m.get('raw', {}).get('self_id') or 'legacy'),
                m["msg_id"], m["group_id"], m["user_id"], m.get("role", ""), m.get("nickname", ""),
                m["ts"], m.get("text", ""), json.dumps(m.get("links", []), ensure_ascii=False),
                json.dumps(m.get("images", []), ensure_ascii=False),
                json.dumps(m.get("files", []), ensure_ascii=False),
                1 if m.get("at_me") else 0, 1 if m.get("at_all") else 0,
                m.get("reply_to"), json.dumps(m.get("raw", {}), ensure_ascii=False),
            ),
        )

    def pending_messages(self, limit: int = 500, after_id: int = 0) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM messages WHERE block_id IS NULL AND id>? ORDER BY id LIMIT ?",
            (after_id, limit),
        )

    def priority_messages(self, course_groups, limit=200):
        groups=list(course_groups)
        condition='at_all=1'
        if groups:
            condition += f" OR group_id IN ({','.join('?' for _ in groups)})"
        return self._query('SELECT * FROM messages WHERE block_id IS NULL AND ('+condition+') ORDER BY id LIMIT ?',(*groups,limit))

    def assign_block(self, msg_ids: list[int], block_id: int) -> None:
        if not msg_ids:
            return
        q = ",".join("?" for _ in msg_ids)
        self._exec(f"UPDATE messages SET block_id=? WHERE id IN ({q}) AND block_id IS NULL", [block_id, *msg_ids])

    # ---------- blocks ----------
    def create_block(self, group_id: str, msgs: list[sqlite3.Row], text: str,
                     images: list[str] | None = None,
                     files: list[dict] | None = None) -> int:
        ids = [int(m["id"]) for m in msgs]
        ts = int(time.time())
        with self._lock, self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            current = self._conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE block_id IS NULL AND group_id=? AND id IN ({','.join('?' for _ in ids)})",
                (group_id, *ids)).fetchone()[0]
            if current != len(ids):
                raise ValueError('Messages already assigned or wrong group')
            cur = self._conn.execute(
                """INSERT INTO blocks (group_id, start_ts, end_ts, msg_ids, text, images, files, pushed, created_ts)
                   VALUES (?,?,?,?,?,?,?,0,?)""",
                (group_id, int(msgs[0]["ts"]), int(msgs[-1]["ts"]),
                 json.dumps(ids), text, json.dumps(images or [], ensure_ascii=False),
                 json.dumps(files or [], ensure_ascii=False), ts),
            )
            block_id = int(cur.lastrowid)
            self._conn.execute(f"UPDATE messages SET block_id=? WHERE id IN ({','.join('?' for _ in ids)})", (block_id, *ids))
        return block_id

    def block_messages(self, block_id: int) -> list[sqlite3.Row]:
        return self._query('SELECT * FROM messages WHERE block_id=? ORDER BY ts,id', (block_id,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def audit(self, entity: str, ident: object, action: str, detail: str = '') -> None:
        self._exec('INSERT INTO audit(ts,entity,entity_id,action,detail) VALUES(?,?,?,?,?)',
                   (int(time.time()), entity, str(ident), action, detail))

    def backlog(self) -> dict:
        return {name: self._query(sql)[0][0] for name, sql in {
            'pending_messages': 'SELECT COUNT(*) FROM messages WHERE block_id IS NULL',
            'orphan_blocks': 'SELECT COUNT(*) FROM blocks WHERE topic IS NULL',
            'unknown_deliveries': "SELECT COUNT(*) FROM deliveries WHERE state='unknown'",
            'queued_deliveries': "SELECT COUNT(*) FROM deliveries WHERE state IN ('queued','retryable')",
        }.items()}

    def set_block_topic(self, block_id: int, topic: str, decided_by: str, confident: bool = True) -> None:
        self._exec(
            "UPDATE blocks SET topic=?, decided_by=?, confident=? WHERE id=?",
            (topic, decided_by, 1 if confident else 0, block_id),
        )

    def unpushed_blocks(self, limit: int = 200) -> list[sqlite3.Row]:
        # 仅 @我 实时；其余（通知/链接/闲聊/计算机/生物/文件）全部定时。
        return self._query(
            "SELECT * FROM blocks WHERE pushed=0 AND topic='at_me' ORDER BY id LIMIT ?",
            (limit,),
        )

    def pending_topics(self, topics: tuple[str, ...], limit: int = 1000) -> list[sqlite3.Row]:
        q = ",".join("?" for _ in topics)
        return self._query(
            f"SELECT * FROM blocks WHERE pushed=0 AND topic IN ({q}) ORDER BY group_id, id LIMIT ?",
            (*topics, limit),
        )

    def orphan_blocks(self, limit: int = 200, after_id: int = 0) -> list[sqlite3.Row]:
        # 判类未完成(进程重启残留)的块：topic 为空
        return self._query(
            "SELECT * FROM blocks WHERE topic IS NULL AND id>? ORDER BY id LIMIT ?",
            (after_id, limit),
        )

    def mark_block_pushed(self, block_id: int) -> None:
        self._exec("UPDATE blocks SET pushed=1 WHERE id=?", (block_id,))

    def get_block(self, block_id: int) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM blocks WHERE id=?", (block_id,))
        return rows[0] if rows else None

    def blocks_in_window(self, group_id: str, topic: str, start_ts: int, end_ts: int) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM blocks WHERE group_id=? AND topic=? AND created_ts>=? AND created_ts<? ORDER BY id",
            (group_id, topic, start_ts, end_ts),
        )

    # ---------- 邮件投放 ----------
    def set_block_title(self, block_id: int, title: str) -> None:
        self._exec("UPDATE blocks SET title=? WHERE id=?", (title, block_id))

    def untitled_blocks(self, since_ts: int = 0, topics: tuple[str, ...] | None = None,
                        limit: int = 200) -> list[sqlite3.Row]:
        """标题待生成（含旧块补 7 天）的块；topics 非空时只取这些话题。"""
        sql = "SELECT * FROM blocks WHERE (title IS NULL OR title='') AND created_ts>=?"
        params: list = [since_ts]
        if topics:
            sql += f" AND topic IN ({','.join('?' for _ in topics)})"
            params.extend(topics)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        return self._query(sql, params)

    def unmailed_blocks(self, topics: tuple[str, ...], since_ts: int = 0, limit: int = 1000) -> list[sqlite3.Row]:
        """未被邮件投递过的块（按话题 + 创建时间窗口）。"""
        q = ",".join("?" for _ in topics)
        return self._query(
            f"SELECT * FROM blocks WHERE mail_pushed=0 AND topic IN ({q}) "
            "AND created_ts>=? ORDER BY id LIMIT ?",
            (*topics, since_ts, limit),
        )

    def mark_block_mailed(self, block_id: int) -> None:
        self._exec("UPDATE blocks SET mail_pushed=1 WHERE id=?", (block_id,))

    def mailed_block_ids(self, limit: int = 500) -> list[int]:
        """最近已邮件投递的块 id（供 IMAP 反馈映射）。"""
        rows = self._query("SELECT id FROM blocks WHERE mail_pushed=1 ORDER BY id DESC LIMIT ?", (limit,))
        return [int(r["id"]) for r in rows]

    # ---------- pushed ----------
    def record_pushed(self, block_id: int, chat_id: int, thread_id: int | None, tg_message_id: int) -> None:
        self._exec(
            "INSERT INTO pushed (block_id, chat_id, thread_id, tg_message_id, ts) VALUES (?,?,?,?,?)",
            (block_id, chat_id, thread_id, tg_message_id, int(time.time())),
        )

    # ---------- feedback ----------
    def add_feedback(self, group_id: str, user_id: str, text: str,
                     ai_topic: str, verdict: str, corrected_topic: str = "") -> None:
        self._exec(
            """INSERT INTO feedback (ts, group_id, user_id, text, ai_topic, verdict, corrected_topic)
               VALUES (?,?,?,?,?,?,?)""",
            (int(time.time()), group_id, user_id, text, ai_topic, verdict, corrected_topic),
        )

    def recent_feedback(self, limit: int = 12) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,))

    # ---------- memo ----------
    def add_memo(self, block_id: int, source_group: str, text: str, tg_message_id: int) -> None:
        self._exec(
            "INSERT INTO memo (ts, block_id, source_group, text, tg_message_id) VALUES (?,?,?,?,?)",
            (int(time.time()), block_id, source_group, text, tg_message_id),
        )

    # ---------- digests（简报：原话 + 渲染结果，供按钮复用） ----------
    def add_digest(self, group_id: str, topic: str, quotes: str, html: str) -> int:
        cur = self._exec(
            "INSERT INTO digests (ts, group_id, topic, quotes, html) VALUES (?,?,?,?,?)",
            (int(time.time()), group_id, topic, quotes, html),
        )
        return int(cur.lastrowid)

    def get_digest(self, digest_id: int) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM digests WHERE id=?", (digest_id,))
        return rows[0] if rows else None

    def set_digest_topic(self, digest_id: int, topic: str) -> None:
        self._exec("UPDATE digests SET topic=? WHERE id=?", (topic, digest_id))

    # ---------- newspaper ----------
    def newspaper_blocks(self, group_ids: tuple[str, ...] | list[str], since_ts: int,
                         after_id: int = 0, limit: int = 200) -> list[sqlite3.Row]:
        """尚未进入报纸的通知块（按内部 id 递增，排除已发送）。"""
        groups = [str(g) for g in group_ids]
        if not groups:
            return []
        q = ",".join("?" for _ in groups)
        return self._query(
            "SELECT b.* FROM blocks b LEFT JOIN newspaper_items n "
            "ON n.kind='notice' AND n.item_id=b.id "
            "WHERE b.topic IS NOT NULL AND b.created_ts>=? AND b.id>? "
            f"AND b.group_id IN ({q}) AND n.item_id IS NULL "
            "ORDER BY b.id LIMIT ?",
            (int(since_ts), int(after_id), *groups, int(limit)),
        )

    def newspaper_blocks_between(self, group_id: str, start_ts: int, end_ts: int,
                                 limit: int = 30) -> list[sqlite3.Row]:
        """同一群在时间窗内的通知块，用来给紧急提醒补上下文与图片。"""
        return self._query(
            "SELECT * FROM blocks WHERE topic IS NOT NULL AND group_id=? "
            "AND end_ts>=? AND start_ts<=? ORDER BY id LIMIT ?",
            (str(group_id), int(start_ts), int(end_ts), int(limit)),
        )

    def newspaper_messages(self, group_ids: tuple[str, ...] | list[str], since_ts: int,
                           after_id: int = 0, limit: int = 500,
                           kind: str = "tech") -> list[sqlite3.Row]:
        groups = [str(g) for g in group_ids]
        if not groups:
            return []
        q = ",".join("?" for _ in groups)
        return self._query(
            "SELECT m.* FROM messages m LEFT JOIN newspaper_items n "
            "ON n.kind=? AND n.item_id=m.id "
            "WHERE m.ts>=? AND m.id>? "
            f"AND m.group_id IN ({q}) AND n.item_id IS NULL "
            "ORDER BY m.id LIMIT ?",
            (str(kind), int(since_ts), int(after_id), *groups, int(limit)),
        )

    def mark_newspaper_items(self, kind: str, item_ids: list[int], paper_key: str,
                             state: str = "sent") -> None:
        if not item_ids:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO newspaper_items(kind,item_id,state,paper_key,sent_ts) "
                "VALUES(?,?,?,?,?) ON CONFLICT(kind,item_id) DO UPDATE SET "
                "state=excluded.state,paper_key=excluded.paper_key,sent_ts=excluded.sent_ts",
                [(kind, int(i), state, paper_key, int(time.time())) for i in item_ids],
            )

    def newspaper_sent_ids(self, kind: str) -> set[int]:
        return {int(r["item_id"]) for r in
                self._query("SELECT item_id FROM newspaper_items WHERE kind=? AND state='sent'", (kind,))}

    def finalize_newspaper_manifests(self) -> int:
        """Atomically apply immutable manifests whose delivery has positive confirmation."""
        now = int(time.time())
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                rows = list(self._conn.execute(
                    "SELECT n.* FROM newspaper_delivery_manifest n "
                    "JOIN deliveries d ON d.key=n.delivery_key "
                    "WHERE n.finalized=0 AND d.state='sent' ORDER BY d.sent_ts,d.key"))
                for row in rows:
                    ids = json.loads(row["ids_json"])
                    cursor = json.loads(row["cursor_json"])
                    if (not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids)
                            or not isinstance(cursor, dict)
                            or any(not isinstance(k, str) or not isinstance(v, str)
                                   for k, v in cursor.items())):
                        raise ValueError(f"Invalid newspaper manifest: {row['delivery_key']}")
                    self._conn.executemany(
                        "INSERT INTO newspaper_items(kind,item_id,state,paper_key,sent_ts) "
                        "VALUES(?,?,'sent',?,?) ON CONFLICT(kind,item_id) DO UPDATE SET "
                        "state='sent',paper_key=excluded.paper_key,sent_ts=excluded.sent_ts",
                        [(row["kind"], i, row["delivery_key"], now) for i in ids])
                    for key, value in cursor.items():
                        old_row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
                        old = str(old_row[0]) if old_row else ""
                        if key.endswith(("after_id", "urgent_id")):
                            value = str(max(int(old or 0), int(value)))
                        elif key.endswith(("last_slot", "last_eval", "last_forced")) and old:
                            value = max(old, value)
                        self._conn.execute(
                            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, value))
                    self._conn.execute(
                        "UPDATE newspaper_delivery_manifest SET finalized=1 WHERE delivery_key=?",
                        (row["delivery_key"],))
                self._conn.commit()
                return len(rows)
            except BaseException:
                self._conn.rollback()
                raise

    # ---------- kv ----------
    def kv_get(self, key: str, default: str = "") -> str:
        rows = self._query("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def kv_set(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


_db: Database | None = None


def get_db(path: Path | None = None) -> Database:
    global _db
    if _db is None:
        if path is None:
            from info_hub.settings import get_settings

            path = get_settings().db_path
        _db = Database(path)
    return _db
