"""Validate the preserved export without inventing a calendar."""
import hashlib
import json
import re
from pathlib import Path


def inspect_schedule(path: Path) -> dict:
    raw = path.read_bytes()
    data = json.loads(raw)
    if data.get('t') != 'ncs_qr_schedule' or data.get('v') != 2 or not isinstance(data.get('courses'), list):
        raise ValueError('Unsupported schedule export')
    groups = {}
    varying = []
    for c in data['courses']:
        if not isinstance(c.get('name'), str) or not c['name'].strip():
            raise ValueError('Course name required')
        if not isinstance(c.get('weeks'), list) or any(type(w) is not int or not 1 <= w <= 53 for w in c['weeks']):
            raise ValueError('Invalid course weeks')
        for key, maximum in (('week_time',7),('start_time',30),('time_count',30)):
            if type(c.get(key)) is not int or not 0 <= c[key] <= maximum:
                raise ValueError(f'Invalid {key}')
        info = c.get('info', '')
        for gid in re.findall(r'(?:QQ\s*群(?:号)?|群)\s*[：:（(]?\s*([1-9][0-9]{4,11})', info, re.I):
            groups[gid] = c['name']
        if '第' in info and '周' in info and ('教' in info or '礼堂' in info):
            varying.append(c['name'])
    return {'version': data['v'], 'sha256': hashlib.sha256(raw).hexdigest(),
            'records': len(data['courses']), 'course_groups': groups,
            'missing': ['semester_first_monday', 'period_clock_times', 'special_week_confirmation'],
            'varying_classrooms': sorted(set(varying)), 'calendar_ready': False}
