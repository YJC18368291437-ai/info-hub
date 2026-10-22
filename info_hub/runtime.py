"""运行时装配：单例组件 + 后台流水线循环 + 启动/关闭钩子。"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from info_hub.classify import Classifier
from info_hub.db import get_db
from info_hub.health import write_heartbeat
from info_hub.mailer import get_mailer
from info_hub.pipeline import Pipeline
from info_hub.rules import get_rules
from info_hub.scheduler import SummaryScheduler
from info_hub.settings import get_settings
from info_hub.telegram_push import get_pusher

logger = logging.getLogger("info_hub.runtime")

_pipeline: Pipeline | None = None
_scheduler: SummaryScheduler | None = None
_task: asyncio.Task | None = None
_title_task: asyncio.Task | None = None
_heartbeat_task: asyncio.Task | None = None
_heartbeat_last_ok = time.time()
_HEARTBEAT = Path(__file__).resolve().parents[1] / 'data' / 'heartbeat.json'


async def _heartbeat() -> None:
    """Independent tick so a busy pipeline does not look like a dead bot."""
    while True:
        pipeline_ok = _task is not None and not _task.done()
        write_heartbeat(
            _HEARTBEAT,
            pipeline_ok,
            None if pipeline_ok else 'pipeline task stopped',
            last_ok=_heartbeat_last_ok,
        )
        await asyncio.sleep(30)


def get_pipeline() -> Pipeline:
    global _pipeline
    if _pipeline is None:
        s = get_settings()
        db = get_db()
        _pipeline = Pipeline(s, db, get_rules(), Classifier(s, db), get_pusher())

        def _critical():
            if (getattr(s, "newspaper_enabled", False)
                    and not getattr(s, "newspaper_legacy_events", True)):
                return get_scheduler().newspaper.urgent()
            return get_scheduler().mail_dispatch.immediate()

        _pipeline.critical_dispatch = _critical
    return _pipeline


def get_scheduler() -> SummaryScheduler:
    global _scheduler
    if _scheduler is None:
        s = get_settings()
        db = get_db()
        _scheduler = SummaryScheduler(s, db, Classifier(s, db), get_pusher())
    return _scheduler


async def _loop() -> None:
    global _heartbeat_last_ok
    s = get_settings()
    pipe = get_pipeline()
    while True:
        try:
            await pipe.tick()
            get_scheduler().mail_dispatch._events()  # persist semantics even with mail safely disabled
            use_newspaper = (getattr(s, "newspaper_enabled", False)
                             and not getattr(s, "newspaper_legacy_events", True))
            if use_newspaper:
                # 紧急课程先发，图片转发可能下载较慢，放在后面。
                await get_scheduler().newspaper.urgent()
                await get_scheduler().newspaper.forward_images()
            else:
                await get_scheduler().mail_dispatch.immediate()
            _heartbeat_last_ok = time.time()
        except Exception as exc:  # noqa: BLE001
            logger.warning("流水线异常: %s", type(exc).__name__)
        await asyncio.sleep(s.pipeline_interval_seconds)


async def _recover_orphans() -> None:
    """重启后残留的"半成品块"（判类未完成）重新判类。"""
    db = get_db()
    pipe = get_pipeline()
    after = 0
    while True:
        rows = db.orphan_blocks(after_id=after)
        if not rows:
            break
        for row in rows:
            after = row['id']
            try:
                evidence = db.block_messages(row['id'])
                if row['images'] not in (None,'[]','') or row['files'] not in (None,'[]',''):
                    topic, by = 'needs_review', 'media:recover'
                else:
                    topic, by = await pipe._decide(str(row['group_id']), evidence, row['text'] or '')
                db.set_block_topic(row['id'],topic,by+'+recover',confident=topic!='needs_review')
            except Exception as exc:
                logger.warning('Recovery block %s failed: %s',row['id'],type(exc).__name__)


async def _backfill_titles() -> None:
    """旧块后台补齐标题（只补过去 7 天）。与判类解耦，失败不阻断。"""
    s = get_settings()
    db = get_db()
    clf = get_pipeline().classifier
    since = int(time.time()) - 7 * 86400
    rows = db.untitled_blocks(since, topics=("notice", "file", "link", "cs", "bio"), limit=500)
    if not rows:
        return
    logger.info("补标题：%d 个块（过去 7 天）", len(rows))
    done = 0
    for row in rows:
        try:
            title = await clf.title(s.group_name(str(row["group_id"])),
                                    str(row["topic"] or ""), row["text"] or "")
            if title:
                db.set_block_title(int(row["id"]), title)
                done += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("补标题块 %s 失败: %s", row["id"], exc)
    logger.info("补标题完成：%d/%d", done, len(rows))


async def startup() -> None:
    global _task, _title_task, _heartbeat_task, _heartbeat_last_ok
    if _task and not _task.done():
        return
    s = get_settings()
    get_db()
    logger.info("info-hub 启动：DRY_RUN=%s 群=%d 话题=%s",
                s.dry_run, len(s.groups), ",".join(s.threads.keys()) or "无")
    try:
        if s.tg_enabled:
            await get_pusher().start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram 启动失败，忽略（邮件/采集继续）: %s", exc)
    await get_mailer().start()
    get_scheduler().start()
    await _recover_orphans()
    _heartbeat_last_ok = time.time()
    _task = asyncio.create_task(_loop())
    _heartbeat_task = asyncio.create_task(_heartbeat())
    # Titles are optional; never delay critical routing or backfill to a third party automatically.
    _title_task = None


async def shutdown() -> None:
    global _task, _title_task, _heartbeat_task
    for t in (_task, _title_task, _heartbeat_task):
        if t:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    _task = None
    _title_task = None
    _heartbeat_task = None
    get_scheduler().stop()
    await get_scheduler().wait_stopped()
    await get_mailer().stop()
    await get_pusher().stop()
    await get_pipeline().classifier.close()
    await get_scheduler().classifier.close()
    await get_scheduler().mail_dispatch.close()
    newspaper = getattr(get_scheduler(), "newspaper", None)
    if newspaper is not None and hasattr(newspaper, "close"):
        await newspaper.close()
    # APScheduler shutdown is queued on the event loop.
    await asyncio.sleep(0)
    from info_hub import db as db_module
    if db_module._db:
        db_module._db.close()
        db_module._db = None
    global _pipeline, _scheduler
    _pipeline = _scheduler = None
    from info_hub import mailer as mail_module, telegram_push as tg_module
    mail_module._channel = None
    tg_module._pusher = None
