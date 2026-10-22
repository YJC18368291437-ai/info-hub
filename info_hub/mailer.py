"""邮件传输与增量 IMAP UID 反馈。真实连接需 LIVE_DELIVERY_ENABLED 且非 DRY_RUN；归档和删除不训练兴趣。所有事项发送经 outbox.py 逐封记账。"""
from __future__ import annotations

import asyncio
import base64
import email
import imaplib
import json
import logging
import re
import smtplib
import ssl
import threading
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, parseaddr
from typing import Iterable

from info_hub.db import Database
from info_hub.settings import Settings

logger = logging.getLogger("info_hub.mail")

# 每封邮件带一个块 id 头，IMAP 回读时据此映射到 DB
BLOCK_HEADER = "X-InfoHub-Block"

_FLAG_RE = re.compile(rb"\\Flagged", re.IGNORECASE)
_FOLDER_RE = re.compile(r'"([^"]*)"\s*$')


def decode_folder(value: str) -> str:
    def part(m):
        if not m[1]:
            return '&'
        token = m[1].replace(',', '/')
        try:
            return base64.b64decode(token + '='*((-len(token))%4), validate=True).decode('utf-16-be')
        except (ValueError, UnicodeError):
            return m[0]
    return re.sub(r'&([A-Za-z0-9+,]*)-', part, value)


def _folder_kind(name: str, uninterested: str = '不感兴趣') -> str:
    """把 IMAP 文件夹名归到语义位置。QQ 用英文名（无 SPECIAL-USE 标注）。"""
    name = decode_folder(name).strip()
    n = name.casefold()
    if n == uninterested.strip().casefold():
        return 'uninterested'
    if n == "inbox":
        return "inbox"
    if n in ("junk", "spam", "junk e-mail") or "垃圾邮件" in name:
        return "junk"
    if "deleted" in n or "trash" in n or "已删除" in name or "垃圾箱" in name:
        return "trash"
    if n == "archive" or "归档" in name:
        return "archive"
    if "sent" in n or "draft" in n or "草稿" in name or "已发送" in name:
        return "skip"
    return "other"


@dataclass
class MailAction:
    """从 IMAP 侧读回的反馈动作。"""

    block_id: int
    verdict: str  # "memo"（旗标）| "read"（归档）| "noise"（删除/垃圾）
    detail: str = ""


def decode_mime(value: str | None) -> str:
    """解码邮件头（QQ/中文主题常为 =?UTF-8?...?=）。"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def build_message(
    settings: Settings,
    *,
    subject: str,
    text: str,
    html: str | None = None,
    to: str | None = None,
    block_id: int | None = None,
    attachments: Iterable[tuple[str, bytes, str]] = (),
) -> EmailMessage:
    """组装一封邮件。attachments 为 (filename, data, mime_type) 序列。"""
    msg = EmailMessage()
    msg["From"] = formataddr((settings.mail_from_name, settings.mail_address))
    msg["To"] = to or settings.mail_address
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    if block_id is not None:
        msg[BLOCK_HEADER] = str(int(block_id))
    if html:
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(text)
    for name, data, mime in attachments:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=name)
    return msg


class MailSender:
    """SMTP 发信（QQ 邮箱：465 走 SSL，587 走 STARTTLS）。"""

    def __init__(self, settings: Settings):
        self.s = settings

    def send(self, msg: EmailMessage) -> None:
        host, port = self.s.mail_smtp_host, self.s.mail_smtp_port
        context = ssl.create_default_context()
        if port == 587:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.starttls(context=context)
                smtp.login(self.s.mail_address, self.s.mail_auth_code)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
                smtp.login(self.s.mail_address, self.s.mail_auth_code)
                smtp.send_message(msg)

    async def send_async(self, msg: EmailMessage) -> None:
        """在线程池里发信，避免阻塞事件循环。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.send, msg)


class MailFetcher:
    """IMAP 收信：读取收件箱里本服务发出的邮件，把删除/星标动作转成反馈。"""

    def __init__(self, settings: Settings):
        self.s = settings
        self._scan_lock = threading.Lock()

    def _connect(self) -> imaplib.IMAP4_SSL:
        m = imaplib.IMAP4_SSL(self.s.mail_imap_host, self.s.mail_imap_port, timeout=30)
        try:
            m.login(self.s.mail_address, self.s.mail_auth_code)
        except BaseException:
            try:
                m.shutdown()
            finally:
                raise
        return m

    @staticmethod
    def _block_id(header: bytes) -> int | None:
        try:
            parsed = email.message_from_bytes(header)
        except Exception:  # noqa: BLE001
            return None
        raw = parsed.get(BLOCK_HEADER)
        if not raw:
            return None
        try:
            return int(str(raw).strip())
        except ValueError:
            return None

    def _parse_folders(self, data: list) -> list[str]:
        names: list[str] = []
        for line in data or []:
            s = line.decode("utf-8", "replace") if isinstance(line, (bytes, bytearray)) else str(line)
            if "\\Noselect" in s:
                continue
            m = _FOLDER_RE.search(s.strip())
            if m:
                names.append(m.group(1))
            elif s.strip():
                names.append(s.rsplit(' ',1)[-1])
        return names

    def locate_messages(self) -> dict[str, tuple[str, bool, int | None, int | None]]:
        with self._scan_lock:
            return self._locate_messages()

    def _locate_messages(self) -> dict[str, tuple[str, bool, int | None, int | None]]:
        """Incremental UID discovery + bounded rotating flag/move reconciliation.

        Every known UID is revisited, including old mail; no last-500 cutoff.
        A UIDVALIDITY change invalidates ONLY that folder's cache and starts a paged rebuild.
        """
        db = self.db
        with self._connect() as m:
            typ, data = m.list()
            if typ != 'OK':
                raise RuntimeError('IMAP LIST failed')
            for name in self._parse_folders(data):
                decoded = decode_folder(name)
                kind = _folder_kind(decoded, self.s.uninterested_folder)
                if kind == 'skip':
                    continue
                typ, _ = m.select('"' + name.replace('\\','\\\\').replace('"','\\"') + '"', readonly=True)
                if typ != 'OK':
                    continue
                _, validity_data = m.response('UIDVALIDITY')
                validity = (validity_data[0] or b'').decode('ascii') if validity_data else ''
                if not validity:
                    # Container folders (e.g. QQ "其他文件夹") select OK but have no
                    # UIDVALIDITY; they hold no messages and must not abort the scan.
                    logger.debug("IMAP folder without UIDVALIDITY, skipped: %s", decoded)
                    continue
                state = db._query('SELECT * FROM imap_folders WHERE name=?',(name,))
                if not state or state[0]['uidvalidity'] != validity:
                    with db._lock, db._conn:
                        db._conn.execute('DELETE FROM imap_messages WHERE folder=?',(name,))
                        db._conn.execute('INSERT OR REPLACE INTO imap_folders VALUES(?,?,0,0)',(name,validity))
                    high = sweep = 0
                else:
                    high, sweep = state[0]['high_uid'], state[0]['sweep_uid']
                typ, res = m.uid('search',None,'UID',f'{high+1}:*')
                if typ != 'OK':
                    continue
                new_ids = sorted({int(x) for x in (res[0] or b'').split() if int(x)>high})[:250]
                known = db._query('SELECT uid FROM imap_messages WHERE folder=? AND uid>? ORDER BY uid LIMIT 250',(name,sweep))
                if not known:
                    sweep = 0
                    known = db._query('SELECT uid FROM imap_messages WHERE folder=? ORDER BY uid LIMIT 250',(name,))
                known_ids = [r['uid'] for r in known]
                ids = sorted(set(new_ids+known_ids))
                fetched_ids = set()
                if ids:
                    typ, fetched = m.uid('fetch',','.join(map(str,ids)),
                        '(UID FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID X-INFOHUB-EVENT X-INFOHUB-VERSION X-INFOHUB-BLOCK X-INFOHUB-READONLY)])')
                    if typ != 'OK':
                        continue
                    with db._lock, db._conn:
                        for part in fetched or []:
                            if not isinstance(part,tuple):
                                continue
                            meta, header = part
                            match = re.search(rb'UID (\d+)',meta,re.I)
                            if not match:
                                continue
                            uid = int(match[1]); fetched_ids.add(uid)
                            parsed = email.message_from_bytes(header)
                            mid = str(parsed.get('Message-ID') or '').strip()
                            eid = parsed.get('X-InfoHub-Event')
                            ver = parsed.get('X-InfoHub-Version')
                            if not mid and parsed.get(BLOCK_HEADER):
                                mid = 'legacy-block:'+str(parsed[BLOCK_HEADER])
                            if not mid:
                                continue
                            if parsed.get('X-InfoHub-ReadOnly'):
                                eid = ver = None
                            db._conn.execute('INSERT OR REPLACE INTO imap_messages VALUES(?,?,?,?,?,?,?,?)',
                                (name,validity,uid,mid,int(eid) if eid and eid.isdigit() else None,
                                 int(ver) if ver and ver.isdigit() else None,int(bool(_FLAG_RE.search(meta))),kind))
                        for uid in set(known_ids)-fetched_ids:
                            db._conn.execute('DELETE FROM imap_messages WHERE folder=? AND uidvalidity=? AND uid=?',(name,validity,uid))
                db._exec('UPDATE imap_folders SET high_uid=?,sweep_uid=? WHERE name=?',
                         (max([high]+new_ids),max(known_ids) if known_ids else 0,name))
        out = {}
        priority = {'inbox':6,'uninterested':5,'archive':4,'other':3,'trash':2,'junk':1}
        for row in db._query('SELECT * FROM imap_messages'):
            mid = row['message_id']
            old = out.get(mid)
            if old and priority.get(old[0],0)>priority.get(row['kind'],0):
                continue
            out[mid] = (row['kind'],bool(row['flagged']),row['event_id'],row['version'])
        return out

    def locate(self):
        return {int(mid.split(':')[1]):(v[0],v[1]) for mid,v in self.locate_messages().items() if mid.startswith('legacy-block:')}


class MailChannel:
    """装配发信 + 收信，并在后台线程里周期轮询反馈。"""

    def __init__(self, settings: Settings, db: Database):
        self.s = settings
        self.db = db
        self.sender = MailSender(settings)
        self.fetcher = MailFetcher(settings)
        self.fetcher.db = db
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._inflight = set()

    @property
    def ready(self) -> bool:
        return bool(self.s.mail_enabled and self.s.mail_address and self.s.mail_auth_code and not self.s.dry_run and self.s.live_delivery_enabled)

    def _deliver_sync(self, messages: list[EmailMessage]) -> int:
        """发信/投递：默认 IMAP APPEND（不耗发信配额）；`MAIL_SEND_MODE=smtp` 时走 SMTP。"""
        from info_hub.outbox import DeliveryError
        if not self.ready:
            raise DeliveryError('disabled')
        count = 0
        for msg in messages:
            if self.s.mail_send_mode == 'smtp':
                try:
                    self.sender.send(msg)
                except Exception:
                    raise DeliveryError('smtp_write', unknown=True) from None
            else:
                try:
                    m = self.fetcher._connect()
                except Exception:
                    raise DeliveryError('connect',unknown=False) from None
                try:
                    try:
                        typ, resp = m.append('INBOX',None,imaplib.Time2Internaldate(time.time()),msg.as_bytes())
                    except Exception:
                        raise DeliveryError('append_write',unknown=True) from None
                    if typ != 'OK':
                        raise DeliveryError('append_rejected',unknown=False)
                finally:
                    try:
                        m.logout()
                    except Exception:
                        pass  # logout failure must not erase an acknowledged APPEND
            count += 1
        return count

    async def deliver(self, messages: list[EmailMessage]) -> int:
        """一次连接批量投递（APPEND/SMTP 均在线程池里跑，不阻塞事件循环）。"""
        if not messages:
            return 0
        task = asyncio.create_task(asyncio.to_thread(self._deliver_sync, list(messages)))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return await asyncio.shield(task)

    async def send_message(self, *args, **kwargs) -> None:
        """单封投递（兼容/测试用）。"""
        await self.deliver([build_message(self.s, *args, **kwargs)])

    def poll_once(self) -> list[MailAction]:
        if not self.ready:
            return []
        try:
            located = self.fetcher.locate_messages()
        except Exception as exc:
            logger.warning('IMAP poll failed: %s',type(exc).__name__)
            return []
        from info_hub.feedback import apply_observations
        return apply_observations(self.db,located)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.s.mail_poll_seconds)

    async def start(self) -> None:
        if not self.ready:
            logger.info("邮件通道未启用（需 MAIL_ENABLED、LIVE_DELIVERY_ENABLED、DRY_RUN=false 同时就绪），跳过")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="info-hub-mail", daemon=True)
        self._thread.start()
        logger.info("邮件通道已启动（账号隐藏，轮询 %ds）", self.s.mail_poll_seconds)

    async def stop(self) -> None:
        self._stop.set()
        if self._inflight:
            await asyncio.gather(*list(self._inflight),return_exceptions=True)
        if self._thread:
            await asyncio.to_thread(self._thread.join, 45)
            if self._thread.is_alive():
                raise RuntimeError('IMAP worker still stopping; database remains open')
            self._thread = None


_channel: MailChannel | None = None


def get_mailer() -> MailChannel:
    global _channel
    if _channel is None:
        from info_hub.db import get_db
        from info_hub.settings import get_settings

        _channel = MailChannel(get_settings(), get_db())
    return _channel
