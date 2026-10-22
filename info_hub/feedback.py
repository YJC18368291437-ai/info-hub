"""Mailbox state is mutable. Only the dedicated folder supplies negative preference."""
import time


def apply_observations(db, located):
    changed = []
    # All versions/copies of an event are resolved together; INBOX wins.
    by_event = {}
    priority = {'inbox':6,'uninterested':5,'archive':4,'other':3,'trash':2,'junk':1}
    for mid,(kind,flagged,eid,version) in located.items():
        if eid is None and mid.startswith('legacy-block:'):
            rows = db._query('SELECT event_id,version FROM event_sources WHERE block_id=?',(int(mid.split(':')[1]),))
            if rows:
                eid,version = rows[0]
        if eid is None:
            continue  # includes read-only multi-item weekly mail
        old = by_event.get(eid)
        candidate = (kind,flagged,version or 0)
        if old and (priority.get(old[0],0),old[2],old[1]) >= (priority.get(kind,0),version or 0,flagged):
            continue
        by_event[eid] = candidate
    for eid,(kind,flagged,version) in by_event.items():
        rows = db._query('SELECT * FROM events WHERE id=?',(eid,))
        if not rows:
            continue
        e = rows[0]
        status = e['status']
        if flagged:
            status = 'pending' if status != 'registered' else status
        elif kind == 'archive':
            status = 'archived'
        elif kind == 'inbox' and status in ('pending','archived'):
            status = 'open'
        negative = int(kind == 'uninterested')
        if status == e['status'] and negative == e['negative']:
            continue
        with db._lock, db._conn:
            db._conn.execute('UPDATE events SET status=?,negative=? WHERE id=?',(status,negative,eid))
            # Close queued reminders, but never erase an ambiguous transport write.
            if status in ('archived','completed','declined') or negative:
                db._conn.execute("UPDATE deliveries SET state='cancelled',stage='user_state' WHERE event_id=? AND kind='reminder24h' AND state IN ('queued','retryable')",(eid,))
            db._conn.execute('INSERT INTO audit(ts,entity,entity_id,action,detail) VALUES(?,?,?,?,?)',
                (int(time.time()),'event',str(eid),'mail_state',f'{kind};flag={int(flagged)};status={status};negative={negative}'))
        changed.append(eid)
    return changed
