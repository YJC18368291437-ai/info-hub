"""Offline regressions: temp SQLite and a fake mail channel; no QQ/mail/API calls."""
import asyncio, tempfile, time, unittest
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from info_hub.db import Database
from info_hub.mail_dispatch import MailDispatcher

class FakeMailer:
    ready=True
    def __init__(self): self.fail=False; self.count=0
    async def deliver(self,messages):
        if self.fail:
            from info_hub.outbox import DeliveryError
            raise DeliveryError("connect", unknown=False)
        self.count+=len(messages)
        return len(messages)

class CursorRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.temp.name)/"test.sqlite")
        self.db._conn.execute("PRAGMA synchronous=OFF")  # temporary fixture only
        self.clock=1900000000
        self.timepatch=patch("info_hub.mail_dispatch.time.time",side_effect=lambda:self.clock)
        self.timepatch.start()
        self.mail=FakeMailer()
        settings=SimpleNamespace(mail_enabled=True,mail_daily_limit=0,
                                 group_name=lambda x:x,topic_label=lambda x:x)
        self.dispatch=MailDispatcher(settings,self.db,self.mail)
        self.dispatch._build_block_message=AsyncMock(side_effect=lambda b:EmailMessage())
        self.buildpatch=patch("info_hub.mail_dispatch.build_message",side_effect=lambda *a,**k:EmailMessage())
        self.buildpatch.start()
    async def asyncTearDown(self):
        self.buildpatch.stop(); self.timepatch.stop()
        self.db._conn.close(); self.temp.cleanup()
    def block(self,topic=None,age=100,text=None):
        c=self.db._exec("INSERT INTO blocks(group_id,start_ts,end_ts,text,topic,title,created_ts) VALUES(?,?,?,?,?,?,?)",
                        ("test",self.clock-age,self.clock-age,text or "报名 fixture " + str(self.db._query("SELECT COUNT(*) FROM blocks")[0][0]),topic,"fixture",self.clock-age))
        return c.lastrowid
    async def test_late_daily_classification_delivered_once(self):
        i=self.block()
        self.assertEqual(await self.dispatch.daily(),0)
        self.clock+=20; self.db.set_block_topic(i,"notice","test")
        self.assertEqual(await self.dispatch.daily(),1)
        self.assertEqual(await self.dispatch.daily(),0)
    async def test_failed_daily_delivery_retried(self):
        self.block("notice"); self.mail.fail=True
        self.assertEqual(await self.dispatch.daily(),0)
        self.clock+=120; self.mail.fail=False
        self.assertEqual(await self.dispatch.daily(),1)
        self.assertEqual(await self.dispatch.daily(),0)
    async def test_late_weekly_classification_delivered_once(self):
        i=self.block(text="论文资料")
        self.assertEqual(await self.dispatch.weekly(),0)
        self.clock+=20; self.db.set_block_topic(i,"cs","test")
        self.assertEqual(await self.dispatch.weekly(),1)
        self.assertEqual(await self.dispatch.weekly(),0)
    async def test_daily_limit_batch_remaining_not_lost(self):
        for _ in range(1001): self.block("notice")
        self.assertEqual(await self.dispatch.daily(),1000)
        self.clock+=20
        self.assertEqual(await self.dispatch.daily(),1)
        self.assertEqual(await self.dispatch.daily(),0)
    async def test_initial_backfill_stays_seven_days(self):
        self.block("notice",age=8*86400)
        self.assertEqual(await self.dispatch.daily(),0)

if __name__=="__main__": unittest.main(verbosity=2)
