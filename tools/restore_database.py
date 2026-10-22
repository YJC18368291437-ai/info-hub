"""Offline SQLite restore with a verified pre-restore backup; --apply required."""
import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from info_hub.migrations import backup
from info_hub.process_lock import ProcessLock


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('snapshot',type=Path)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    source=args.snapshot.resolve(); target=ROOT/'data/info_hub.db'
    if source==target.resolve() or not source.is_file():
        raise SystemExit('Choose an existing backup, not the current DB')
    lock=ProcessLock(ROOT/'data/run.lock').acquire()
    try:
        if not (ROOT/'data/PAUSED').exists():
            raise SystemExit('Pause keepalive and stop ALL service/manual writers first')
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src:
            if src.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise SystemExit('Backup failed integrity check')
            if args.apply:
                with closing(sqlite3.connect(target)) as dest:
                    preserved=backup(dest,target)
                    src.backup(dest)
                print('Restored; previous DB preserved at '+str(preserved))
            else:
                print('Backup verified only; no DB changed')
    finally:
        lock.close()


if __name__=='__main__': main()
