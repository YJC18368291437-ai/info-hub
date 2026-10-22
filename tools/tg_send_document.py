#!/usr/bin/env python3
"""手动把本地文件发到 Telegram 文件线程，验证原图通道。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from telegram import Bot
from telegram.request import HTTPXRequest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from info_hub.settings import get_settings


async def _main(paths: list[Path]) -> int:
    s = get_settings()
    if not s.tg_token or not s.tg_chat_id:
        raise SystemExit("未配置 TG_TOKEN / TG_CHAT_ID")
    chat_id, thread = s.resolve_target("file")
    req = HTTPXRequest(proxy=s.tg_proxy, connect_timeout=30, read_timeout=120)
    bot = Bot(s.tg_token, request=req)
    await bot.initialize()
    try:
        for path in paths:
            if not path.is_file():
                print(f"跳过（不存在）：{path}")
                continue
            size = path.stat().st_size
            print(f"发送 {path.name} · {size} bytes ...", end="", flush=True)
            with path.open("rb") as fh:
                msg = await bot.send_document(
                    chat_id=chat_id,
                    document=fh,
                    filename=path.name,
                    message_thread_id=thread,
                    caption=f"INFOHUB 原图通道测试 · {path.name} · {size / 1024:.0f} KB",
                )
            print(f" ok message_id={msg.message_id}")
    finally:
        await bot.shutdown()
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python tools/tg_send_document.py <文件> [文件...]")
        return 2
    return asyncio.run(_main([Path(p) for p in sys.argv[1:]]))


if __name__ == "__main__":
    raise SystemExit(main())
