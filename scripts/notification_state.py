"""Transactional notification outbox, independent of model review caches."""

import contextlib
import hashlib
import json
import pathlib
import sqlite3
import time
from urllib.parse import urlparse

from deal_text import normalized
from product_filter import body_text


def message_record(item):
    host = urlparse(item.get("url") or "").hostname or "xianbao"
    identity = host + ":" + str(item.get("id") or item.get("url") or "")
    text = normalized(item.get("title")) + "\n" + body_text(item)
    fingerprint = hashlib.sha256(text.encode()).hexdigest()
    key = hashlib.sha256((identity + "\n" + fingerprint).encode()).hexdigest()
    return {"key": key, "identity": identity, "fingerprint": fingerprint, "item": item}


class NotificationState:
    def __init__(self, path, writable=True, clock=time.time):
        self.path, self.writable, self.clock = pathlib.Path(path), writable, clock
        if writable:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
        elif self.path.is_file():
            self.db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10, isolation_level=None)
        else:
            self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if writable or not self.path.is_file():
            self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS notifications (
                key TEXT PRIMARY KEY, identity TEXT NOT NULL, fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL, owner TEXT, lease_until REAL DEFAULT 0,
                attempts INTEGER DEFAULT 0, retry_at REAL DEFAULT 0, sent_at REAL,
                updated_at REAL NOT NULL, last_error TEXT)""")
            self.db.execute("CREATE INDEX IF NOT EXISTS notification_fingerprints ON notifications(fingerprint, status)")

    @contextlib.contextmanager
    def transaction(self):
        if not self.writable:
            raise RuntimeError("notification_state_read_only")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def initialized(self):
        return bool(self.db.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone())

    def enqueue(self, records, baseline=False):
        with self.transaction():
            now = self.clock()
            for record in records:
                self.db.execute("""UPDATE notifications SET status='superseded', owner=NULL, lease_until=0
                    WHERE identity=? AND key<>? AND status IN ('pending','failed','processing')""",
                                (record["identity"], record["key"]))
                self.db.execute("""INSERT INTO notifications
                    (key,identity,fingerprint,payload,status,updated_at) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,
                    status=CASE WHEN notifications.status='superseded' THEN 'pending' ELSE notifications.status END""",
                                (record["key"], record["identity"], record["fingerprint"],
                                 json.dumps(record["item"], ensure_ascii=False), "baseline" if baseline else "pending", now))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('initialized',?)", (str(now),))

    def queued(self, limit=500):
        rows = self.db.execute("""SELECT payload FROM notifications
            WHERE status IN ('pending','failed','processing') ORDER BY updated_at DESC LIMIT ?""", (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def _block_reason(self, record, dedup_seconds, max_attempts):
        now = self.clock()
        row = self.db.execute("SELECT * FROM notifications WHERE key=?", (record["key"],)).fetchone()
        if row:
            if row["status"] in ("sent", "baseline"):
                return "already_sent" if row["status"] == "sent" else "baseline_suppressed"
            if row["status"] in ("sending", "delivery_unknown"):
                return "delivery_unconfirmed"
            if row["status"] == "processing" and row["lease_until"] > now:
                return "in_progress"
            if row["status"] == "superseded":
                return "superseded"
            if row["attempts"] >= max_attempts:
                return "retry_exhausted"
            if row["retry_at"] > now:
                return "retry_not_due"
        duplicate = self.db.execute("""SELECT status FROM notifications WHERE fingerprint=? AND key<>? AND (
            (status IN ('sent','baseline') AND COALESCE(sent_at,updated_at)>=?) OR
            (status='processing' AND lease_until>?) OR status IN ('sending','delivery_unknown')) LIMIT 1""",
                                    (record["fingerprint"], record["key"], now - dedup_seconds, now)).fetchone()
        if duplicate:
            return "duplicate_content" if duplicate["status"] in ("sent", "baseline") else "content_in_progress"
        return None

    def block_reason(self, record, dedup_seconds, max_attempts):
        return self._block_reason(record, dedup_seconds, max_attempts)

    def claim(self, record, owner, lease_seconds, dedup_seconds, max_attempts):
        with self.transaction():
            reason = self._block_reason(record, dedup_seconds, max_attempts)
            if reason:
                return reason
            updated = self.db.execute("""UPDATE notifications SET status='processing',owner=?,lease_until=?,updated_at=?
                WHERE key=?""", (owner, self.clock() + lease_seconds, self.clock(), record["key"]))
            if updated.rowcount != 1:
                raise RuntimeError("notification_not_enqueued")
        return None

    def release(self, key, owner, delay=0, error=None):
        with self.transaction():
            self.db.execute("""UPDATE notifications SET status='pending',owner=NULL,lease_until=0,
                retry_at=?,updated_at=?,last_error=? WHERE key=? AND owner=? AND status='processing'""",
                            (self.clock() + delay, self.clock(), error, key, owner))

    def begin_send(self, key, owner, lease_seconds):
        with self.transaction():
            cursor = self.db.execute("""UPDATE notifications SET status='sending',attempts=attempts+1,
                lease_until=?,updated_at=? WHERE key=? AND owner=? AND status='processing' AND lease_until>?""",
                                     (self.clock() + lease_seconds, self.clock(), key, owner, self.clock()))
            return cursor.rowcount == 1

    def sent(self, key, owner):
        with self.transaction():
            cursor = self.db.execute("""UPDATE notifications SET status='sent',sent_at=?,updated_at=?,owner=NULL,
                lease_until=0,last_error=NULL WHERE key=? AND owner=? AND status='sending'""",
                                     (self.clock(), self.clock(), key, owner))
            if cursor.rowcount != 1:
                raise RuntimeError("notification_send_lease_lost")

    def failed(self, key, owner, error, delay, uncertain=False):
        with self.transaction():
            self.db.execute("""UPDATE notifications SET status=?,owner=NULL,lease_until=0,retry_at=?,
                updated_at=?,last_error=? WHERE key=? AND owner=? AND status='sending'""",
                            ("delivery_unknown" if uncertain else "failed", self.clock() + delay,
                             self.clock(), error, key, owner))

    def status_counts(self):
        return {row["status"]: row["count"] for row in self.db.execute("SELECT status,COUNT(*) AS count FROM notifications GROUP BY status")}

    def close(self):
        self.db.close()
