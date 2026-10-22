"""Safe manual entry: offline preview by default; --live is an explicit operator action."""
import argparse
import asyncio
import datetime
from zoneinfo import ZoneInfo
from info_hub.settings import get_settings

def _since_ts(raw):
    tz = ZoneInfo('Asia/Shanghai')
    if raw:
        d = datetime.datetime.strptime(raw, '%Y-%m-%d').replace(tzinfo=tz)
    else:
        d = datetime.datetime.now(tz) - datetime.timedelta(days=7)
    return int(d.timestamp())

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['daily','weekly','immediate','school-backlog'],nargs='?',default='daily')
    parser.add_argument('--since',help='school-backlog start date, e.g. 2026-10-16')
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    if not args.live:
        from infohub_admin import preview
        await preview(args.mode,_since_ts(args.since))
        return
    from info_hub.db import get_db
    from info_hub.mailer import get_mailer
    from info_hub.mail_dispatch import MailDispatcher
    s=get_settings()
    if not (s.live_delivery_enabled and s.mail_enabled and not s.dry_run):
        raise SystemExit('Live delivery remains disabled; see acceptance report')
    db=get_db(); disp=MailDispatcher(s,db,get_mailer())
    try:
        if args.mode=='school-backlog':
            print('Confirmed envelopes:',await disp.school_backlog(_since_ts(args.since)))
        else:
            print('Confirmed envelopes:',await getattr(disp,args.mode)())
    finally:
        await disp.close(); db.close()

if __name__=='__main__':
    asyncio.run(main())
