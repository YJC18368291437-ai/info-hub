"""Conservative event identity, versions and routing. All source blocks are retained.

Only exact announcements or explicit title + organizer + edition identities merge.
Uncertain similarities remain separate review items; a missed merge beats a lost update.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
CATEGORIES = {'course', 'opportunity', 'knowledge', 'exploration', 'needs_review', 'chat'}
KEY_FIELDS = ('title','organizer','edition','start_raw','start_ts','deadline_raw','deadline_ts',
              'location','eligibility','cost','travel','skills','registration','cancelled')


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def content(text: str) -> str:
    # Strip ONLY collector nickname prefixes, never arbitrary sentences.
    labels = ('标题','主办方','届次','时间','截止','截止时间','地点','资格','费用','出行','技能','报名方式')
    return '\n'.join((line if re.match(r'^(?:'+ '|'.join(labels) + r')[：:]',line) else
                      re.sub(r'^[^\n:：]{1,40}: ', '', line)).strip() for line in text.splitlines()).strip()


def exact_time(raw: str) -> int | None:
    """A complete year/date/clock is required. A date alone has no invented 23:59."""
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?', raw):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace('Z','+00:00'))
        return int((dt if dt.tzinfo else dt.replace(tzinfo=TZ)).timestamp())
    except ValueError:
        return None


def describe(block: dict, course_groups: dict) -> dict:
    text = content(block.get('text') or '')
    topic = block.get('topic') or 'needs_review'
    fields = {}
    labels = {'标题':'title','主办方':'organizer','届次':'edition','时间':'start_raw',
              '截止':'deadline_raw','截止时间':'deadline_raw','地点':'location','资格':'eligibility',
              '费用':'cost','出行':'travel','技能':'skills','报名方式':'registration'}
    for line in text.splitlines():
        m = re.match(r'^([^：:]{1,8})[：:]\s*(.+)$', line)
        if m and m[1] in labels:
            fields[labels[m[1]]] = m[2].strip()
    gid = str(block['group_id'])
    course_source = gid in course_groups
    change = bool(re.search(r'停课|取消|改期|调课|教室.*(?:改|变)|(?:改|变).*(?:教室|地点|时间)|必须|务必', text))
    teaching = bool(re.search(r'上课|停课|调课|课时|教室|课程|考试|实验课|作业|交表|教务', text))
    mandatory = course_source and change and teaching
    action = bool(re.search(r'讲座|招募|招聘|实习|比赛|报名|征集|合作|截止|提交', text))
    if mandatory:
        category = 'course'
    elif course_source or topic == 'needs_review' or not text or '[图]' in text or '[合并转发]' in text:
        category = 'needs_review'
    elif action:
        # Negation/quotation must be reviewed, not asserted as a firm opportunity.
        category = 'needs_review' if re.search(r'不是|不需要|无需|别报名|据说|听说|[“「>]', text) else 'opportunity'
    elif topic in ('cs','bio'):
        category = 'knowledge'
    elif topic == 'chat':
        category = 'chat'
    else:
        category = 'needs_review'
    if topic == 'exploration':
        category = 'exploration'
    fields.setdefault('title', text.splitlines()[0][:100] if text else '[媒体公告待确认]')
    for key in KEY_FIELDS:
        fields.setdefault(key, None if key.endswith('_ts') else '')
    fields['start_ts'] = exact_time(fields['start_raw'])
    fields['deadline_ts'] = exact_time(fields['deadline_raw'])
    fields.update(category=category, mandatory=mandatory, confidence=1.0 if mandatory else 0.5,
                  relevance='生科/科研/合作/实践优先；适格性待核实', eligibility=fields['eligibility'] or '待确认',
                  text=text, timezone='Asia/Shanghai', cancelled=bool(re.search(r'取消|停课',text)),
                  source_links=re.findall(r'https?://[^\s<>]+', text),
                  images=json.loads(block.get('images') or '[]'), files=json.loads(block.get('files') or '[]'),
                  source_group=gid, reason='QQ群现有消息；未进行外网搜索')
    return fields


class EventStore:
    def __init__(self, db):
        self.db = db

    def ingest(self, block: dict, payload: dict, now: int | None = None) -> int:
        now = int(time.time()) if now is None else now
        if payload['category'] not in CATEGORIES:
            raise ValueError('Invalid category')
        stable = all(payload.get(k) for k in ('title','organizer','edition'))
        identity = digest([payload[k] for k in ('title','organizer','edition')]) if stable else digest(
            [content(payload['text']), payload.get('images', []), payload.get('files', [])])
        fp = digest({k:payload.get(k) for k in KEY_FIELDS})
        # Without explicit fields exact text is the conservative fingerprint.
        if not stable:
            fp = digest([fp, content(payload['text'])])
        with self.db._lock, self.db._conn:
            c = self.db._conn
            c.execute('BEGIN IMMEDIATE')
            old_source = c.execute('SELECT event_id FROM event_sources WHERE block_id=?',(block['id'],)).fetchone()
            if old_source:
                return old_source[0]
            row = c.execute('SELECT * FROM events WHERE identity_key=?',(identity,)).fetchone()
            changed = row is None or row['fingerprint'] != fp
            version = (row['version'] + 1 if changed else row['version']) if row else 1
            if row:
                eid = row['id']
                previous = json.loads(row['payload'])
                changes = {k: [previous.get(k),payload.get(k)] for k in KEY_FIELDS if previous.get(k) != payload.get(k)}
                if changed:
                    c.execute('UPDATE events SET version=?,category=?,mandatory=?,confidence=?,payload=?,fingerprint=?,updated_ts=? WHERE id=?',
                              (version,payload['category'],int(payload['mandatory']),payload['confidence'],json.dumps(payload,ensure_ascii=False),fp,now,eid))
            else:
                changes = {}
                eid = c.execute('INSERT INTO events(identity_key,version,category,mandatory,confidence,payload,fingerprint,first_seen,updated_ts) VALUES(?,?,?,?,?,?,?,?,?)',
                                (identity,1,payload['category'],int(payload['mandatory']),payload['confidence'],json.dumps(payload,ensure_ascii=False),fp,now,now)).lastrowid
            c.execute('INSERT INTO event_sources VALUES(?,?,?)',(block['id'],eid,version))
            if changed:
                c.execute('INSERT INTO event_versions VALUES(?,?,?,?,?)',(eid,version,json.dumps(payload,ensure_ascii=False),json.dumps(changes,ensure_ascii=False),now))
                c.execute('INSERT INTO audit(ts,entity,entity_id,action,detail) VALUES(?,?,?,?,?)',(now,'event',str(eid),'version',str(version)))
            return eid

    def import_blocks(self, floor: int = 0, course_groups: dict | None = None) -> int:
        n = 0
        while True:
            rows = self.db._query('''SELECT b.* FROM blocks b LEFT JOIN event_sources s ON b.id=s.block_id
                WHERE s.block_id IS NULL AND b.topic IS NOT NULL AND (b.created_ts>=? OR b.mail_pushed=1)
                ORDER BY b.id LIMIT 250''',(floor,))
            if not rows:
                return n
            for row in rows:
                b = dict(row)
                eid = self.ingest(b,describe(b,course_groups or {}),now=int(b['created_ts']))
                if b['mail_pushed']:
                    self.db.kv_set(f'event:legacy_sent:{eid}', '1')
                n += 1

    def due(self, event, now: int, mode: str) -> list[str]:
        e = dict(event)
        p = json.loads(e['payload'])
        initial_key = f"event:{e['id']}:v{e['version']}:initial"
        sent = self.db._query("SELECT 1 FROM deliveries WHERE key=? AND state='sent'", (initial_key,))
        if e['version'] == 1 and self.db.kv_get(f"event:legacy_sent:{e['id']}"):
            sent = True
        deadline = p.get('deadline_ts')
        if e['category'] == 'chat':
            return []
        if not e['mandatory'] and (e['negative'] or e['status'] in ('completed','declined','archived')):
            return []
        expired = deadline is not None and deadline <= now
        urgent = deadline is not None and 0 < deadline - e['updated_ts'] < 86400
        immediate = e['mandatory'] or e['category'] == 'needs_review' or urgent
        initial_due = ((mode == 'immediate' and immediate) or
                       (mode == 'daily' and e['category'] in ('opportunity','needs_review')) or
                       (mode == 'weekly' and e['category'] in ('knowledge','exploration')))
        result = []
        if not sent and initial_due and (not expired or e['mandatory'] or e['category'] == 'needs_review'):
            result.append('initial')
        if (deadline is not None and not expired and not urgent and now >= deadline - 86400
                and e['status'] not in ('completed','declined','archived') and mode == 'immediate'):
            # At exactly 24h send one initial notification, not two envelopes.
            if result or not sent:
                if 'initial' not in result:
                    result.append('initial')
            else:
                first = self.db._query('SELECT sent_ts FROM deliveries WHERE key=?',(initial_key,))
                if not first or (first[0]['sent_ts'] or 0) < deadline - 86400:
                    result.append('reminder24h')
        return result

    def set_status(self, event_id: int, status: str) -> None:
        if status not in ('open','pending','registered','completed','declined','archived'):
            raise ValueError('Invalid task status')
        self.db._exec('UPDATE events SET status=? WHERE id=?',(status,event_id))
        self.db.audit('event',event_id,'status',status)
