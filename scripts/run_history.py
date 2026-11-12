"""Run audit history; never doubles as the notification outbox."""

import contextlib
import datetime
import json
import pathlib
import sqlite3
import time
import uuid

from product_filter import body_text
from deal_text import normalized


ACTIVE = ("collecting", "reviewing")


class RunBusy(RuntimeError):
    pass


def summary(report):
    metrics = report.get("metrics", {})
    return {"input_count": report.get("input_count", 0), "selected_count": report.get("selected_count", 0),
            "eligible_count": report.get("eligible_count", 0), "sent_count": report.get("sent_count", 0),
            "review_enabled": report.get("review_enabled", False), "review_degraded": report.get("review_degraded", False),
            "elapsed_seconds": report.get("total_elapsed_seconds", report.get("elapsed_seconds", 0)),
            "feed_counts": report.get("feed_counts", {}), "feed_errors": report.get("feed_errors", []),
            "errors": report.get("errors", []), "ai_requests": metrics.get("ai", {}).get("requests", 0),
            "total_tokens": metrics.get("ai", {}).get("total_tokens", 0),
            "cache_hits": sum(value for key, value in metrics.get("cache", {}).items() if key.endswith("_hit")),
            "rate_wait_seconds": round(metrics.get("source_network", {}).get("rate_wait_seconds", 0)
                                       + metrics.get("ai", {}).get("rate_wait_seconds", 0), 3)}


def display_rows(snapshot, report):
    reviewed = {str(row["source_id"]): row for row in report.get("items", [])}
    skipped = {(str(row.get("id")), normalized(row.get("title"))): row for row in report.get("skipped", [])}
    rows, seen = [], set()
    for index, item in enumerate(snapshot.get("items", [])):
        ident = str(item.get("id") or item.get("url") or "")
        result = reviewed.get(ident)
        skip = skipped.get((ident, normalized(item.get("title"))))
        # Repeated source IDs can occur in snapshots; only the reviewed copy passes.
        if ident in seen:
            result, skip = None, {"reason": "duplicate_message"}
        seen.add(ident)
        rows.append({"row_key": f"{ident}:{index}", "source_id": ident, "title": item.get("title") or "无标题",
                     "source_url": item.get("url"), "source_feed": item.get("source_feed"),
                     "published_at": item.get("published_at"), "source_content": body_text(item),
                     "rule_decision": "passed" if result else "filtered",
                     "decision": result.get("decision") if result else "filtered",
                     "reason": result.get("reason") if result else (skip or {}).get("reason", "not_selected"),
                     "is_product": result.get("is_product") if result else None,
                     "review_source": result.get("review_source", "disabled") if result else "not_reviewed",
                     "delivery_status": result.get("delivery_status") if result else "not_eligible",
                     "eligible_to_send": result.get("eligible_to_send", False) if result else False})
    for ident, row in reviewed.items():
        if ident not in seen:
            rows.append({"row_key": ident + ":queue", **row, "from_queue": True})
    return rows


class RunHistory:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
                phase TEXT NOT NULL, origin TEXT NOT NULL, lease_until REAL NOT NULL,
                config_revision TEXT, config_json TEXT NOT NULL, snapshot_json TEXT,
                report_json TEXT, summary_json TEXT, error TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS runs_active ON runs(phase,lease_until)")

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, config, revision, origin="cli", exclusive=False, timeout=180):
        ident, now = uuid.uuid4().hex, time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE runs SET phase='interrupted',finished_at=?,error='previous_run_interrupted'
                WHERE phase IN ('collecting','reviewing') AND lease_until<?""", (now, now))
            if exclusive and db.execute("SELECT 1 FROM runs WHERE phase IN ('collecting','reviewing')").fetchone():
                raise RunBusy("已有一轮任务正在运行，请等待完成")
            db.execute("INSERT INTO runs (id,started_at,phase,origin,lease_until,config_revision,config_json) VALUES (?,?,?,?,?,?,?)",
                       (ident, now, "collecting", origin, now + timeout + 60, revision, json.dumps(config, ensure_ascii=False)))
        return ident

    def stage(self, ident, phase, snapshot=None):
        with self.connection() as db:
            if snapshot is not None:
                db.execute("UPDATE runs SET phase=?,snapshot_json=? WHERE id=?", (phase, json.dumps(snapshot, ensure_ascii=False), ident))
            else:
                db.execute("UPDATE runs SET phase=? WHERE id=?", (phase, ident))

    def finish(self, ident, report=None, error=None):
        with self.connection() as db:
            db.execute("UPDATE runs SET phase=?,finished_at=?,report_json=?,summary_json=?,error=? WHERE id=?",
                       ("failed" if error else "completed", time.time(), json.dumps(report, ensure_ascii=False) if report else None,
                        json.dumps(summary(report), ensure_ascii=False) if report else None, error, ident))

    def list(self, limit=50):
        with self.connection() as db:
            result = db.execute("""SELECT id,started_at,finished_at,phase,origin,config_revision,summary_json,error,lease_until
                FROM runs ORDER BY started_at DESC LIMIT ?""", (min(max(1, limit), 100),)).fetchall()
        rows = []
        for row in result:
            item = dict(row)
            item["summary"] = json.loads(item.pop("summary_json") or "{}")
            if item["phase"] in ACTIVE and item["lease_until"] < time.time():
                item["phase"], item["error"] = "interrupted", "previous_run_interrupted"
            rows.append(item)
        return rows

    def get(self, ident):
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (ident,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("config", "snapshot", "report", "summary"):
            result[key] = json.loads(result.pop(key + "_json") or "{}")
        result["rows"] = display_rows(result["snapshot"], result["report"])
        if result["phase"] in ACTIVE and result["lease_until"] < time.time():
            result["phase"], result["error"] = "interrupted", "previous_run_interrupted"
        return result


def captured_time(run):
    value = run.get("snapshot", {}).get("captured_at")
    if value:
        try:
            return datetime.datetime.fromisoformat(value).timestamp()
        except (ValueError, TypeError):
            pass
    return run["started_at"]
