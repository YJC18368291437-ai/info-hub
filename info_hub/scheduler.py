"""Asia/Shanghai 时段补跑。邮件按事项路由；旧 Telegram 调度仅 TG_ENABLED 时注册，成功后记游标。"""
from __future__ import annotations

import datetime as _dt
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from info_hub.classify import Classifier
from info_hub.db import Database
from info_hub.mail_dispatch import MailDispatcher
from info_hub.newspaper import NewspaperDispatcher
from info_hub.settings import Settings
from info_hub.telegram_push import TelegramPusher

logger = logging.getLogger("info_hub.scheduler")

# (话题key, 标题, 领域名, 是否小白讲解)
SPECS = (
    ("chat", "📋 闲聊简报", "闲聊", False),
    ("cs", "💻 计算机简报", "计算机/AI/技术", True),
    ("bio", "🧬 生物科学简报", "生物科学", False),
)

DEFAULT_WINDOW = 4 * 3600  # 首次运行回看 4 小时


class SummaryScheduler:
    def __init__(self, settings: Settings, db: Database,
                 classifier: Classifier, pusher: TelegramPusher):
        self.s = settings
        self.db = db
        self.classifier = classifier
        self.pusher = pusher
        from info_hub.mailer import get_mailer

        self.mail_dispatch = MailDispatcher(settings, db, get_mailer())
        self.newspaper = NewspaperDispatcher(settings, db, classifier, get_mailer())
        self.sched = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._active = set()

    def start(self) -> None:
        if self.sched.running:
            return
        self.sched.add_job(self.pump,'interval',seconds=30,id='mail_pump',
                           replace_existing=True,coalesce=True,max_instances=1,misfire_grace_time=120)
        if self.s.tg_enabled:
            hours = ','.join(map(str,self.s.summary_hours))
            self.sched.add_job(self._run,CronTrigger(hour=hours,minute=0,timezone='Asia/Shanghai'),
                               id='legacy_digests',replace_existing=True,coalesce=True,max_instances=1)
        self.sched.start()

    def stop(self) -> None:
        for task in list(self._active):
            task.cancel()
        if self.sched.running:
            self.sched.shutdown(wait=False)

    async def wait_stopped(self):
        if self._active:
            await asyncio.gather(*list(self._active),return_exceptions=True)
        await asyncio.sleep(0)

    async def pump(self):
        task=asyncio.current_task()
        self._active.add(task)
        try:
            await self._pump()
        finally:
            self._active.discard(task)

    async def _pump(self):
        if not self.mail_dispatch.ready:
            return
        from info_hub.events import TZ
        from info_hub.outbox import Outbox
        from info_hub.scheduling import latest_slots
        use_newspaper = (getattr(self.s, "newspaper_enabled", False)
                         and not getattr(self.s, "newspaper_legacy_events", True))
        if use_newspaper:
            await self.newspaper.tick()
        else:
            now = _dt.datetime.now(TZ)
            daily, weekly = latest_slots(now,self.s.summary_hours,self.s.mail_weekly_dow,self.s.mail_weekly_hour)
            await self.mail_dispatch.immediate()
            # Revisit the latest due slot on restart/misfire; per-event delivery keys provide idempotency.
            # Keep scanning this slot for retries and newly classified backlog; no cursor hides a failure.
            await self.mail_dispatch.daily(cutoff=int(daily.timestamp()))
            self.db.kv_set('schedule:last_daily_slot',daily.isoformat())
            await self.mail_dispatch.weekly(cutoff=int(weekly.timestamp()))
            # Slot is observational; delivery-event keys remain the authority. Late classification
            # and chunked backlog can continue within the same slot without repeating sent items.
            self.db.kv_set('schedule:last_weekly_slot',weekly.isoformat())
        await Outbox(self.db,self.mail_dispatch.mailer).reconcile()
        if not use_newspaper:
            await self.mail_dispatch.todo()

    async def _run(self) -> None:
        if not self.s.tg_enabled:
            return
        now = int(_dt.datetime.now().timestamp())
        for group_id in list(self.s.groups.keys()):
            for topic, title, domain, explain in SPECS:
                try:
                    await self._digest(str(group_id), topic, title, domain, explain, now)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("群 %s/%s 简报失败: %s", group_id, topic, exc)
        await self._flush_batches()

    async def _flush_batches(self) -> None:
        """非实时的原文类（通知/链接/文件）随定时批量投递。"""
        blocks = self.db.pending_topics(("notice", "link", "file"))
        for group in self._merge_adjacent(blocks):
            try:
                result = await self.pusher.push_blocks(group)
                if result is not None and not self.s.dry_run:
                    for b in group:
                        self.db.mark_block_pushed(int(b["id"]))
            except Exception as exc:  # noqa: BLE001
                logger.warning("投递批量块失败: %s", exc)

    def _merge_adjacent(self, blocks: list) -> list[list]:
        win = self.s.block_merge_window_seconds
        groups: list[list] = []
        cur: list = []
        for b in blocks:
            if cur:
                prev = cur[-1]
                same = (
                    str(b["group_id"]) == str(prev["group_id"])
                    and b["topic"] == prev["topic"]
                    and int(b["start_ts"] or 0) - int(prev["end_ts"] or 0) <= win
                )
                if same:
                    cur.append(b)
                    continue
                groups.append(cur)
                cur = []
            cur = [b]
        if cur:
            groups.append(cur)
        return groups

    async def _digest(self, group_id: str, topic: str, title: str, domain: str,
                      explain: bool, now_ts: int) -> None:
        cursor_key = f"cursor:{topic}:{group_id}"
        since = int(self.db.kv_get(cursor_key, "0") or "0")
        if since <= 0:
            since = now_ts - DEFAULT_WINDOW
        blocks = self.db.blocks_in_window(group_id, topic, since, now_ts + 1)
        if not blocks:
            return
        quotes = self._anonymize(blocks)  # 人名 -> a/b/c/d
        name = self.s.group_name(group_id)
        if topic == "chat":
            data = await self.classifier.summarize(name, quotes)
        else:
            data = await self.classifier.digest(name, domain, quotes, explain=explain)
        if not data:
            return
        when = _dt.datetime.now().strftime("%H:%M")
        title_line = f"{title} · {name} · {when}"
        html = self._render(title_line, data)
        chat_id, thread = self.s.resolve_target(topic)
        did = self.db.add_digest(group_id, topic, quotes, html)
        if self.s.dry_run or self.pusher.app is None:
            logger.info("[DRY_RUN] 简报 -> %s\n%s", self.s.topic_label(topic), html)
            return
        if not chat_id:
            return
        await self.pusher.app.bot.send_message(
            chat_id=chat_id, message_thread_id=thread,
            text=html, parse_mode="HTML", reply_markup=self.pusher.digest_buttons(did),
        )
        self.db.kv_set(cursor_key, str(now_ts))

    @staticmethod
    def _anonymize(blocks: list) -> str:
        """把"昵称: 正文"里的昵称换成 a/b/c/d（按首次出现顺序）。"""
        from info_hub.textfmt import anonymize

        return anonymize([b["text"] or "" for b in blocks])

    @staticmethod
    def _render(title_line: str, data: dict) -> str:
        import html as _h

        out = [f"<b>{_h.escape(title_line)}</b>"]
        tldr = str(data.get("tldr") or "").strip()
        if tldr:
            out.append(_h.escape(tldr))
        detail: list[str] = []
        topics = data.get("topics")
        if isinstance(topics, list) and topics:
            for i, t in enumerate(topics, 1):
                if not isinstance(t, dict):
                    continue
                nm = str(t.get("name") or "").strip()
                if detail:
                    detail.append("")
                detail.append(f"<b>{i}. {_h.escape(nm)}</b>")
                for p in (t.get("points") or []):
                    p = str(p).strip()
                    if p:
                        detail.append("· " + _h.escape(p))
        else:
            for p in (data.get("points") or []):
                p = str(p).strip()
                if p:
                    detail.append("· " + _h.escape(p))
        if detail:
            out.append("<blockquote expandable>" + "\n".join(detail) + "</blockquote>")
        return "\n".join(out)
