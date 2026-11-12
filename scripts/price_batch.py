import argparse
import collections
import concurrent.futures
import datetime
import hashlib
import json
import pathlib
import re
import statistics
import sys
import time
from decimal import Decimal, ROUND_HALF_UP

from deal_text import deal_facts, grams, identity_title, models, normalized, price_values, quantity
from price_lookup import Budget, Cache, Http, Lookup, LookupStopped
from xianbao_history import XianbaoHistoryLookup


ROOT = pathlib.Path(__file__).resolve().parent.parent
TZ = datetime.timezone(datetime.timedelta(hours=8))


def now():
    return datetime.datetime.now(TZ).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_input(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        return data
    return data.get("items", data.get("data", {}).get("items", []))


def collect(http):
    from urllib.parse import urlencode
    captured = now()
    config = http.config
    categories = config.get("feed_categories", ["kuan", "weibo-chaohua"])
    limit = config.get("feed_limit", 20)
    if (not isinstance(categories, list) or not categories
            or any(not isinstance(category, str) or not category.strip() for category in categories)):
        raise ValueError("feed_categories_must_be_nonempty_alias_list")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("feed_limit_must_be_between_1_and_20")
    categories = list(dict.fromkeys(category.strip() for category in categories))
    items, failures = [], []
    counts = {}
    for category in categories:
        counts[category] = 0
        url = "https://new.xianbao.fun/api/v1/feed?" + urlencode({"cate": category, "limit": limit, "with_html": 1})
        try:
            payload = json.loads(http.get(url))
            if payload.get("code") not in (0, 200):
                raise LookupStopped("feed_business_error")
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise LookupStopped("feed_invalid_items")
            rows = data["items"]
            expected_id = config.get("feed_category_ids", {}).get(category)
            dropped = 0
            for row in rows:
                if not isinstance(row, dict):
                    raise LookupStopped("feed_invalid_row")
                row_category = row.get("category")
                returned_id = row_category.get("id") if isinstance(row_category, dict) else None
                if expected_id is not None and str(returned_id) != str(expected_id):
                    dropped += 1
                    continue
                items.append({**row, "source_feed": category})
                counts[category] += 1
            if dropped:
                failures.append({"feed": category, "error": "feed_category_mismatch", "dropped_count": dropped})
        except (LookupStopped, ValueError) as error:
            failures.append({"feed": category, "error": str(error)})
    return {"captured_at": captured, "feed_categories": categories, "feed_counts": counts,
            "items": items, "feed_errors": failures}


def select(items, config):
    selected, skipped, seen, semantic_seen = [], [], set(), []
    for item in sorted(items, key=lambda row: row.get("published_at", 0), reverse=True):
        title = normalized(item.get("title"))
        text = title + " " + normalized(item.get("content"))
        reason = None
        if item.get("published_at") and time.time() - float(item["published_at"]) > config["max_age_minutes"] * 60:
            reason = "stale"
        elif config["include_keywords"] and not any(word in text for word in config["include_keywords"]):
            reason = "include_filter"
        elif any(word in text for word in config["exclude_keywords"]):
            reason = "keyword_excluded"
        elif any(re.search(pattern, text, re.I) for pattern in config["exclude_patterns"]):
            reason = "pattern_excluded"
        elif item.get("price") is None and not price_values(text + " " + normalized(re.sub(r"<[^>]*>", " ", str(item.get("content_html") or "")))):
            reason = "price_missing"
        key = hashlib.sha256(identity_title(title).encode()).hexdigest()
        if key in seen:
            reason = "duplicate_product_in_batch"
        tokens = grams(title)
        size = quantity(title)
        for previous, price, previous_size, previous_models in semantic_seen:
            overlap = len(tokens & previous) / max(1, min(len(tokens), len(previous)))
            same_size = (size["amount"], size["unit"]) == (previous_size["amount"], previous_size["unit"])
            if item.get("price") == price and overlap >= .78 and same_size and models(title) == previous_models:
                reason = "duplicate_product_in_batch"
                break
        if reason:
            skipped.append({"id": str(item.get("id")), "title": title, "reason": reason})
            continue
        seen.add(key)
        if len(selected) >= config["max_items"]:
            skipped.append({"id": str(item.get("id")), "title": title, "reason": "batch_item_limit"})
            continue
        selected.append(item)
        semantic_seen.append((tokens, item.get("price"), size, models(title)))
    return selected, skipped


def evaluate(item, config, lookup, deadline):
    started = time.monotonic()
    facts = deal_facts(item)
    result = {"source_id": str(item.get("id")), "title": item.get("title"), "source_url": item.get("url"), "source_content": normalized(item.get("content") or item.get("excerpt"))[:1400], "published_at": item.get("published_at"), "facts": facts, "decision": "needs_review", "searches": 0, "pages": 0, "network_requests": 0}
    budget = Budget(config, deadline)
    try:
        if facts["status"] != "ready":
            result["reason"] = facts["status"]
            return result
        value = lookup.run_item(item, facts, budget) if isinstance(lookup, XianbaoHistoryLookup) else lookup.run(facts, budget)
        result.update(value)
        reference = result.get("reference")
        if reference:
            history_mode = reference.get("evidence_type") == "xianbao_history"
            threshold = (100 - Decimal(str(config["min_discount_pct"]))) / 100 if history_mode else Decimal(config["max_price_ratio"])
            passes = Decimal(reference["ratio"]) <= threshold
            result["price_gate_passed"] = passes
            result["discount_pct"] = str(((1 - Decimal(reference["ratio"])) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            result["discount_zhe"] = str((Decimal(reference["ratio"]) * 10).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            if passes:
                result["decision"] = "estimated_candidate" if reference.get("requires_review") or reference["match"] == "category_estimate" else "conditional_candidate" if facts["conditions"] else "candidate"
            else:
                result["decision"] = "not_below_threshold"
            result["reference_label"] = reference["reference_label"] if history_mode else "值得买搜索摘要估价，未核实实时售价" if reference.get("evidence_type") == "search_snippet" else "值得买展示参考价，非结算核验价"
        else:
            result["reason"] = value.get("reason") or "reference_unavailable_or_spec_mismatch"
            if any(any(term in error for term in ("provider_", "challenge", "http_")) for error in value.get("errors", [])):
                result.update(decision="deferred", reason="reference_provider_unavailable")
    except LookupStopped as error:
        result.update(decision="deferred", reason=str(error))
    except Exception as error:
        # A malformed record must not abort the rest of the batch; keep a named error.
        result.update(decision="error", reason=type(error).__name__ + ": " + str(error)[:160])
    finally:
        result.update(searches=budget.searches, pages=budget.pages, network_requests=budget.network, elapsed_seconds=round(time.monotonic() - started, 3), stages={key: round(value, 4) for key, value in budget.timings.items()})
    return result


def run(items, config, cache_path, http=None, deadline=None):
    started = time.monotonic()
    deadline = deadline or started + config["batch_timeout_seconds"]
    http = http or Http(config, deadline)
    cache = Cache(cache_path)
    http.cache = cache
    lookup = XianbaoHistoryLookup(config, cache, http) if config.get("price_backend") == "xianbao_history" else Lookup(config, cache, http)
    chosen, skipped = select(items, config)
    completed = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=config["workers"]) as pool:
            pending = {pool.submit(evaluate, item, config, lookup, deadline): index for index, item in enumerate(chosen)}
            for future in concurrent.futures.as_completed(pending):
                result = future.result()
                result["input_index"] = pending[future]
                completed.append(result)
                # JSON Lines on stderr lets an orchestrator consume early results immediately.
                print(json.dumps({"event": "item_done", "id": result["source_id"], "decision": result["decision"], "seconds": result["elapsed_seconds"]}, ensure_ascii=False), file=sys.stderr, flush=True)
        completed.sort(key=lambda row: row["input_index"])
        durations = sorted(item["elapsed_seconds"] for item in completed)
        counts = collections.Counter(item["decision"] for item in completed)
        priced = [item for item in completed if item.get("reference")]
        coverage = {
            "with_comparable_price": len(priced),
            "from_search_snippet": sum(item["reference"].get("evidence_type") == "search_snippet" for item in priced),
            "from_product_page": sum(item["reference"].get("evidence_type") == "product_page" for item in priced),
            "from_xianbao_history": sum(item["reference"].get("evidence_type") == "xianbao_history" for item in priced),
            "items_with_priced_snippets": sum(item.get("snippets_with_prices", 0) > 0 for item in completed),
            "price_gate_passed": sum(bool(item.get("price_gate_passed")) for item in completed),
            "without_comparable_price": len(completed) - len(priced),
        }
        return {"finished_at": now(), "elapsed_seconds": round(time.monotonic() - started, 3), "input_count": len(items), "selected_count": len(chosen), "price_backend": config.get("price_backend", "smzdm"), "min_discount_pct": config.get("min_discount_pct") if config.get("price_backend") == "xianbao_history" else None, "decisions": dict(counts), "price_coverage": coverage, "stopped_sources": dict(http.stopped_sources), "metrics": {"network": dict(http.stats), "cache": dict(cache.stats), "item_median_seconds": statistics.median(durations) if durations else 0, "item_p95_seconds": durations[min(len(durations) - 1, int(len(durations) * .95))] if durations else 0}, "limits": {key: config[key] for key in ("workers", "max_searches_per_item", "max_pages_per_item", "max_requests_per_batch", "item_timeout_seconds", "batch_timeout_seconds", "search_interval_seconds", "wiki_interval_seconds")}, "model_review": "not_invoked", "notification_delivery": "not_invoked", "items": completed, "skipped": skipped}
    finally:
        cache.close()


def main():
    parser = argparse.ArgumentParser(description="Deal pipeline with optional AI review; explicit --backend selects legacy price checks")
    parser.add_argument("--config", type=pathlib.Path, default=ROOT / "batch-price-config.json")
    parser.add_argument("--input", type=pathlib.Path, help="Frozen UTF-8 item list; omit to collect current Xianbao feeds")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--cache", type=pathlib.Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--backend", choices=("xianbao_history", "smzdm"))
    parser.add_argument("--monitor-config", type=pathlib.Path)
    parser.add_argument("--state", type=pathlib.Path)
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-stdout", action="store_true", help="Emit the full report for the monitor bridge")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if config.get("mode") == "product_only" and not args.backend:
        from product_filter import main as product_main
        product_config = args.config.resolve().parent / config.get("product_filter_config", "product-filter-config.json")
        forwarded = ["--config", str(product_config), "--output", str(args.output)]
        if args.input:
            forwarded.extend(["--input", str(args.input)])
        if args.limit is not None:
            forwarded.extend(["--limit", str(args.limit)])
        if args.cache is not None:
            forwarded.extend(["--cache", str(args.cache)])
        if args.monitor_config is not None:
            forwarded.extend(["--monitor-config", str(args.monitor_config)])
        if args.state is not None:
            forwarded.extend(["--state", str(args.state)])
        if args.no_ai:
            forwarded.append("--no-ai")
        if args.dry_run:
            forwarded.append("--dry-run")
        if args.json_stdout:
            forwarded.append("--json-stdout")
        product_main(forwarded)
        return
    config.setdefault("price_backend", "smzdm")
    if args.backend:
        config["price_backend"] = args.backend
    config.setdefault("wiki_interval_max_seconds", config["wiki_interval_seconds"])
    if config.get("provider_state_path"):
        state_path = pathlib.Path(config["provider_state_path"])
        config["provider_state_path"] = str((args.config.resolve().parent / state_path).resolve()) if not state_path.is_absolute() else str(state_path)
    if args.limit is not None:
        config["max_items"] = args.limit
    if not 1 <= config["workers"] <= 4 or not 1 <= config["max_items"] <= 50:
        parser.error("workers must be 1..4 and item limit 1..50")
    for field in ("item_timeout_seconds", "request_timeout_seconds", "batch_timeout_seconds", "max_pages_per_item", "max_searches_per_item", "max_requests_per_batch", "search_interval_seconds", "wiki_interval_seconds", "wiki_interval_max_seconds"):
        if config[field] <= 0:
            parser.error(field + " must be positive")
    if config["wiki_interval_max_seconds"] < config["wiki_interval_seconds"]:
        parser.error("wiki_interval_max_seconds must be at least wiki_interval_seconds")
    if not Decimal(0) < Decimal(config["max_price_ratio"]) <= Decimal(1):
        parser.error("max_price_ratio must be in (0,1]")
    if config["price_backend"] not in ("smzdm", "xianbao_history"):
        parser.error("unknown price_backend")
    if config["price_backend"] == "xianbao_history":
        if not Decimal(0) < Decimal(str(config.get("min_discount_pct", 0))) < Decimal(100):
            parser.error("min_discount_pct must be in (0,100)")
        config.setdefault("history_max_products", 10)
        for field, maximum in (("history_days", 365), ("history_min_samples", 10), ("history_max_products", 10), ("history_search_limit", 50)):
            if not isinstance(config.get(field), int) or not 1 <= config[field] <= maximum:
                parser.error(field + " is outside its supported range")
        if config.get("history_search_cache_seconds", 0) <= 0:
            parser.error("history_search_cache_seconds must be positive")
        if config["history_min_samples"] > config["history_max_products"]:
            parser.error("history_min_samples must not exceed history_max_products")
        synonyms = config.get("history_category_synonyms", {})
        subtypes = config.get("history_category_subtypes", {})
        if not isinstance(subtypes, dict):
            parser.error("history_category_subtypes must map base categories to subtype dictionaries")
        maps = [synonyms, config.get("history_category_exclusions", {}), *subtypes.values()]
        for mapping in maps:
            if not isinstance(mapping, dict) or any(not isinstance(key, str) or not key.strip()
                    or not isinstance(terms, list) or any(not isinstance(term, str) or not term.strip() for term in terms)
                    for key, terms in mapping.items()):
                parser.error("history_category_* dictionaries must map categories to lists of nonempty strings")
    started = time.monotonic()
    deadline = started + config["batch_timeout_seconds"]
    http = Http(config, deadline)
    if args.input:
        items = read_input(args.input)
        snapshot = {"input_file": str(args.input.resolve()), "items": items}
        original = json.loads(args.input.read_text(encoding="utf-8-sig"))
        if isinstance(original, dict):
            snapshot.update({key: original[key] for key in ("captured_at", "feed_errors") if key in original})
    else:
        snapshot = collect(http)
        items = snapshot["items"]
    write_json(args.output.with_suffix(".input.json"), snapshot)
    result = run(items, config, args.cache or ROOT / ".price-cache" / "prices.sqlite3", http, deadline)
    result["limits"]["wiki_interval_max_seconds"] = config["wiki_interval_max_seconds"]
    result.update(total_elapsed_seconds=round(time.monotonic() - started, 3), input_snapshot_path=str(args.output.with_suffix(".input.json").resolve()), input_captured_at=snapshot.get("captured_at"), feed_errors=snapshot.get("feed_errors", []))
    if config["price_backend"] == "xianbao_history":
        result["history_sampling"] = {"min_products": config["history_min_samples"], "max_products": config["history_max_products"],
                                      "selection": "recent_products_brand_round_robin", "aggregation": "median_of_product_unit_price_medians"}
    write_json(args.output, result)
    print(json.dumps(result if args.json_stdout else {key: result[key] for key in ("total_elapsed_seconds", "selected_count", "decisions", "metrics")}, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
