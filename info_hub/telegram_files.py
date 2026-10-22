"""Telegram 原图通道：只发表情/图片文件，不启动轮询、不处理其他 TG 服务。"""
from __future__ import annotations

import asyncio
import io
import logging

from info_hub.settings import Settings, get_settings

logger = logging.getLogger("info_hub.telegram_files")


class TelegramFileSender:
    def __init__(self, settings: Settings):
        self.s = settings
        self.enabled = bool(getattr(settings, "tg_image_enabled", False)
                            and settings.tg_token and settings.tg_chat_id)
        self.chat_id, self.thread_id = settings.resolve_target("file")
        self._bot = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> bool:
        if self._bot is not None:
            return True
        if not self.enabled:
            return False
        from telegram import Bot
        from telegram.request import HTTPXRequest
        req = HTTPXRequest(proxy=self.s.tg_proxy, connect_timeout=30, read_timeout=120)
        self._bot = Bot(self.s.tg_token, request=req)
        await self._bot.initialize()
        logger.info("Telegram 原图通道已启动（chat=%s thread=%s）", self.chat_id, self.thread_id)
        return True

    async def send_document(self, data: bytes, filename: str, caption: str = "") -> int | None:
        if not self.enabled or not data:
            return None
        async with self._lock:
            try:
                if not await self._ensure():
                    return None
                msg = await self._bot.send_document(  # type: ignore[union-attr]
                    chat_id=self.chat_id,
                    message_thread_id=self.thread_id,
                    document=io.BytesIO(data),
                    filename=filename,
                    caption=(caption or "")[:1024],
                )
                return int(msg.message_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Telegram 原图发送失败：%s: %s", type(exc).__name__, exc)
                return None

    async def close(self) -> None:
        if self._bot is None:
            return
        try:
            await self._bot.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram 原图通道关闭异常：%s", exc)
        finally:
            self._bot = None


_sender: TelegramFileSender | None = None


def get_tg_files() -> TelegramFileSender:
    global _sender
    settings = get_settings()
    if _sender is None or _sender.s is not settings:
        _sender = TelegramFileSender(settings)
    return _sender


def reset_tg_files() -> None:
    global _sender
    _sender = None
