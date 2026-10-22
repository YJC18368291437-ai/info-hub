"""Restore the exact pre-session source snapshot, preserving pre-existing user edits.

Dry-run unless --apply. Does not touch .env, groups, database, or remove new files.
Stop info-hub and keepalive first. Restores the OLD implementation: keep mail disabled.
"""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'docs/review-baseline'


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    expected=json.loads((BASE/'session-final-hashes.json').read_text(encoding='utf-8'))
    prepared=[]
    with zipfile.ZipFile(BASE/'source-before-session.zip') as archive:
        for name in archive.namelist():
            target=(ROOT/name).resolve()
            if ROOT not in target.parents or name in ('.env','config/groups.yaml','config/group_names.json'):
                raise SystemExit('Invalid restore target')
            if name not in expected or hashlib.sha256(target.read_bytes()).hexdigest()!=expected[name]:
                raise SystemExit('File changed since delivery; refusing overwrite: '+name)
            prepared.append((target,archive.read(name)))
    for target,data in prepared:
        if args.apply:
            target.write_bytes(data)
    print(('Restored ' if args.apply else 'Verified only: ')+str(len(prepared))+' source files; no DB/account action')


if __name__=='__main__': main()
