"""报纸手动入口：默认只生成本地预览；--live 才发邮件。"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
from pathlib import Path

from info_hub.classify import Classifier
from info_hub.db import get_db
from info_hub.mailer import get_mailer
from info_hub.newspaper import (NewspaperDispatcher, TZ, render_notice, render_tech,
                                sample_notice_items, sample_tech_report)
from info_hub.settings import get_settings


def _write_preview(name: str, html: str) -> Path:
    path = Path("data") / f"newspaper_preview_{name}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["preview", "notice", "tech"], nargs="?", default="preview")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    if not args.live:
        now = _dt.datetime.now(TZ)
        items = sample_notice_items(now)
        html, _ = render_notice(now, items, len(items))
        p1 = _write_preview("notice", html)
        tech_html, _ = render_tech(now, sample_tech_report(), forced=False, s=s)
        p2 = _write_preview("tech", tech_html)
        print(f"预览已写入：{p1}")
        print(f"预览已写入：{p2}")
        return

    if not (s.mail_enabled and s.live_delivery_enabled and not s.dry_run):
        raise SystemExit("邮件未启用：需要 MAIL_ENABLED=true、LIVE_DELIVERY_ENABLED=true、DRY_RUN=false")

    db = get_db()
    dispatcher = NewspaperDispatcher(s, db, Classifier(s, db), get_mailer())
    try:
        if args.mode == "preview":
            print("已发送样例封数：", await dispatcher.send_sample())
        elif args.mode == "notice":
            print("已发送通知报封数：", await dispatcher._notice_tick())
        else:
            dispatcher._tech_scan_at = 0
            print("已发送技术报封数：", await dispatcher._tech_tick())
    finally:
        await dispatcher.classifier.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
