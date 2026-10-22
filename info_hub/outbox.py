"""Cross-process atomic claims. An ambiguous write is NEVER blindly retried."""
import base64
import asyncio
import hashlib
import json
import time
import uuid
from email import message_from_bytes, policy


class DeliveryError(Exception):
    def __init__(self, stage: str, unknown: bool = False):
        super().__init__(stage)
        self.stage, self.unknown = stage, unknown


class Outbox:
    def __init__(self, db, mailer):
        self.db, self.mailer = db, mailer
        self.owner = uuid.uuid4().hex

    def queue(self, key, message, events=(), kind='initial', newspaper_manifest=None):
        mid = '<' + hashlib.sha256(key.encode()).hexdigest() + '@info-hub.local>'
        if message.get('Message-ID'):
            message.replace_header('Message-ID',mid)
        else:
            message['Message-ID'] = mid
        message['X-InfoHub-Delivery'] = key
        encoded = base64.b64encode(message.as_bytes()).decode('ascii')
        with self.db._lock, self.db._conn:
            cur = self.db._conn.execute('INSERT OR IGNORE INTO deliveries(key,message_id,kind,payload,event_id,version) VALUES(?,?,?,?,?,?)',
                                        (key,mid,kind,encoded,events[0][0] if len(events)==1 else None,events[0][1] if len(events)==1 else None))
            if cur.rowcount == 1 and newspaper_manifest is not None:
                manifest_kind = str(newspaper_manifest["kind"])
                ids = newspaper_manifest["ids"]
                cursor = newspaper_manifest["cursor"]
                self.db._conn.execute(
                    "INSERT INTO newspaper_delivery_manifest(delivery_key,kind,ids_json,cursor_json,finalized) "
                    "VALUES(?,?,?,?,0)",
                    (key, manifest_kind, json.dumps(ids, separators=(",", ":")),
                     json.dumps(cursor, separators=(",", ":"))))
            for eid, ver in events:
                self.db._conn.execute('INSERT OR IGNORE INTO delivery_events VALUES(?,?,?)',(key,eid,ver))
        return key

    def claim(self, key, now=None):
        now = int(time.time()) if now is None else now
        with self.db._lock, self.db._conn:
            c = self.db._conn
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE deliveries SET state='unknown',stage='expired_claim' WHERE state='claimed' AND lease_until<=?",(now,))
            stale = c.execute('''SELECT 1 FROM delivery_events d JOIN events e ON e.id=d.event_id
                JOIN deliveries m ON m.key=d.delivery_key WHERE d.delivery_key=? AND
                (e.version!=d.version OR
                 (m.kind='reminder24h' AND e.status IN ('completed','declined','archived')) OR
                 (e.mandatory=0 AND (e.negative=1 OR e.status IN ('completed','declined','archived')))) LIMIT 1''',(key,)).fetchone()
            if stale:
                c.execute("UPDATE deliveries SET state='cancelled',stage='stale_event' WHERE key=? AND state IN ('queued','retryable')",(key,))
                return False
            cur = c.execute("""UPDATE deliveries SET state='claimed',owner=?,lease_until=?,attempts=attempts+1
                 WHERE key=? AND state IN ('queued','retryable') AND next_retry<=?""",(self.owner,now+600,key,now))
            return cur.rowcount == 1

    def sent(self, key):
        with self.db._lock, self.db._conn:
            c = self.db._conn
            c.execute("UPDATE deliveries SET state='sent',sent_ts=?,stage='confirmed',owner=NULL WHERE key=?",(int(time.time()),key))
            c.execute('''UPDATE blocks SET mail_pushed=1 WHERE id IN
              (SELECT s.block_id FROM event_sources s JOIN delivery_events d ON d.event_id=s.event_id
                 WHERE d.delivery_key=? AND s.version<=d.version)''',(key,))
            c.execute('INSERT INTO audit(ts,entity,entity_id,action,detail) VALUES(?,?,?,?,?)',
                      (int(time.time()),'delivery',key,'sent','confirmed'))

    async def send(self, key):
        if not self.claim(key):
            return 0
        row = self.db._query('SELECT * FROM deliveries WHERE key=?',(key,))[0]
        try:
            msg = message_from_bytes(base64.b64decode(row['payload']),policy=policy.default)
            n = await self.mailer.deliver([msg])
            if n != 1:
                raise DeliveryError('transport_unconfirmed',unknown=True)
            self.sent(key)
            return 1
        except asyncio.CancelledError:
            self.db._exec("UPDATE deliveries SET state='unknown',stage='cancelled_during_transport' WHERE key=?",(key,))
            raise
        except Exception as exc:
            # Unknown exceptions (including DB failure AFTER APPEND) are ambiguous.
            unknown = not isinstance(exc,DeliveryError) or exc.unknown
            state = 'unknown' if unknown else ('dead-letter' if row['attempts'] >= 5 else 'retryable')
            self.db._exec("UPDATE deliveries SET state=?,stage=?,error=?,next_retry=?,owner=NULL WHERE key=?",
                          (state,getattr(exc,'stage','send_or_commit'),type(exc).__name__,int(time.time())+min(3600,30*2**row['attempts']),key))
            self.db.audit('delivery',key,state,type(exc).__name__)
            return 0

    async def reconcile(self):
        """Positive evidence can confirm unknown writes; absence is not proof of failure."""
        import asyncio
        if not hasattr(self.mailer,'fetcher'):
            return 0
        located = await asyncio.to_thread(self.mailer.fetcher.locate_messages)
        n = 0
        for row in self.db._query("SELECT key,message_id FROM deliveries WHERE state='unknown'"):
            if row['message_id'] in located:
                self.sent(row['key'])
                n += 1
        return n
