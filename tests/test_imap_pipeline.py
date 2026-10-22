"""Fake OneBot evidence and fake IMAP UID server; no external connections."""
import base64
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from info_hub.db import Database
from info_hub.mailer import MailFetcher, MailChannel, build_message
from info_hub.mail_dispatch import MailDispatcher
from info_hub.pipeline import Pipeline
from info_hub.rules import Rules
from info_hub.events import EventStore
from info_hub.outbox import DeliveryError

CHINESE_FOLDER='&'+base64.b64encode('不感兴趣'.encode('utf-16-be')).decode().rstrip('=').replace('/',',')+'-'
TMP=Path(__file__).resolve().parents[1]/'data/test-tmp'


class FakeIMAP:
    def __init__(self):
        self.folders={'INBOX':{},'Archive':{},'My Stuff':{},CHINESE_FOLDER:{},'Sent Messages':{}}
        self.validity={name:1 for name in self.folders}
        self.selected=''; self.fetch_calls=[]
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def list(self): return 'OK',[f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self.folders]
    def select(self,name,readonly=False):
        assert readonly
        self.selected=name.strip('"'); return 'OK',[b'1']
    def response(self,name): return 'UIDVALIDITY',[str(self.validity[self.selected]).encode()]
    def uid(self,op,*args):
        folder=self.folders[self.selected]
        if op=='search':
            lo=int(args[-1].split(':')[0])
            return 'OK',[' '.join(str(uid) for uid in folder if uid>=lo).encode()]
        assert op=='fetch'
        ids=[int(x) for x in args[0].split(',')]
        self.fetch_calls.append((self.selected,ids))
        return 'OK',[(f'{uid} (UID {uid} FLAGS ({flag})'.encode(),header) for uid in ids if uid in folder for header,flag in [folder[uid]]]
    def add(self,uid,eid=1,folder='INBOX',flag='',mid=None,readonly=False):
        header=f'Message-ID: {mid or "<mail-"+str(uid)+">"}\r\nX-InfoHub-Event: {eid}\r\nX-InfoHub-Version: 1\r\n'
        if readonly: header+='X-InfoHub-ReadOnly: true\r\n'
        self.folders[folder][uid]=(header.encode(),flag)


class IMAPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TMP)
        self.db=Database(Path(self.temp.name)/'db')
        self.db._conn.execute('PRAGMA synchronous=OFF')
        self.server=FakeIMAP(); self.fetch=MailFetcher(SimpleNamespace(uninterested_folder='不感兴趣'))
        self.fetch.db=self.db; self.fetch._connect=lambda:self.server
    def tearDown(self): self.db.close(); self.temp.cleanup()

    def test_incremental_beyond_500_and_old_flag_changes(self):
        for uid in range(1,802): self.server.add(uid,eid=uid)
        for _ in range(4): out=self.fetch.locate_messages()
        self.assertEqual(len(out),801)
        self.server.add(1,eid=1,flag='\\Flagged')
        for _ in range(5): out=self.fetch.locate_messages()
        self.assertTrue(out['<mail-1>'][1])
        self.assertLessEqual(max(len(ids) for _,ids in self.server.fetch_calls),500)

    def test_uidvalidity_reset_movement_and_inbox_duplicate(self):
        self.server.add(1); self.fetch.locate_messages()
        self.server.folders['INBOX'].clear(); self.server.validity['INBOX']=2
        self.server.add(1,eid=2,mid='<new>'); self.server.add(7,eid=1,folder='Archive',mid='<mail-1>')
        out=self.fetch.locate_messages()
        self.assertEqual(out['<new>'][2],2); self.assertEqual(out['<mail-1>'][0],'archive')
        self.server.add(10,eid=1,mid='<mail-1>',flag='\\Flagged')
        out=self.fetch.locate_messages(); self.assertEqual(out['<mail-1>'][:2],('inbox',True))
        self.server.add(10,eid=1,mid='<mail-1>',flag='')
        out=self.fetch.locate_messages(); self.assertFalse(out['<mail-1>'][1])

    def test_chinese_folder_sent_excluded_and_digest_not_mapped(self):
        self.server.add(1,folder=CHINESE_FOLDER)
        self.server.add(2,folder='Sent Messages')
        self.server.add(3,readonly=True)
        out=self.fetch.locate_messages()
        self.assertEqual(out['<mail-1>'][0],'uninterested')
        self.assertNotIn('<mail-2>',out); self.assertIsNone(out['<mail-3>'][2])


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TMP); self.db=Database(Path(self.temp.name)/'db')
        self.db._conn.execute('PRAGMA synchronous=OFF')
        self.s=SimpleNamespace(groups={'10001':{'course':True},'20002':{}},rules={},tg_enabled=False,dry_run=True,
            block_window_seconds=120,block_max_messages=25,block_merge_window_seconds=300,
            group_name=lambda g:g,group_tag=lambda g:'common',main_qq=['12345'])
        self.clf=SimpleNamespace(classify=AsyncMock(return_value=('needs_review','fallback')),title=AsyncMock(return_value=''))
        self.pusher=SimpleNamespace(push_blocks=AsyncMock(return_value=None))
        self.pipe=Pipeline(self.s,self.db,Rules(self.s),self.clf,self.pusher)
    async def asyncTearDown(self): self.db.close(); self.temp.cleanup()
    def add(self,mid,group='10001',**kw):
        self.db.add_message(dict(msg_id=mid,group_id=group,account_id='12345',user_id='u',nickname='a',ts=100,text='教室改到A',**kw))

    async def test_course_path_bypasses_wait_and_llm(self):
        self.add(1,at_all=True)
        with patch('info_hub.pipeline.time.time',return_value=101): await self.pipe.tick()
        self.assertEqual(self.db._query('SELECT COUNT(*) FROM blocks')[0][0],1)
        self.clf.classify.assert_not_awaited(); self.clf.title.assert_not_awaited()
        self.pusher.push_blocks.assert_not_awaited()

    async def test_media_failure_visible_evidence_preserved(self):
        self.add(1,images=['https://media.invalid/a'])
        await self.pipe.tick()
        b=dict(self.db.get_block(1))
        self.assertEqual(b['topic'],'needs_review')
        self.assertIn('media.invalid',b['images'])
        self.assertEqual(len(self.db.block_messages(1)),1)

    async def test_501_pending_eventually_processed_no_group_starvation(self):
        for i in range(501): self.add(i,group='20002')
        self.add(999,group='10001',at_all=True)
        for _ in range(4): await self.pipe.tick()
        self.assertEqual(self.db.backlog()['pending_messages'],0)
        self.assertTrue(self.db._query("SELECT 1 FROM blocks WHERE group_id='10001'"))

    async def test_orphan_recovery_pages_and_rebuilds_at_evidence(self):
        from info_hub import runtime
        for i in range(205):
            self.add(i,at_all=True)
            m=self.db._query('SELECT * FROM messages WHERE msg_id=?',(i,))[0]
            self.db.create_block('10001',[m],'original')
        with patch.object(runtime,'get_db',return_value=self.db),patch.object(runtime,'get_pipeline',return_value=self.pipe):
            await runtime._recover_orphans()
            self.assertEqual(self.db.backlog()['orphan_blocks'],0)
            self.assertEqual(self.db._query("SELECT COUNT(*) FROM blocks WHERE topic='notice'")[0][0],205)
            await runtime._recover_orphans()
            self.assertEqual(self.db._query('SELECT COUNT(*) FROM blocks')[0][0],205)

    async def test_tg_no_target_does_not_consume(self):
        self.add(1); await self.pipe.tick(); self.db.set_block_topic(1,'at_me','test')
        self.s.tg_enabled=True; self.s.dry_run=False
        await self.pipe._push()
        self.assertEqual(self.db.get_block(1)['pushed'],0)

    async def test_quoted_keywords_no_hard_notice(self):
        self.assertIsNone(self.pipe.rules.coarse_topic({'text':'据说不用报名考试，这不是通知'}))
        self.assertIsNone(self.pipe.rules.coarse_topic({'text':'引用 @全体成员 的用法'}))


class DeliveryChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TMP); self.db=Database(Path(self.temp.name)/'db')
        self.db._conn.execute('PRAGMA synchronous=OFF')
        self.s=SimpleNamespace(mail_enabled=True,mail_address='fixture@example.invalid',mail_auth_code='fake',
            dry_run=False,live_delivery_enabled=True,mail_send_mode='append',mail_from_name='fixture',mail_daily_limit=0,
            uninterested_folder='不感兴趣',base_dir=Path(self.temp.name),groups={},group_name=lambda g:g,topic_label=lambda t:t)
        self.mail=MailChannel(self.s,self.db)
    async def asyncTearDown(self): self.db.close(); self.temp.cleanup()

    async def test_append_ack_logout_failure_still_success_and_timeout_unknown(self):
        class Conn:
            def append(self,*args): return 'OK',[b'APPENDUID 1 42']
            def logout(self): raise OSError('connection dropped after ACK')
        self.mail.fetcher._connect=lambda:Conn()
        self.assertEqual(self.mail._deliver_sync([build_message(self.s,subject='test',text='x')]),1)
        class Timeout(Conn):
            def append(self,*args): raise TimeoutError('after write')
        self.mail.fetcher._connect=lambda:Timeout()
        with self.assertRaises(DeliveryError) as cm: self.mail._deliver_sync([build_message(self.s,subject='test',text='x')])
        self.assertTrue(cm.exception.unknown)

    async def test_dry_run_makes_no_transport_calls(self):
        self.s.dry_run=True; self.mail.fetcher._connect=lambda: self.fail('real transport attempted')
        with self.assertRaises(DeliveryError): self.mail._deliver_sync([build_message(self.s,subject='test',text='x')])
        self.assertEqual(self.mail.poll_once(),[])

    async def test_daily_cutoff_knowledge_weekly_exploration_quota_and_todo(self):
        class Fake:
            ready=True
            sent=[]
            async def deliver(self,msgs): self.sent.extend(msgs); return len(msgs)
        fake=Fake(); dispatch=MailDispatcher(self.s,self.db,fake)
        store=EventStore(self.db)
        def event(i,cat,ts=100000):
            bid=self.db._exec("INSERT INTO blocks(group_id,text,topic,created_ts) VALUES('10001',?,'bio',?)",(str(i),ts)).lastrowid
            return store.ingest({'id':bid},dict(category=cat,mandatory=False,confidence=0.8,text=str(i),title=str(i),deadline_ts=None),ts)
        e=event(1,'opportunity',200000); event(2,'knowledge')
        for i in range(3,11): event(i,'exploration')
        with patch.object(dispatch,'_backfill_floor',return_value=0):
            with patch('info_hub.mail_dispatch.time.time',return_value=200100):
                self.assertEqual(await dispatch.daily(cutoff=199999),0)
                self.assertEqual(await dispatch.daily(cutoff=200000),1)
                self.assertEqual(await dispatch.daily(cutoff=200000),0)
                self.assertEqual(await dispatch.weekly(),1)
                self.assertEqual(await dispatch.weekly(),0)
                mapped=self.db._query("SELECT COUNT(*) FROM delivery_events d JOIN deliveries m ON m.key=d.delivery_key WHERE m.kind='weekly'")[0][0]
                self.assertEqual(mapped,6) # one knowledge + five exploration, no filler
                store.set_status(e,'pending')
                self.assertEqual(await dispatch.todo(),1); self.assertEqual(await dispatch.todo(),0)
                self.assertIn('E'+str(e),fake.sent[-1].get_body(preferencelist=('plain',)).get_content())
                store.set_status(e,'archived'); self.assertEqual(await dispatch.todo(),1)
        await dispatch.close()
