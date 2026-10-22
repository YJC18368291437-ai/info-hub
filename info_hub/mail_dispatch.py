"""邮件编排：事项初次/更新/24h提醒、只读周报与待办快照。唯一投递账本见 outbox.py；mail_pushed 仅兼容历史。固定7天初始下界保留此前用户修复。"""
from __future__ import annotations

import datetime as _dt
import html as _h
import json
import logging
import time
from email.message import EmailMessage

import aiohttp

from info_hub.db import Database
from info_hub.mailer import MailChannel, build_message
from info_hub.rules import get_rules, source_header
from info_hub.settings import Settings
from info_hub.textfmt import anonymize, anonymize_blocks

logger = logging.getLogger("info_hub.mail.dispatch")

DAILY_TOPICS = ("notice", "file", "link")
WEEKLY_TOPICS = ("cs", "bio")
BACKFILL_DAYS = 7
BACKFILL_FLOOR_KEY = "mail:backfill_floor"
IMG_MAX_BYTES = 10 * 1024 * 1024
SYSTEM_EVENT_PREFIX = "[群事件待确认]"
CHAT_MARKERS = ("啥时候", "什么时候", "几点", "哪里", "在哪", "怎么", "为什么",
                "可以", "不用", "没有", "好的", "谢谢", "?", "？")

_STICKER_MARKERS = ("gxh.vip.qq.com", "/club/item/", "qzonestyle.gtimg.cn", "qzone/em")


def _is_sticker(url: object) -> bool:
    u = str(url)
    return any(m in u for m in _STICKER_MARKERS)


def _when(ts: object) -> str:
    try:
        t = int(ts or 0)
    except (TypeError, ValueError):
        t = 0
    if not t:
        return _dt.datetime.now().strftime("%m-%d %H:%M")
    return _dt.datetime.fromtimestamp(t).strftime("%m-%d %H:%M")


class MailDispatcher:
    def __init__(self, settings: Settings, db: Database, mailer: MailChannel):
        self.s = settings
        self.db = db
        self.mailer = mailer
        self._http: aiohttp.ClientSession | None = None

    @property
    def ready(self) -> bool:
        return self.s.mail_enabled and self.mailer.ready

    # ---------- 图片下载 ----------
    async def _download(self, url: str) -> bytes:
        from info_hub.safe_download import download
        return await download(url, kind='image')


    # ---------- 回看下界 / 计数 ----------
    def _backfill_floor(self) -> int:
        """返回可投递块的最小 `created_ts`（首次运行起固定，永不前移）。

        旧实现按话题维护 `mail:cursor:<topic>` 并在每次扫描前推进到 `now`，
        于是"尚未判类 / 投递失败 / 第 1000 封之后"的块会被游标永久跳过。
        现在改为一次性定格的下界：首次调用时写入 `now - 7 天`，后续每次返回同一值。
        真正的"已发"去重完全交给 `blocks.mail_pushed`，因此这些待处理块会一直
        保持可投递，直到成功发出。

        迁移：历史遗留的 `mail:cursor:*` 键不再读取（保持原样、无害）。它们在旧代码里
        总是被推进到"上次运行时刻"，比新下界（首次运行 - 7 天）更晚、更窄；忽略它们
        只会让最近 7 天内的待处理块重新入选，不会重复发送已 `mail_pushed=1` 的块。
        """
        raw = self.db.kv_get(BACKFILL_FLOOR_KEY, "")
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
        floor = int(time.time()) - BACKFILL_DAYS * 86400
        self.db._exec('INSERT OR IGNORE INTO kv(key,value) VALUES(?,?)',(BACKFILL_FLOOR_KEY,str(floor)))
        return int(self.db.kv_get(BACKFILL_FLOOR_KEY))

    def _sent_today(self) -> int:
        today = _dt.datetime.now().strftime("%Y%m%d")
        try:
            return int(self.db.kv_get(f"mail:sent:{today}", "0") or "0")
        except ValueError:
            return 0

    def _set_sent_today(self, n: int) -> None:
        today = _dt.datetime.now().strftime("%Y%m%d")
        self.db.kv_set(f"mail:sent:{today}", str(int(n)))

    # ---------- 渲染 ----------
    @staticmethod
    def _title(b) -> str:
        t = (b["title"] or "").strip()
        if t:
            return t
        for line in (b["text"] or "").split("\n"):
            line = line.strip()
            if line:
                return line[:40]
        return "（无标题）"

    @staticmethod
    def _files(b) -> list[dict]:
        try:
            fs = json.loads(b["files"] or "[]")
        except (KeyError, TypeError, ValueError):
            fs = []
        return [f for f in fs if f]

    @staticmethod
    def _images(b) -> list[str]:
        try:
            imgs = json.loads(b["images"] or "[]")
        except (KeyError, TypeError, ValueError):
            imgs = []
        return [str(u) for u in imgs if u]

    @staticmethod
    def _html(text: str) -> str:
        return "<div>" + _h.escape(text).replace("\n", "<br>") + "</div>"

    # ---------- 学校通知过滤 ----------
    def _school_notice_block(self, b) -> bool:
        groups = getattr(self.s, 'mail_source_groups', ()) or ()
        topics = getattr(self.s, 'mail_topics', ()) or ()
        gid = str(b["group_id"])
        topic = str(b["topic"] or "")
        if groups and gid not in groups:
            return False
        if topics and topic not in topics:
            return False
        text = (b["text"] or "").strip()
        if text.lstrip(":： \t").startswith(SYSTEM_EVENT_PREFIX):
            return False
        media = bool(self._images(b) or self._files(b))
        if topic == "needs_review":
            if not text:
                return media
            if len(text) < 80 and any(m in text for m in CHAT_MARKERS):
                return False
            return media or get_rules().has_school_notice(text)
        return bool(text or media)

    def _event_allowed(self, e, kind: str = "initial") -> bool:
        rows = self.db._query(
            "SELECT b.group_id,b.topic,b.text,b.images,b.files,b.mail_pushed "
            "FROM event_sources s JOIN blocks b ON b.id=s.block_id WHERE s.event_id=?",
            (e["id"],))
        allowed = [b for b in rows if self._school_notice_block(b)]
        if not allowed:
            return False
        # A one-off backfill already covers these blocks; do not resend the initial mail.
        if kind == "initial" and e["version"] == 1 and all(b["mail_pushed"] for b in allowed):
            return False
        return True

    async def _attachments(self, b) -> list[tuple[str, bytes, str]]:
        atts: list[tuple[str, bytes, str]] = []
        for i, url in enumerate(self._images(b)):
            if _is_sticker(url):
                continue
            try:
                data = await self._download(url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("通知图片下载失败: %s", type(exc).__name__)
                continue
            if len(data) > IMG_MAX_BYTES:
                logger.warning("通知图片过大跳过（>10MB）")
                continue
            ext = ".png" if url.lower().endswith(".png") else ".jpg"
            atts.append((f"image{i + 1}{ext}", data, "image/png" if ext == ".png" else "image/jpeg"))
        return atts

    def _block_body(self, b, anon_text: str) -> str:
        gid = str(b["group_id"])
        lines = [source_header(self.s.group_name(gid), "", int(b["end_ts"] or 0))]
        if anon_text.strip():
            lines.append(anon_text)
        for f in self._files(b):
            lines.append(f"📎 {f.get('name') or '文件'}: {f.get('url') or ''}")
        return "\n\n".join(x for x in lines if x)

    async def _build_block_message(self, b) -> EmailMessage:
        topic = str(b["topic"])
        anon = anonymize([b["text"] or ""])
        body = self._block_body(b, anon)
        subject = f"info-hub · {self.s.topic_label(topic)} · {self._title(b)}"
        atts = await self._attachments(b) if topic == "notice" else []
        return build_message(
            self.s, subject=subject, text=body, html=self._html(body),
            block_id=int(b["id"]), attachments=atts,
        )

    def _build_summary_message(self, blocks: list) -> EmailMessage:
        anon = anonymize_blocks([b["text"] or "" for b in blocks])
        lines: list[str] = []
        for b, a in zip(blocks, anon):
            lines.append(f"【{self.s.topic_label(str(b['topic']))}】"
                         f"{self.s.group_name(str(b['group_id']))} · {_when(b['end_ts'])}")
            if (b["title"] or "").strip():
                lines.append(f"· {b['title'].strip()}")
            if a.strip():
                lines.append(a)
            lines.append("")
        body = f"（当日超出上限，{len(blocks)} 条并入本封汇总）\n\n" + "\n".join(lines)
        return build_message(self.s, subject=f"info-hub · 汇总 · {_dt.datetime.now().strftime('%m-%d')}",
                             text=body, html=self._html(body))

    # ---------- 投递 ----------
    def _events(self):
        from info_hub.events import EventStore
        from info_hub.schedule import inspect_schedule
        courses = {}
        path = getattr(self.s, 'base_dir', None)
        if path:
            schedule = path / 'docs' / 'schedule-import.json'
            if schedule.exists():
                courses = inspect_schedule(schedule)['course_groups']
        # Explicit critical course groups supplement the export; no inferred timetable.
        for gid, group in getattr(self.s, 'groups', {}).items():
            from info_hub.sources import is_course_source
            if is_course_source(self.s,gid):
                courses[gid] = group.get('name', gid)
        store = EventStore(self.db)
        store.import_blocks(self._backfill_floor(), courses)
        return store

    def _event_message(self, e, kind):
        p = json.loads(e['payload'])
        label = '待确认 · ' if e['category'] == 'needs_review' or e['confidence'] < 0.8 else ''
        lines = [f"事项 E{e['id']} / v{e['version']} · {e['status']}",
                 '旗标=待我处理；取消旗标并归档=关闭待办；仅专用文件夹表示不感兴趣。']
        if kind == 'reminder24h':
            lines.append('截止前提醒（重启补跑可能不足24小时），请核对原文时间。')
        for k, name in [('deadline_raw','截止'),('start_raw','开始'),('location','地点'),
                        ('eligibility','资格'),('cost','费用'),('travel','出行'),('skills','技能'),('registration','报名')]:
            lines.append(f"{name}：{p.get(k) or '未注明/待确认'}")
        if p.get('deadline_raw') and p.get('deadline_ts') is None:
            lines.append('截止不明确：未安排精确提醒。')
        changes = self.db._query('SELECT changes FROM event_versions WHERE event_id=? AND version=?',(e['id'],e['version']))
        if changes and e['version'] > 1:
            lines.append('关键变化（原 → 新）：' + json.dumps(json.loads(changes[0]['changes']),ensure_ascii=False))
        lines.extend(['来源：' + ', '.join(str(x['block_id']) for x in self.db._query('SELECT block_id FROM event_sources WHERE event_id=?',(e['id'],))),
                      anonymize([p.get('text') or '[无文字媒体，请查看原群]'])])
        for url in p.get('images',[]):
            lines.append('图片（外部原链）：' + url)
        for f in p.get('files',[]):
            lines.append('文件：' + str(f.get('name','文件')) + ' ' + str(f.get('url','')))
        body = '\n\n'.join(lines)
        msg = build_message(self.s,subject=f"info-hub · {label}E{e['id']} · {p['title'][:90]}",text=body,html=self._html(body))
        msg['X-InfoHub-Event'] = str(e['id'])
        msg['X-InfoHub-Version'] = str(e['version'])
        return msg

    async def _dispatch(self, mode, cutoff=None):
        from info_hub.outbox import Outbox
        if not self.ready:
            return 0
        store = self._events()
        outbox = Outbox(self.db,self.mailer)
        now = int(time.time())
        n = 0
        after = 0
        budget = 1000
        # ID keyset pages do not favor a fixed topic/group. Unsent rows persist.
        while budget > 0:
            rows = self.db._query('SELECT * FROM events WHERE id>? ORDER BY id LIMIT 250',(after,))
            if not rows:
                break
            for e in rows:
                after = e['id']
                if cutoff is not None and e['updated_ts'] > cutoff:
                    continue
                for kind in store.due(e,now,mode):
                    if not self._event_allowed(e,kind):
                        continue
                    if budget <= 0:
                        break
                    if self.s.mail_daily_limit > 0 and not e['mandatory']:
                        start = int(_dt.datetime.fromtimestamp(now, self._tz()).replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
                        count = self.db._query("SELECT COUNT(*) FROM deliveries WHERE state='sent' AND sent_ts>=?",(start,))[0][0]
                        if count >= self.s.mail_daily_limit:
                            continue  # preserve per-event mapping; no ambiguous overflow digest
                    key = f"event:{e['id']}:v{e['version']}:{kind}"
                    old = self.db._query('SELECT state,next_retry FROM deliveries WHERE key=?',(key,))
                    if old and (old[0]['state'] not in ('queued','retryable') or old[0]['next_retry']>now):
                        continue
                    outbox.queue(key,self._event_message(e,kind),[(e['id'],e['version'])],kind)
                    n += await outbox.send(key)
                    budget -= 1
        logger.info('邮件扫描 mode=%s sent=%d backlog=%s',mode,n,self.db.backlog())
        return n

    @staticmethod
    def _tz():
        from zoneinfo import ZoneInfo
        return ZoneInfo('Asia/Shanghai')

    async def immediate(self):
        return await self._dispatch('immediate')

    async def daily(self, cutoff=None):
        return await self._dispatch('daily',cutoff)

    async def weekly(self, cutoff=None):
        from info_hub.events import digest
        from info_hub.outbox import Outbox
        if not self.ready:
            return 0
        if not getattr(self.s, 'mail_weekly_enabled', True):
            return 0
        store = self._events()
        now = int(time.time())
        rows = self.db._query("SELECT * FROM events WHERE category IN ('knowledge','exploration') ORDER BY id")
        selected = []
        exploration = []
        week = _dt.datetime.fromtimestamp(cutoff if cutoff is not None else now,self._tz()).strftime('%G-W%V')
        box = Outbox(self.db,self.mailer)
        pending = self.db._query("SELECT key,state FROM deliveries WHERE kind='weekly' AND key LIKE ? AND state IN ('queued','retryable','claimed','unknown')",('weekly:'+week+':%',))
        if pending:
            return sum([await box.send(r['key']) for r in pending])
        used = self.db._query("""SELECT COUNT(*) FROM delivery_events d JOIN deliveries m ON m.key=d.delivery_key
            JOIN events e ON e.id=d.event_id WHERE m.kind='weekly' AND m.key LIKE ? AND m.state='sent'
            AND e.category='exploration'""",('weekly:'+week+':%',))[0][0]
        for e in rows:
            if cutoff is not None and e['updated_ts'] > cutoff:
                continue
            if not store.due(e,now,'weekly'):
                continue
            if not self._event_allowed(e,'weekly'):
                continue
            key = f"event:{e['id']}:v{e['version']}:initial"
            if self.db._query("SELECT 1 FROM delivery_events d JOIN deliveries m ON d.delivery_key=m.key WHERE d.event_id=? AND d.version=? AND m.state IN ('sent','unknown','claimed')",(e['id'],e['version'])):
                continue
            if e['category'] == 'exploration':
                p = json.loads(e['payload'])
                if len(exploration)+used >= 5 or (p.get('deadline_ts') and p['deadline_ts'] <= now):
                    continue
                exploration.append(e)
            else:
                selected.append(e)
        selected = selected[:50]  # bounded digest; the next pump continues remaining knowledge
        selected.extend(exploration)
        if not selected:
            return 0
        # Read-only digest: individual operations use the explicit offline E-ID command.
        lines = ['本周知识清单（只读汇总：整封旗标/归档/删除不映射条目）',
                 '逐条处理：运行 infohub_admin.py status E<编号> pending|completed|declined|registered；',
                 '不感兴趣/撤销：infohub_admin.py preference E<编号> off|on。',
                 f'探索 {len(exploration)} 条（本周目标3–5；不足不凑数；仅来自已采集QQ群，未做外网搜索）。']
        for e in selected:
            p = json.loads(e['payload'])
            lines += [f"E{e['id']} / v{e['version']} · {p['title']}",anonymize([p['text']]),p.get('reason','')]
        body = '\n\n'.join(lines)
        msg = build_message(self.s,subject='info-hub · 周报 · '+week,text=body,html=self._html(body))
        msg['X-InfoHub-ReadOnly'] = 'true'
        msg['X-InfoHub-Items'] = ','.join(str(e['id']) for e in selected)
        key = 'weekly:'+week+':'+digest([(e['id'],e['version']) for e in selected])
        box = Outbox(self.db,self.mailer)
        box.queue(key,msg,[(e['id'],e['version']) for e in selected],kind='weekly')
        n = await box.send(key)
        if n:
            self.db.kv_set('exploration:'+week,str(used+len(exploration)))
        return n

    def _build_school_summary(self, blocks: list) -> EmailMessage:
        anon = anonymize_blocks([b["text"] or "" for b in blocks])
        first = _dt.datetime.fromtimestamp(int(blocks[0]["created_ts"] or 0), self._tz())
        last = _dt.datetime.fromtimestamp(int(blocks[-1]["created_ts"] or 0), self._tz())
        span = first.strftime("%m-%d") if first.date() == last.date() else f"{first:%m-%d}~{last:%m-%d}"
        lines = [f"学校通知补推 · {len(blocks)} 条 · {span}", ""]
        for b, a in zip(blocks, anon):
            gid = str(b["group_id"])
            lines.append(f"【{self.s.topic_label(str(b['topic']))}】"
                         f"{self.s.group_name(gid)} · {_when(b['end_ts'])}")
            if (b["title"] or "").strip():
                lines.append(f"· {b['title'].strip()}")
            if a.strip():
                lines.append(a)
            for f in self._files(b):
                lines.append(f"📎 {f.get('name') or '文件'}: {f.get('url') or ''}")
            for url in self._images(b):
                if not _is_sticker(url):
                    lines.append(f"📷 {url}")
            lines.append("")
        body = "\n".join(lines)
        msg = build_message(self.s, subject=f"info-hub · 学校通知补推 · {span}",
                            text=body, html=self._html(body))
        msg['X-InfoHub-ReadOnly'] = 'true'
        return msg

    async def school_backlog(self, since_ts: int) -> int:
        """One summary envelope for pending school notices; idempotent by block-ID digest."""
        from info_hub.events import digest
        from info_hub.outbox import Outbox
        if not self.ready:
            return 0
        rows = [dict(r) for r in self.db._query(
            "SELECT * FROM blocks WHERE mail_pushed=0 AND created_ts>=? AND topic IS NOT NULL "
            "ORDER BY created_ts,id", (int(since_ts),))]
        rows = [b for b in rows if self._school_notice_block(b)]
        if not rows:
            logger.info("学校通知补推：无待发")
            return 0
        msg = self._build_school_summary(rows)
        key = "backlog:school:" + digest([int(b["id"]) for b in rows])
        box = Outbox(self.db, self.mailer)
        box.queue(key, msg, [], kind="backlog")
        n = await box.send(key)
        if n:
            for b in rows:
                self.db._exec("UPDATE blocks SET mail_pushed=1 WHERE id=?", (b["id"],))
            logger.info("学校通知补推：已发送 %d 条", len(rows))
        return n

    async def close(self):
        if self._http and not self._http.closed:
            await self._http.close()

    async def todo(self):
        """A read-only mailbox snapshot of explicitly flagged/registered events."""
        from info_hub.events import digest
        from info_hub.outbox import Outbox
        if not self.ready:
            return 0
        rows = self.db._query("SELECT * FROM events WHERE status IN ('pending','registered') ORDER BY id")
        rows = [e for e in rows if self._event_allowed(e,'todo')]
        state = [(e['id'],e['version'],e['status']) for e in rows]
        signature = digest(state)
        previous = self.db.kv_get('todo:signature')
        if signature == previous or (not rows and not previous):
            return 0
        key = 'todo:'+digest([self.db.kv_get('todo:last_key'),signature])
        lines = ['待我处理（只读快照；旗标单条事项邮件来更新，整封清单操作无反馈语义）']
        now = int(time.time())
        for e in rows:
            p = json.loads(e['payload'])
            deadline = p.get('deadline_ts')
            reminder = '未安排'
            if deadline and deadline > now and deadline-e['first_seen']>=86400:
                reminder = _dt.datetime.fromtimestamp(max(now,deadline-86400),self._tz()).isoformat()
                if self.db._query("SELECT 1 FROM deliveries WHERE event_id=? AND version=? AND kind='reminder24h' AND state='sent'",(e['id'],e['version'])):
                    reminder = '已提醒'
            lines.append(f"E{e['id']} / v{e['version']} · {e['status']} · {p['title']}\n截止：{p.get('deadline_raw') or '未明确'}；下一提醒：{reminder}")
        if not rows:
            lines.append('当前无开放待办。')
        body = '\n\n'.join(lines)
        msg = build_message(self.s,subject='info-hub · 待我处理',text=body,html=self._html(body))
        msg['X-InfoHub-ReadOnly'] = 'true'
        box=Outbox(self.db,self.mailer); box.queue(key,msg,kind='todo')
        n=await box.send(key)
        if n:
            self.db.kv_set('todo:signature',signature)
            self.db.kv_set('todo:last_key',key)
        return n
