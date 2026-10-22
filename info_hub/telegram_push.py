"""Telegram 投放：发到论坛群对应话题；📌/🗑/↪️ 按钮与回调（G38/G39）。

- 📌 备忘：转发到 📌备忘 话题（附来源群名），并从原话题删除该条（移走，不是复制）。
- 🗑 噪音：从原话题删除（DB 留底），并记 feedback(noise) —— 供 AI 学习。
- ↪️ 改话题：弹出话题选择，重投到新话题 + 删除原条，记 feedback(topic)。
"""
from __future__ import annotations

import json
import logging

import aiohttp

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from info_hub.db import Database
from info_hub.rules import source_header
from info_hub.settings import Settings
from info_hub.textfmt import md_to_html

logger = logging.getLogger("info_hub.telegram")

# /here 支持中英文分类名
_TOPIC_ALIASES = {
    "at_me": "at_me", "@我": "at_me", "我": "at_me",
    "notice": "notice", "通知": "notice", "通知任务": "notice",
    "chat": "chat", "闲聊": "chat",
    "memo": "memo", "备忘": "memo",
    "cs": "cs", "计算机": "cs", "技术": "cs",
    "bio": "bio", "生物": "bio", "生物科学": "bio",
    "link": "link", "链接": "link",
    "file": "file", "文件": "file",
}


class TelegramPusher:
    def __init__(self, settings: Settings, db: Database):
        self.s = settings
        self.db = db
        self.app: Application | None = None
        self._http: aiohttp.ClientSession | None = None

    async def _download(self, url: str) -> bytes:
        from info_hub.safe_download import download
        return await download(url, kind='image')


    # ---------- 生命周期 ----------
    async def start(self) -> None:
        if not self.s.tg_enabled or self.s.dry_run:
            return
        if not self.s.tg_token or not self.s.tg_chat_id:
            logger.warning("未配置 TG_TOKEN/TG_CHAT_ID，Telegram 推送与按钮禁用（DRY_RUN 仍可看判类）")
            return
        req = HTTPXRequest(proxy=self.s.tg_proxy, connect_timeout=20, read_timeout=30)
        upd = HTTPXRequest(proxy=self.s.tg_proxy, connect_timeout=20, read_timeout=30)
        self.app = (
            Application.builder()
            .token(self.s.tg_token)
            .request(req)
            .get_updates_request(upd)
            .build()
        )
        self.app.add_handler(CallbackQueryHandler(self._on_callback))
        self.app.add_handler(
            MessageHandler(filters.TEXT & filters.Regex(r"^/(name|群名)\b"), self._on_name)
        )
        self.app.add_handler(
            MessageHandler(filters.TEXT & filters.Regex(r"^/(here|绑定)\b"), self._on_here)
        )
        self.app.add_handler(
            MessageHandler(filters.TEXT & filters.Regex(r"^/(setup|建话题)\b"), self._on_setup)
        )
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram 推送已启动（chat=%s）", self.s.tg_chat_id)

    async def start_sender(self) -> None:
        """只初始化 bot（不轮询），供本机手动触发一次推送。"""
        if not self.s.tg_enabled or self.s.dry_run or self.app is not None:
            return
        req = HTTPXRequest(proxy=self.s.tg_proxy, connect_timeout=20, read_timeout=30)
        self.app = Application.builder().token(self.s.tg_token).request(req).build()
        await self.app.initialize()

    async def stop(self) -> None:
        if self.app is None:
            return
        try:
            if getattr(self.app, "updater", None) and self.app.updater.running:
                await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram 关闭异常: %s", exc)

    # ---------- 发送 ----------
    def _buttons(self, block_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📌 备忘", callback_data=f"m:{block_id}"),
            InlineKeyboardButton("✅ 已读", callback_data=f"r:{block_id}"),
            InlineKeyboardButton("🗑 噪音", callback_data=f"x:{block_id}"),
            InlineKeyboardButton("↪️ 改话题", callback_data=f"t:{block_id}"),
        ]])

    def _topic_keyboard(self, prefix: str, ident: int) -> InlineKeyboardMarkup:
        order = ["at_me", "notice", "cs", "bio", "link", "chat"]
        row = [InlineKeyboardButton(self.s.topic_label(k), callback_data=f"{prefix}:{ident}:{k}") for k in order]
        return InlineKeyboardMarkup([row[:3], row[3:]])

    def digest_buttons(self, digest_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📌 备忘", callback_data=f"dm:{digest_id}"),
                InlineKeyboardButton("✅ 已读", callback_data=f"dr:{digest_id}"),
                InlineKeyboardButton("🗑 噪音", callback_data=f"dx:{digest_id}"),
                InlineKeyboardButton("↪️ 改话题", callback_data=f"dt:{digest_id}"),
            ],
            [InlineKeyboardButton("📜 原话", callback_data=f"d:{digest_id}")],
        ])

    async def push_blocks(self, blocks: list) -> int | None:
        """把一组块投到其话题（同群同话题短时合并成一条），返回 Telegram message id。"""
        if not self.s.tg_enabled or self.s.dry_run:
            return None
        if not blocks:
            return None
        block_id = int(blocks[0]["id"])
        topic = blocks[0]["topic"]
        chat_id, thread_id = self.s.resolve_target(topic)
        if not chat_id:
            logger.warning("分类 %s 无接收群（未绑定），跳过 block %s", topic, block_id)
            return None
        text = self._render(blocks)
        if self.s.dry_run or self.app is None:
            logger.info("[DRY_RUN] -> %s\n%s", self.s.topic_label(topic), text)
            return None
        msg = await self.app.bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
            parse_mode="HTML",
            reply_markup=self._buttons(block_id),
        )
        self.db.record_pushed(block_id, chat_id, thread_id, msg.message_id)
        for url in self._collect_images(blocks)[:6]:
            if topic in ("memo", "link") or self._is_sticker(url):
                continue
            try:
                data = await self._download(url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("下载图片失败: %s", exc)
                continue
            try:
                if len(data) <= 10 * 1024 * 1024:
                    await self.app.bot.send_photo(
                        chat_id=chat_id, message_thread_id=thread_id,
                        photo=data, reply_to_message_id=msg.message_id,
                    )
                else:
                    await self.app.bot.send_document(
                        chat_id=chat_id, message_thread_id=thread_id,
                        document=data, filename="image.jpg", reply_to_message_id=msg.message_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("发图失败: %s", exc)
        if topic not in ("memo", "link"):
            for f in self._collect_files(blocks)[:6]:
                await self._send_file(chat_id, thread_id, msg.message_id, f)
        return msg.message_id

    _STICKER_MARKERS = ("gxh.vip.qq.com", "/club/item/", "qzonestyle.gtimg.cn", "qzone/em")

    @classmethod
    def _is_sticker(cls, url: object) -> bool:
        u = str(url)
        return any(m in u for m in cls._STICKER_MARKERS)

    @staticmethod
    def _collect_files(blocks: list) -> list[dict]:
        out: list[dict] = []
        for b in blocks:
            try:
                fs = json.loads(b["files"] or "[]")
            except (KeyError, TypeError, ValueError):
                fs = []
            out.extend(f for f in fs if f)
        return out

    async def _send_file(self, chat_id: int, thread_id: int | None, reply_to: int, f: dict) -> None:
        url = f.get("url")
        if not url:
            return
        name = f.get("name") or "文件"
        kind = f.get("kind")
        try:
            data = await self._download(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载文件失败: %s", exc)
            return
        if len(data) > 48 * 1024 * 1024:
            logger.warning("文件过大跳过: %s", name)
            return
        try:
            if kind == "video":
                await self.app.bot.send_video(  # type: ignore[union-attr]
                    chat_id=chat_id, message_thread_id=thread_id,
                    video=data, reply_to_message_id=reply_to,
                )
            else:
                await self.app.bot.send_document(  # type: ignore[union-attr]
                    chat_id=chat_id, message_thread_id=thread_id,
                    document=data, filename=name, reply_to_message_id=reply_to,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("转发文件失败: %s", exc)

    @staticmethod
    def _collect_images(blocks: list) -> list[str]:
        out: list[str] = []
        for b in blocks:
            try:
                imgs = json.loads(b["images"] or "[]")
            except (KeyError, TypeError, ValueError):
                imgs = []
            out.extend(str(u) for u in imgs if u)
        return out

    def _render(self, blocks: list) -> str:
        import html as _h

        first, last = blocks[0], blocks[-1]
        header = source_header(self.s.group_name(str(first["group_id"])), "", int(last["end_ts"] or 0))
        body = "\n\n".join((b["text"] or "").strip() for b in blocks if (b["text"] or "").strip())
        head = f"<b>{_h.escape(header)}</b>"
        if body:
            return f"{head}\n<blockquote expandable>{_h.escape(body)}</blockquote>"
        return head

    # ---------- 回调 ----------
    async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self._handle_callback(update, ctx)
        except Exception:  # noqa: BLE001
            logger.exception("按钮回调异常")

    async def _handle_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None:
            return
        await q.answer()
        parts = q.data.split(":")
        action = parts[0]
        try:
            ident = int(parts[1])
        except (IndexError, ValueError):
            return

        # 简报相关（id 是 digests 表主键，不查 blocks）
        if action in ("d", "dm", "dx", "dr", "dt"):
            if action == "d":
                await self._show_quotes(q, ident)
            elif action == "dm":
                await self._do_memo_digest(q, ident)
            elif action == "dx":
                row = self.db.get_digest(ident)
                if row is not None:
                    self.db.add_feedback(str(row["group_id"]), "", str(row["topic"] or ""),
                                         str(row["topic"] or ""), "noise", "")
                await self._delete(q)
            elif action == "dr":
                await self._delete(q)
            elif action == "dt" and len(parts) == 3:
                await self._do_retopic_digest(q, ident, parts[2])
            elif action == "dt":
                await q.edit_message_reply_markup(reply_markup=self._topic_keyboard("dt", ident))
            return

        # block 相关
        block = self.db.get_block(ident)
        if block is None:
            return
        group_id = str(block["group_id"])
        text = block["text"] or ""
        if action == "m":  # 📌 备忘
            await self._do_memo(q, ident, group_id, text)
        elif action == "x":  # 🗑 噪音
            self.db.add_feedback(group_id, "", text, block["topic"], "noise", "")
            await self._delete(q)
        elif action == "r":  # ✅ 已读
            await self._delete(q)
        elif action == "t" and len(parts) == 3:  # 选定新话题
            await self._do_retopic(q, ident, parts[2])
        elif action == "t":  # 弹出话题选择
            await q.edit_message_reply_markup(reply_markup=self._topic_keyboard("t", ident))

    async def _do_memo_digest(self, q, digest_id: int) -> None:
        row = self.db.get_digest(digest_id)
        if row is None:
            return
        if not self.s.dry_run and self.app is not None:
            mchat, mthread = self.s.resolve_target("memo")
            msg = await self.app.bot.send_message(
                chat_id=mchat, message_thread_id=mthread,
                text="📌 " + (row["html"] or ""), parse_mode="HTML",
            )
            self.db.add_memo(0, self.s.group_name(str(row["group_id"])), row["quotes"] or "", msg.message_id)
        self.db.add_feedback(str(row["group_id"]), "", str(row["topic"] or ""), str(row["topic"] or ""), "memo", "")
        await self._delete(q)

    async def _do_retopic_digest(self, q, digest_id: int, new_topic: str) -> None:
        row = self.db.get_digest(digest_id)
        if row is None:
            return
        self.db.set_digest_topic(digest_id, new_topic)
        self.db.add_feedback(str(row["group_id"]), "", str(row["topic"] or ""),
                             str(row["topic"] or ""), "topic", new_topic)
        if not self.s.dry_run and self.app is not None:
            nchat, nthread = self.s.resolve_target(new_topic)
            await self.app.bot.send_message(
                chat_id=nchat, message_thread_id=nthread,
                text=row["html"] or "", parse_mode="HTML",
                reply_markup=self.digest_buttons(digest_id),
            )
        await self._delete(q)

    async def _on_here(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Telegram 指令：/here <分类> —— 把本群绑定为该分类的接收群。"""
        msg = update.effective_message
        chat = update.effective_chat
        if msg is None or chat is None:
            return
        parts = (msg.text or "").split(maxsplit=1)
        raw = parts[1].strip() if len(parts) > 1 else ""
        topic = _TOPIC_ALIASES.get(raw)
        if not topic:
            await msg.reply_text(
                "用法：/here 分类\n分类：" + " / ".join(["@我", "通知", "闲聊", "备忘", "计算机", "生物", "链接", "文件"])
            )
            return
        from info_hub.settings import set_target

        set_target(topic, chat.id, msg.message_thread_id)
        here = "本话题" if msg.message_thread_id else "本群"
        await msg.reply_text(f"✅ 已把{here}绑定为「{self.s.topic_label(topic)}」的接收处")

    async def _on_setup(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Telegram 指令：/setup 计算机 闲聊 生物科学 —— 自动建话题并绑定分类。"""
        msg = update.effective_message
        chat = update.effective_chat
        if msg is None or chat is None or self.app is None:
            return
        args = (msg.text or "").split()[1:]
        if not args:
            await msg.reply_text("用法：/setup 计算机 闲聊 生物科学\n（需先把本群设为论坛群，并给机器人 Manage Topics 权限）")
            return
        from info_hub.settings import set_target

        lines: list[str] = []
        for raw in args:
            topic = _TOPIC_ALIASES.get(raw)
            if not topic:
                lines.append(f"· {raw}：未知分类，跳过")
                continue
            label = self.s.topic_label(topic)
            try:
                t = await self.app.bot.create_forum_topic(chat_id=chat.id, name=label)
                set_target(topic, chat.id, t.message_thread_id)
                lines.append(f"· {label}：已建并绑定")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"· {label}：失败（{exc}）")
        await msg.reply_text("处理结果：\n" + "\n".join(lines))

    async def _show_quotes(self, q, digest_id: int) -> None:
        row = self.db.get_digest(digest_id)
        if row is None or self.app is None or q.message is None:
            return
        text = (row["quotes"] or "").strip() or "（无原话）"
        chunks: list[str] = []
        cur = "📜 原话\n"
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > 3500:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        if cur:
            chunks.append(cur)
        for i, ch in enumerate(chunks):
            await self.app.bot.send_message(
                chat_id=q.message.chat_id,
                message_thread_id=q.message.message_thread_id,
                text=ch,
                reply_to_message_id=q.message.message_id if i == 0 else None,
            )

    async def _on_name(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Telegram 指令：/name <群号> <名称> —— 自定义群名。"""
        msg = update.effective_message
        chat = update.effective_chat
        if msg is None or chat is None or chat.id not in self.s.known_chat_ids():
            return
        parts = (msg.text or "").split(maxsplit=2)
        if len(parts) < 3:
            await msg.reply_text("用法：/name 群号 名称\n例：/name 123456789 某大学信管群")
            return
        from info_hub.settings import set_group_name

        gid, name = parts[1].strip(), parts[2].strip()
        set_group_name(gid, name)
        await msg.reply_text(f"✅ 已把群 {gid} 命名为「{name}」")

    async def _do_memo(self, q, block_id: int, group_id: str, text: str) -> None:
        header = source_header(self.s.group_name(group_id), "", 0)
        memo_text = f"📌 {header}\n{text}"
        if not self.s.dry_run and self.app is not None:
            mchat, mthread = self.s.resolve_target("memo")
            msg = await self.app.bot.send_message(
                chat_id=mchat, message_thread_id=mthread,
                text=md_to_html(memo_text), parse_mode="HTML",
            )
            self.db.add_memo(block_id, self.s.group_name(group_id), text, msg.message_id)
        self.db.add_feedback(group_id, "", text, "memo", "memo", "")
        await self._delete(q)

    async def _do_retopic(self, q, block_id: int, new_topic: str) -> None:
        block = self.db.get_block(block_id)
        if block is None:
            return
        self.db.set_block_topic(block_id, new_topic, "user", True)
        self.db.add_feedback(str(block["group_id"]), "", block["text"] or "",
                             block["topic"], "topic", new_topic)
        if not self.s.dry_run and self.app is not None:
            nchat, nthread = self.s.resolve_target(new_topic)
            newblock = self.db.get_block(block_id)
            msg = await self.app.bot.send_message(
                chat_id=nchat, message_thread_id=nthread,
                text=self._render([newblock]), parse_mode="HTML",
                reply_markup=self._buttons(block_id),
            )
            self.db.record_pushed(block_id, nchat, nthread, msg.message_id)
        await self._delete(q)

    async def _delete(self, q) -> None:
        if self.s.dry_run or self.app is None or q.message is None:
            return
        try:
            await self.app.bot.delete_message(chat_id=q.message.chat_id, message_id=q.message.message_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除消息失败: %s", exc)


_pusher: TelegramPusher | None = None


def get_pusher() -> TelegramPusher:
    global _pusher
    if _pusher is None:
        from info_hub.db import get_db
        from info_hub.settings import get_settings

        _pusher = TelegramPusher(get_settings(), get_db())
    return _pusher
