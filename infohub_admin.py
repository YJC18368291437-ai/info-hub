"""Offline operator tools; never logs in or modifies a real mailbox."""
from contextlib import closing
import argparse
import asyncio
import json
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent


async def preview(mode='daily', since_ts=None):
    from info_hub.db import Database
    from info_hub.mail_dispatch import MailDispatcher
    from info_hub.settings import get_settings
    s = replace(get_settings(),dry_run=True,live_delivery_enabled=False,llm_enabled=False)
    folder = ROOT / 'data' / 'test-tmp'
    folder.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as td:
        target = Path(td)/'preview.sqlite'
        if s.db_path.exists():
            with closing(sqlite3.connect(s.db_path.as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
        db = Database(target)
        class PreviewMail:
            ready=True
            async def deliver(self,messages):
                return len(messages)  # only the throwaway snapshot is marked sent
        dispatcher = MailDispatcher(s,db,PreviewMail())
        try:
            if mode == 'school-backlog':
                n = await dispatcher.school_backlog(since_ts)
            else:
                n = await getattr(dispatcher,mode)()
            print(json.dumps({'mode':mode,'preview_envelopes':n,'backlog':db.backlog(),
                              'real_mail_connections':0,'source_db_modified':False},ensure_ascii=False))
        finally:
            await dispatcher.close()
            db.close()


def static_check():
    from info_hub.settings import get_settings
    from info_hub.schedule import inspect_schedule
    s = get_settings()
    path = Path('D:/napcat-toolkit/napcat/config') / ('onebot11_'+s.self_qq+'.json')
    client_present = False
    if path.exists():
        cfg = json.loads(path.read_text(encoding='utf-8-sig'))
        clients = cfg.get('network',{}).get('websocketClients',cfg.get('websocketClients',[]))
        client_present = any(c.get('url')=='ws://127.0.0.1:8082/onebot/v11/ws' and c.get('enable',False) for c in clients)
    print(json.dumps({'self_matches_main':s.self_qq in s.main_qq,
        'mail_separate_from_collector':s.mail_address.split('@')[0]!=s.self_qq,
        'whitelist_count':len(s.groups),'target_napcat_config_exists':path.exists(),
        'target_8082_client':client_present,'live_delivery_enabled':s.live_delivery_enabled,
        'tg_enabled':s.tg_enabled,'llm_enabled':s.llm_enabled,
        'schedule':inspect_schedule(ROOT/'docs/schedule-import.json')},ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('static-check')
    sub.add_parser('backlog')
    sub.add_parser('tasks')
    p=sub.add_parser('preview'); p.add_argument('mode',choices=['daily','weekly','immediate'],default='daily',nargs='?')
    p=sub.add_parser('status'); p.add_argument('event'); p.add_argument('value',choices=['open','pending','registered','completed','declined','archived'])
    p=sub.add_parser('preference'); p.add_argument('event'); p.add_argument('value',choices=['on','off'])
    if (args:=parser.parse_args()).command=='static-check':
        static_check(); return
    if args.command=='preview':
        asyncio.run(preview(args.mode)); return
    from info_hub.db import Database
    from info_hub.events import EventStore
    db=Database(ROOT/'data/info_hub.db')
    try:
        if args.command=='backlog':
            print(json.dumps(db.backlog()))
        elif args.command=='tasks':
            for e in db._query("SELECT id,status,payload FROM events WHERE status IN ('pending','registered') ORDER BY id"):
                p=json.loads(e['payload'])
                print(f"E{e['id']} {e['status']} 截止={p.get('deadline_raw') or '未知'} {p['title']}")
        else:
            eid=int(args.event.upper().removeprefix('E'))
            if not db._query('SELECT 1 FROM events WHERE id=?',(eid,)):
                raise SystemExit('Unknown event ID')
            if args.command=='status':
                EventStore(db).set_status(eid,args.value)
            else:
                db._exec('UPDATE events SET negative=? WHERE id=?',(int(args.value=='off'),eid))
                db.audit('event',eid,'explicit_preference',args.value)
    finally:
        db.close()


if __name__=='__main__':
    main()
