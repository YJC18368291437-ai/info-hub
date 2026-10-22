"""Undo only this session's inserted guards; refuse to overwrite subsequent edits.

Default: inspect hashes only. --apply restores bytes, never starts a process.
"""
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    changes=json.loads((ROOT/'docs/review-baseline/napcat-guards.json').read_text(encoding='utf-8'))
    prepared=[]
    for change in changes:
        p=Path(change['path'])
        if p.parent.resolve()!=Path('D:/napcat-toolkit').resolve():
            raise SystemExit('Invalid target path')
        raw=p.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=change['after_sha256']:
            raise SystemExit('Changed since guard insertion; refusing overwrite: '+str(p))
        text=raw.decode(change['encoding']); start=change['offset_chars']; guard=change['guard']
        if text[start:start+len(guard)]!=guard:
            raise SystemExit('Guard mismatch')
        restored=(text[:start]+text[start+len(guard):]).encode(change['encoding'])
        if hashlib.sha256(restored).hexdigest()!=change['before_sha256']:
            raise SystemExit('Original hash mismatch')
        prepared.append((p,restored))
    for p,raw in prepared:
        if args.apply:
            p.write_bytes(raw)
        print(('restored: ' if args.apply else 'verified only: ')+str(p))


if __name__=='__main__': main()
