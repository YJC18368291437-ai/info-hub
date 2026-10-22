"""待处理消息按内部 ID 持续分页；课程/@全体优先，媒体/模型失败保留为待确认。旧 Telegram 路径默认关闭。"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

from info_hub.block_builder import block_files, block_images, block_text, build_blocks
from info_hub.classify import Classifier
from info_hub.db import Database
from info_hub.rules import CONTEXT_TOPICS, Rules
from info_hub.settings import Settings
from info_hub.sources import is_course_source
from info_hub.telegram_push import TelegramPusher

logger = logging.getLogger("info_hub.pipeline")


class Pipeline:
    def __init__(self, settings: Settings, db: Database, rules: Rules,
                 classifier: Classifier, pusher: TelegramPusher):
        self.s = settings
        self.db = db
        self.rules = rules
        self.classifier = classifier
        self.pusher = pusher
        self._scan_id = 0
        self.critical_dispatch = None

    async def tick(self) -> None:
        await self._build()
        if self.s.tg_enabled:
            await self._push()

    # ---------- 聚块 + 判类 ----------
    async def _build(self) -> None:
        normal = self.db.pending_messages(after_id=self._scan_id)
        priority = self.db.priority_messages([g for g in self.s.groups if is_course_source(self.s,g)])
        if not normal:
            self._scan_id = 0
        else:
            self._scan_id = max(m['id'] for m in normal)
        seen = {m['id'] for m in priority}
        pending = priority + [m for m in normal if m['id'] not in seen]
        if not pending:
            return
        by_group: dict[str, list] = defaultdict(list)
        for m in pending:
            by_group[str(m["group_id"])].append(m)

        now = time.time()
        window = self.s.block_window_seconds
        for group_id, msgs in by_group.items():
            msgs.sort(key=lambda m: int(m["ts"]))
            for block in build_blocks(msgs, window, self.s.block_max_messages):
                course = is_course_source(self.s,group_id)
                urgent = course or any(m['at_all'] for m in block)
                if now - int(block[-1]["ts"]) < window and not urgent:
                    break  # 尾部未闭合，留给下一轮
                ids = [int(m["id"]) for m in block]
                text = block_text(block)
                images = block_images(block)
                files = block_files(block)
                if not text.strip() and not images and not files:
                    self.db.assign_block(ids, -1)  # 空块不再处理，防死循环
                    continue
                block_id = self.db.create_block(group_id, block, text, images, files)
                if files or images:
                    topic, decided_by = "needs_review", "media:unread"
                else:
                    topic, decided_by = await self._decide(group_id, block, text)
                self.db.set_block_topic(block_id, topic, decided_by, confident=topic != 'needs_review')
                if urgent and self.critical_dispatch is not None:
                    await self.critical_dispatch()
                try:
                    if urgent or topic == 'needs_review':
                        continue  # no second network call on critical/review path
                    title = await self.classifier.title(self.s.group_name(group_id), topic, text)
                    if title:
                        self.db.set_block_title(block_id, title)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("生成标题失败 block %s: %s", block_id, type(exc).__name__)
                logger.info("block %s [%s/%s] %d 条 -> %s",
                            block_id, group_id, self.s.group_tag(group_id),
                            len(block), topic)

    async def _decide(self, group_id: str, block: list, text: str) -> tuple[str, str]:
        # 1) 规则硬判
        hint = None
        for m in block:
            h = self.rules.coarse_topic(dict(m))
            if h == "at_me":
                hint = "at_me"
                break
            if h == "notice":
                hint = hint or "notice"
        if hint:
            return hint, "rule"

        if is_course_source(self.s,group_id):
            return 'needs_review', 'course:confirm'

        # 2) LLM 判类
        topic, decided_by = await self.classifier.classify(group_id, text)

        # 3) 链接兜底（G53）
        links = self.rules.extract_links(text)
        fallback = self.rules.link_fallback_topic(topic, links)
        if fallback:
            return fallback, decided_by + "+link"
        return topic, decided_by

    # ---------- 投放（同群同话题短时合并成一条，G32 修订） ----------
    async def _push(self) -> None:
        if not self.s.tg_enabled:
            return
        blocks = self.db.unpushed_blocks()
        for group in self._merge_adjacent(blocks):
            result = await self.pusher.push_blocks(group)
            if result is not None and not self.s.dry_run:
                for b in group:
                    self.db.mark_block_pushed(int(b["id"]))

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
