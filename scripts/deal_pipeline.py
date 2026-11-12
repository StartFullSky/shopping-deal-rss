"""Program-owned collection, rules, outbox, optional AI review and delivery."""

import argparse
import json
import pathlib
import sys
import time
import uuid
from urllib.parse import urlparse

from deal_config import DELIVERY_DEFAULTS, load_config, revision, settings
from notification_delivery import BarkSender, DeliveryError
from notification_state import NotificationState, message_record
from product_filter import body_text, run as review_products, select_items


ROOT = pathlib.Path(__file__).resolve().parents[1]


def allowed_source(item, config):
    uri = urlparse(item.get("url") or "")
    category = item.get("source_feed") or uri.path.strip("/").split("/")[0]
    if uri.hostname != "new.xianbao.fun" or category not in config["feed_categories"]:
        return False
    expected = config.get("feed_category_ids", {}).get(category)
    returned = item.get("category")
    return not isinstance(returned, dict) or expected is None or str(returned.get("id")) == str(expected)


def notification(item):
    title = str(item.get("title") or "商品线报").strip()[:128]
    url = item.get("url") or ""
    if len(url) > 2000 or urlparse(url).scheme not in ("https", "http"):
        url = ""
    footer = "\n原文：" + url if url else ""
    budget = max(0, 5900 - len(footer.encode()))
    body = body_text(item).encode()[:budget].decode("utf-8", "ignore").strip()
    return {"title": title, "body": (body or title) + footer, "url": url}


def select_local(items, config, now_ts=None):
    scoped, rejected = [], []
    for item in items:
        if allowed_source(item, config):
            scoped.append(item)
        else:
            rejected.append({"id": str(item.get("id")), "title": item.get("title"), "reason": "source_not_allowed"})
    chosen, skipped = select_items(scoped, config, now_ts=now_ts)
    return chosen, rejected + skipped


def run(items, config, cache_path, state_path=None, sender=None, review_client=None, dry_run=False, deadline=None):
    started = time.monotonic()
    deadline = deadline or started + config["batch_timeout_seconds"]
    delivery = {**DELIVERY_DEFAULTS, **config.get("delivery", {})}
    live = bool(delivery["enabled"]) and not dry_run
    state_path = state_path or delivery["state_path"]
    state = NotificationState(state_path, writable=live)
    owner = uuid.uuid4().hex
    claimed, rows, errors, previews = [], [], [], []
    review_enabled = bool(config.get("ai", {}).get("enabled", False))
    report = {"mode": "product_only", "status": "completed" if live else "preview", "input_count": len(items),
              "selected_count": 0, "items": rows, "skipped": [], "decisions": {}, "errors": errors,
              "review_enabled": review_enabled, "review_degraded": False, "model": None,
              "price_lookup": "disabled", "discount_gate": "disabled", "sent_count": 0,
              "attempted_count": 0, "eligible_count": 0, "would_send": previews,
              "notification_delivery": "enabled" if live else "disabled", "metrics": {"ai": {}, "cache": {}, "batches": 0}}
    try:
        # The review limit applies after persistent deduplication, so already-sent
        # rows at the top of a feed cannot hide later, unsent messages.
        fresh, skipped = select_local(items, {**config, "max_items": max(len(items), config["max_items"])})
        report["skipped"].extend(skipped)
        records = [message_record(item) for item in fresh]
        if live and not state.initialized() and delivery["cold_start"] == "baseline":
            if not records:
                report["status"] = "baseline_waiting_for_data"
                return report
            state.enqueue(records, baseline=True)
            report.update(status="initialized_baseline", baseline_count=len(records))
            return report
        if live:
            state.enqueue(records)
        pool = {message_record(i)["identity"]: i for i in state.queued()}
        pool.update({r["identity"]: r["item"] for r in records})
        candidates, queued_skipped = select_local(list(pool.values()), {**config, "max_items": max(len(pool), config["max_items"])})
        report["skipped"].extend(queued_skipped)
        accepted = []
        for item in candidates:
            record = message_record(item)
            if len(accepted) >= config["max_items"]:
                report["skipped"].append({"id": str(item["id"]), "title": item.get("title"), "reason": "batch_item_limit"})
                continue
            if live:
                reason = state.claim(record, owner, delivery["lease_seconds"], delivery["dedup_seconds"], delivery["max_attempts"])
            else:
                reason = state.block_reason(record, delivery["dedup_seconds"], delivery["max_attempts"])
            if reason:
                report["skipped"].append({"id": str(item["id"]), "title": item.get("title"), "reason": reason})
            else:
                accepted.append(item)
                if live:
                    claimed.append(record["key"])
        report["selected_count"] = len(accepted)
        verdicts = {}
        if review_enabled and accepted:
            try:
                review = review_products(accepted, config, cache_path, client=review_client, deadline=deadline)
                verdicts = {row["source_id"]: row for row in review["items"]}
                report["metrics"] = review["metrics"]
                report["model"] = review["model"]
                errors.extend(review["errors"])
            except Exception as error:
                # Configuration/transport failures must not stop the business pipeline.
                errors.append("review_unavailable_" + type(error).__name__)
        eligible = []
        for item in accepted:
            ident = str(item["id"])
            record = message_record(item)
            review = verdicts.get(ident)
            row = {"source_id": ident, "title": item.get("title"), "source_url": item.get("url"),
                   "source_feed": item.get("source_feed"), "source_category": item.get("category"),
                   "source_content": body_text(item)[:1400], "published_at": item.get("published_at"),
                   "rule_decision": "passed", "is_product": None, "product_name": None,
                   "review_source": "disabled", "decision": "rule_eligible", "reason": "local_rules_passed",
                   "eligible_to_send": True, "delivery_status": "queued"}
            if review_enabled:
                if review:
                    row.update({key: review[key] for key in ("is_product", "product_name", "review_source", "decision", "reason")})
                if not review or review["is_product"] is None:
                    is_error = not review or review.get("review_source") == "unreviewed"
                    policy = config["ai"].get("on_error" if is_error else "on_unknown", "bypass")
                    row.update(decision="rule_eligible" if policy == "bypass" else "deferred",
                               review_source="fallback" if is_error else "model_unknown",
                               reason=("ai_unavailable_local_rules_passed" if is_error else "ai_unknown_local_rules_passed")
                                      if policy == "bypass" else ("ai_unavailable_deferred" if is_error else "ai_unknown_deferred"),
                               eligible_to_send=policy == "bypass")
                    report["review_degraded"] = True
                elif review["is_product"] is False:
                    row["eligible_to_send"] = False
            rows.append(row)
            if row["eligible_to_send"]:
                eligible.append((item, record, row))
            else:
                row["delivery_status"] = "ai_rejected" if row["is_product"] is False else "review_deferred"
                if live:
                    state.release(record["key"], owner, error=row["delivery_status"])
        report["eligible_count"] = len(eligible)
        if not live:
            for _, _, row in eligible:
                row["delivery_status"] = "preview"
            previews.extend(notification(item) for item, _, _ in eligible[:delivery["max_per_run"]])
            return report
        if eligible and sender is None:
            try:
                sender = BarkSender({**config, "delivery": delivery})
            except Exception as error:
                errors.append("delivery_configuration_" + type(error).__name__)
                for _, _, row in eligible:
                    row["delivery_status"] = "configuration_error"
                return report
        for item, record, row in eligible:
            if report["attempted_count"] >= delivery["max_per_run"] or time.monotonic() >= deadline:
                break
            if not select_local([item], config)[0]:
                row["delivery_status"] = "rules_changed_or_expired"
                continue
            if not state.begin_send(record["key"], owner, delivery["lease_seconds"]):
                row["delivery_status"] = "lease_lost"
                continue
            report["attempted_count"] += 1
            try:
                receipt = sender.send(notification(item))
                if not isinstance(receipt, dict) or receipt.get("confirmed") is not True:
                    raise DeliveryError("delivery_ack_unconfirmed", uncertain=True)
            except DeliveryError as error:
                row["delivery_status"] = "delivery_unknown" if error.uncertain else "failed"
                state.failed(record["key"], owner, str(error), delivery["retry_delay_seconds"], error.uncertain)
                errors.append(str(error))
                continue
            except Exception as error:
                row["delivery_status"] = "delivery_unknown"
                state.failed(record["key"], owner, "delivery_unconfirmed_" + type(error).__name__, 0, uncertain=True)
                errors.append("delivery_unconfirmed_" + type(error).__name__)
                continue
            try:
                state.sent(record["key"], owner)
            except Exception:
                row["delivery_status"] = "delivery_unknown"
                errors.append("delivery_confirmation_state_write_failed")
                break
            row["delivery_status"] = "sent"
            report["sent_count"] += 1
        return report
    finally:
        for key in claimed:
            state.release(key, owner)
        from collections import Counter
        report["decisions"] = dict(Counter(row["decision"] for row in rows))
        report["delivery_states"] = state.status_counts()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        state.close()


def execute(config, cache_path, snapshot=None, state_path=None, dry_run=False,
            history=None, run_id=None, config_revision=None, output=None):
    """One shared execution path for the CLI, monitor and local dashboard."""
    from price_batch import collect, now, write_json
    from price_lookup import Http
    started = time.monotonic()
    deadline = started + config["batch_timeout_seconds"]
    if history and run_id is None:
        run_id = history.begin({"settings": settings(config), "sources": config["feed_categories"],
                                "delivery_enabled": bool(config.get("delivery", {}).get("enabled")) and not dry_run},
                               config_revision, timeout=config["batch_timeout_seconds"])
    try:
        source_http = Http(config, deadline)
        if snapshot is None:
            snapshot = collect(source_http)
        if history:
            history.stage(run_id, "reviewing", snapshot)
        if output:
            write_json(output.with_suffix(".input.json"), snapshot)
        result = run(snapshot["items"], config, cache_path, state_path=state_path, dry_run=dry_run, deadline=deadline)
        result.update(finished_at=now(), input_captured_at=snapshot.get("captured_at"), feed_categories=snapshot.get("feed_categories", []),
                      feed_counts=snapshot.get("feed_counts", {}), feed_errors=snapshot.get("feed_errors", []),
                      total_elapsed_seconds=round(time.monotonic() - started, 3))
        result["metrics"]["source_network"] = dict(source_http.stats)
        if output:
            write_json(output, result)
        if history:
            history.finish(run_id, result)
        return result
    except Exception as error:
        if history:
            history.finish(run_id, error="run_failed_" + type(error).__name__)
        raise


def main(argv=None):
    from price_batch import read_input
    from run_history import RunHistory
    parser = argparse.ArgumentParser(description="Program-owned deal delivery with optional AI review")
    parser.add_argument("--config", type=pathlib.Path, default=ROOT / "product-filter-config.json")
    parser.add_argument("--monitor-config", type=pathlib.Path)
    parser.add_argument("--input", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--cache", type=pathlib.Path, default=ROOT / ".price-cache" / "products.sqlite3")
    parser.add_argument("--state", type=pathlib.Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-stdout", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config, args.monitor_config)
    if args.no_ai:
        config.setdefault("ai", {})["enabled"] = False
    if args.limit is not None:
        config["max_items"] = args.limit
    if not 1 <= config["max_items"] <= 100 or not 1 <= config["delivery"]["max_per_run"] <= 100:
        parser.error("item or notification limit out of range")
    if config["delivery"]["cold_start"] not in ("baseline", "send"):
        parser.error("delivery.cold_start must be baseline or send")
    for policy in ("on_error", "on_unknown"):
        if config.get("ai", {}).get(policy, "bypass") not in ("bypass", "defer"):
            parser.error("AI fallback policies must be bypass or defer")
    snapshot = None
    if args.input:
        raw = json.loads(args.input.read_text(encoding="utf-8-sig"))
        snapshot = {"input_file": str(args.input.resolve()), "items": read_input(args.input)}
        if isinstance(raw, dict):
            snapshot.update({k: raw[k] for k in ("captured_at", "feed_categories", "feed_counts", "feed_errors") if k in raw})
    result = execute(config, args.cache, snapshot, args.state, args.dry_run,
                     RunHistory(config["history_path"]), config_revision=revision(args.config, args.monitor_config), output=args.output)
    print(json.dumps(result if args.json_stdout else {k: result[k] for k in
          ("status", "mode", "selected_count", "decisions", "review_enabled", "review_degraded", "eligible_count", "sent_count", "errors", "metrics", "total_elapsed_seconds")}, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
