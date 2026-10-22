"""旧 Telegram 手动入口，默认关闭；TG_ENABLED=false 或 DRY_RUN 时不启动、不消费状态。"""
from __future__ import annotations

import asyncio
import logging

from info_hub.classify import Classifier
from info_hub.db import get_db
from info_hub.scheduler import SummaryScheduler
from info_hub.settings import get_settings
from info_hub.telegram_push import get_pusher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")


async def main() -> None:
    s = get_settings()
    if not s.tg_enabled or s.dry_run:
        print('Legacy Telegram disabled; no state consumed')
        return
    db = get_db()
    pusher = get_pusher()
    classifier = Classifier(s, db)
    await pusher.start_sender()
    try:
        sched = SummaryScheduler(s, db, classifier, pusher)
        await sched._run()
    finally:
        if classifier._session is not None and not classifier._session.closed:
            await classifier._session.close()
        if pusher._http is not None and not pusher._http.closed:
            await pusher._http.close()
        if pusher.app is not None:
            try:
                await pusher.app.shutdown()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    asyncio.run(main())
