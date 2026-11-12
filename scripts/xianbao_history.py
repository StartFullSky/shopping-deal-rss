"""Compare a lead with deduplicated historical deal quotes from Xianbao's API."""

import collections
import hashlib
import json
import re
import time
from decimal import Decimal, ROUND_HALF_UP

import requests
from bs4 import BeautifulSoup

from deal_text import MEASURE, deal_facts, grams, identity_title, models, normalized, product_kind, similarity
from price_lookup import LookupStopped


def brand_token(title):
    title = identity_title(title).strip()
    latin = re.match(r"([A-Za-z][A-Za-z&.' -]*)(?=[\u4e00-\u9fff\d]|$)", title)
    if latin:
        return latin[1].strip().lower()
    chinese = re.match(r"[\u4e00-\u9fff]{2,}", title)
    return chinese[0][:2] if chinese else None


def category_kind(facts, config):
    # A gift mentioned after the main item is not the product being priced.
    title = re.split(r"赠送?|附赠|加赠|送", facts["title"], maxsplit=1)[0]
    synonyms = config.get("history_category_synonyms", {})
    known = product_kind(title)
    for subtype, terms in config.get("history_category_subtypes", {}).get(known, {}).items():
        if any(term in title for term in terms):
            return subtype
    if known and len(known) > 1:
        kind = known
        for kind, terms in synonyms.items():
            if known in (kind, *terms):
                break
        else:
            kind = known
        # An explicit main product type wins over a word used as a brand, e.g.
        # an Apple phone must not be classified as apple fruit.
    else:
        matches = [(len(term), kind) for kind, terms in synonyms.items()
                   for term in (kind, *terms) if term in title]
        kind = max(matches)[1] if matches else known
    if any(term in title for term in config.get("history_category_exclusions", {}).get(kind, [])):
        return None
    return kind


def historical_queries(facts, config=None):
    config = config or {}
    kind = category_kind(facts, config)
    if not kind or len(kind) < 2:
        return []
    terms = [*config.get("history_category_synonyms", {}).get(kind, []), kind]
    # Single category terms performed much better than broad OR queries in
    # live tests. Keep synonyms as separate fallbacks within the same budget.
    return list(dict.fromkeys(terms))


def median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _timestamp(row):
    try:
        return float(row.get("published_at") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def comparable_samples(item, facts, rows, config):
    current_time = _timestamp(item)
    reasons = collections.Counter()
    if current_time <= 0:
        return [], {"current_timestamp_missing": 1}
    oldest = current_time - config["history_days"] * 86400
    current_brand = brand_token(facts["title"])
    current_kind = category_kind(facts, config)
    samples, seen_ids = [], set()
    for row in sorted(rows, key=_timestamp, reverse=True):
        ident = str(row.get("id") or "")
        when = _timestamp(row)
        if not ident or ident == str(item.get("id")) or row.get("url") and row.get("url") == item.get("url"):
            reasons["current_or_missing_id"] += 1
            continue
        if ident in seen_ids:
            reasons["duplicate_id"] += 1
            continue
        seen_ids.add(ident)
        if when <= 0 or when >= current_time or when < oldest:
            reasons["outside_history_window"] += 1
            continue
        # Search supplies HTML but often omits both plain content and excerpt.
        body = row.get("content") or row.get("excerpt") or BeautifulSoup(row.get("content_html") or "", "html.parser").get_text(" ", strip=True)
        other = deal_facts({**row, "content": body})
        other_kind = category_kind(other, config)
        if re.search(r"^(?:还有|昨天的?|今天的?|这个|那个).{0,5}(?:小?玩具|商品|东西)|助力.*(?:折|刚需)|(?:图里|图中).*(?:hb|红包)", other["title"], re.I):
            reasons["product_identity_unverified"] += 1
            continue
        # Historical announcements are quotes, not currently actionable leads.
        if other["status"] not in ("ready", "not_started") or other["cash_total"] is None or Decimal(other["cash_total"]) <= 0:
            reasons["cash_unverified"] += 1
            continue
        if not current_kind or other_kind != current_kind:
            reasons["unrelated_product"] += 1
            continue
        q = comparable_quantity(facts, other)
        if (current_kind in ("棉柔巾", "湿巾", "抽纸") and other_kind == current_kind
                and q["unit"] in ("张", "抽", "片") and facts["quantity"]["unit"] in ("张", "抽", "片")):
            q = {**q, "unit": facts["quantity"]["unit"]}
        if re.search(r"多规格|多款|任选", other["title"]) or q["unit"] != facts["quantity"]["unit"] or Decimal(q["amount"]) <= 0:
            reasons["incompatible_quantity"] += 1
            continue
        title_tokens = grams(facts["title"])
        other_tokens = grams(other["title"])
        current_overlap = len(title_tokens & other_tokens) / max(1, min(len(title_tokens), len(other_tokens)))
        if (Decimal(other["cash_total"]) == Decimal(facts["cash_total"])
                and Decimal(q["amount"]) == Decimal(facts["quantity"]["amount"])
                and current_time - when <= 86400 and current_overlap >= .78
                and current_brand == brand_token(other["title"])
                and models(other["title"]) == models(facts["title"])):
            reasons["current_offer_crosspost"] += 1
            continue
        match = similarity(facts["title"], other["title"])
        same_brand = bool(current_brand and brand_token(other["title"]) == current_brand)
        # A shared colour/adjective is not a brand (e.g. LILY vs DUSHU khaki coats).
        same_series = same_brand and match["match"] == "same_product_or_series"
        tokens = grams(other["title"])
        cash = Decimal(other["cash_total"])
        duplicate = False
        for previous in samples:
            overlap = len(tokens & previous["_tokens"]) / max(1, min(len(tokens), len(previous["_tokens"])))
            if (cash == Decimal(previous["cash_total"]) and q["unit"] == previous["quantity"]["unit"]
                    and Decimal(q["amount"]) == Decimal(previous["quantity"]["amount"])
                    and abs(when - previous["published_at"]) <= 86400 and overlap >= .78
                    and brand_token(other["title"]) == previous["brand"]
                    and models(other["title"]) == models(previous["title"])):
                duplicate = True
                break
        if duplicate:
            reasons["duplicate_offer"] += 1
            continue
        equivalent = cash / Decimal(q["amount"]) * Decimal(facts["quantity"]["amount"])
        samples.append({
            "id": ident, "title": other["title"], "url": row.get("url"), "published_at": when,
            "cash_total": str(cash), "quantity": q, "equivalent_price": str(equivalent),
            "same_brand": same_brand, "same_series": same_series, "conditions": other["conditions"], "_tokens": tokens,
            "brand": brand_token(other["title"]), "kind": other_kind,
            "unit_price": str(cash / Decimal(q["amount"])),
            "product_identity": product_identity(other["title"], other_kind, config),
            "product_ids": product_ids(body + " " + (row.get("content_html") or "")),
            "comparison_basis": q.get("comparison_basis", "explicit_unit"),
        })
    return samples, dict(reasons)


def history_reference(facts, samples, config):
    minimum = config["history_min_samples"]
    products = group_products(samples)
    if len(products) < minimum:
        return None
    selected = select_products(products, config.get("history_max_products", 10))
    units = [Decimal(s["unit_price"]) for s in selected]
    unit_middle = median(units)
    # Keep sub-cent precision while removing repeating-division residue at the
    # inclusive 20% boundary (e.g. 8.000000 versus 7.999999999999999999).
    middle = (unit_middle * Decimal(facts["quantity"]["amount"])).quantize(Decimal(".000001"), rounding=ROUND_HALF_UP)
    if middle <= 0:
        return None
    representative = min(selected, key=lambda s: abs(Decimal(s["unit_price"]) - unit_middle))
    prices = [Decimal(s["equivalent_price"]) for s in selected]
    return {
        "url": representative["url"], "title": representative["title"], "quantity": facts["quantity"],
        "price": {"amount": str(middle), "basis": "xianbao_product_unit_medians", "member": None},
        "equivalent_price": str(middle), "ratio": str(Decimal(facts["cash_total"]) / middle),
        "unit_price_median": str(unit_middle), "unit": facts["quantity"]["unit"],
        "match": "category_estimate", "requires_review": True,
        "history_scope": "category_distinct_products", "evidence_type": "xianbao_history", "captured_at": time.time(),
        "sample_count": len(selected), "product_count": len(selected), "total_distinct_products": len(products),
        "quote_count": sum(s["quote_count"] for s in selected),
        "brand_count": len({s["brand"] for s in selected if s["brand"]}),
        "selection": "recent_products_brand_round_robin", "history_days": config["history_days"],
        "comparison_basis": "pack_count_unknown_current_size" if any(s["comparison_basis"] == "pack_count_unknown_current_size" for s in selected) else "explicit_unit",
        "comparison_note": "当前单包规格缺失，按包装数量粗估" if any(s["comparison_basis"] == "pack_count_unknown_current_size" for s in selected) else "已按明确单位换算",
        "sample_min_price": str(min(prices)), "sample_max_price": str(max(prices)),
        "samples": selected,
        "reference_label": "线报酷同类不同商品单位价中位数，非市场原价", "search_provider": "xianbao_api",
    }


class XianbaoHistoryLookup:
    def __init__(self, config, cache, http):
        self.config, self.cache, self.http = config, cache, http

    def search(self, query, budget, page=1):
        budget.take("search")
        key = hashlib.sha256(json.dumps([query, self.config["history_search_limit"], page], ensure_ascii=False).encode()).hexdigest()
        def produce():
            url = requests.Request("GET", "https://new.xianbao.fun/api/v1/search", params={
                "q": query, "limit": self.config["history_search_limit"], "page": page, "with_html": 1,
            }).prepare().url
            payload = json.loads(self.http.get(url, budget))
            if payload.get("code") not in (0, 200) or not isinstance(payload.get("data"), dict):
                raise LookupStopped("xianbao_search_business_error")
            items = payload["data"].get("items")
            if not isinstance(items, list):
                raise LookupStopped("xianbao_search_invalid_items")
            return items
        started = time.monotonic()
        try:
            return self.cache.load("xianbao_category_search_v3", key, budget, produce,
                                   self.config["history_search_cache_seconds"], self.config["negative_cache_seconds"])
        finally:
            budget.timings["history_search_seconds"] += time.monotonic() - started

    def run_item(self, item, facts, budget):
        if re.search(r"多规格|多款|任选", facts["title"]):
            return {"reference": None, "errors": ["current_specification_ambiguous"], "usable_references": 0}
        rows, errors, queries, searches, reference = [], [], [], [], None
        samples, rejected = [], {}
        terms = historical_queries(facts, self.config)
        if not terms:
            return {"reference": None, "errors": [], "usable_references": 0, "distinct_products": 0,
                    "reason": "category_unknown", "history_queries": [], "history_searches": []}
        query, page, synonym_index = terms[0], 1, 0
        for index in range(self.config["max_searches_per_item"]):
            try:
                page_rows = self.search(query, budget, page=page)
                rows.extend(page_rows)
                if query not in queries:
                    queries.append(query)
                related = sum(category_kind(deal_facts(row), self.config) == category_kind(facts, self.config) for row in page_rows)
                searches.append({"query": query, "page": page, "returned": len(page_rows), "matching_category": related})
            except LookupStopped as error:
                errors.append(str(error))
                break
            samples, rejected = comparable_samples(item, facts, rows, self.config)
            reference = history_reference(facts, samples, self.config)
            searches[-1]["distinct_products_after"] = len(group_products(samples))
            if reference:
                break
            if (related < len(page_rows) / 2 or len(page_rows) < self.config["history_search_limit"]) and synonym_index + 1 < len(terms):
                synonym_index += 1
                query, page = terms[synonym_index], 1
            elif len(page_rows) == self.config["history_search_limit"]:
                page += 1
            else:
                break
        return {"reference": reference, "usable_references": len(samples), "history_queries": queries,
                "history_searches": searches, "distinct_products": len(group_products(samples)),
                "history_rows_examined": len(rows), "history_rejections": rejected, "errors": errors,
                "reason": None if reference else "insufficient_distinct_products"}


def product_identity(title, kind, config):
    clean = re.split(r"赠送?|附赠|加赠|送", identity_title(title), maxsplit=1)[0].lower()
    clean = re.sub(r"\d+(?:\.\d+)?\s*(?:元|亓|块|r)(?:\s*/\s*[^\s，,]+)?", " ", clean)
    clean = MEASURE.sub(" ", clean)
    clean = re.sub(r"[*x×]\s*\d+", " ", clean)
    clean = re.sub(r"(?:拍|买|下单)\s*\d+\s*(?:件|发\s*\d+\s*包)?", " ", clean)
    for term in sorted(config.get("history_category_synonyms", {}).get(kind, []), key=len, reverse=True):
        clean = clean.replace(term.lower(), kind)
    clean = re.sub(r"组合装?|旅行装|正装|家庭装|补充装|瓶装|袋装|净重|情侣款|男女同款|男女款|同款", " ", clean)
    clean = re.sub(r"[\W_]+", "", clean)
    return clean


def group_products(samples):
    groups = []
    for sample in sorted(samples, key=lambda s: (-s["published_at"], s["id"])):
        group = next((g for g in groups if same_product(sample, g[0])), None)
        if group is None:
            groups.append([sample])
        else:
            group.append(sample)
    products = []
    for group in groups:
        middle = median([Decimal(s["unit_price"]) for s in group])
        representative = min(group, key=lambda s: abs(Decimal(s["unit_price"]) - middle))
        size = Decimal(representative["equivalent_price"]) / Decimal(representative["unit_price"])
        quotes = [{k: v for k, v in s.items() if not k.startswith("_")} for s in group]
        products.append({**quotes[group.index(representative)], "unit_price": str(middle),
                         "equivalent_price": str(middle * size), "quote_count": len(group),
                         "latest_published_at": max(s["published_at"] for s in group), "quotes": quotes})
    return products


def same_product(sample, previous):
    if set(sample.get("product_ids", [])) & set(previous.get("product_ids", [])):
        return True
    if sample["product_identity"] == previous["product_identity"]:
        return True
    a_name, b_name = sample["product_identity"], previous["product_identity"]
    if (sample["kind"] == previous["kind"] and min(len(a_name), len(b_name)) >= max(5, len(sample["kind"]) + 2)
            and (a_name in b_name or b_name in a_name)):
        # A parent brand may be omitted: "金典纯牛奶" / "伊利金典纯牛奶".
        return True
    if not sample["brand"] or sample["brand"] != previous["brand"] or sample["kind"] != previous["kind"]:
        return False
    if models(sample["title"]) != models(previous["title"]):
        return False
    a, b = grams(sample["product_identity"]), grams(previous["product_identity"])
    return bool(a and b and len(a & b) / min(len(a), len(b)) >= .9)


def select_products(products, maximum):
    # Selection never depends on price. Rotate brands so one brand's many
    # variants cannot occupy all ten slots merely by publishing more posts.
    brands = collections.defaultdict(list)
    for product in sorted(products, key=lambda s: (-s["latest_published_at"], s["id"])):
        brands[product["brand"] or "unknown"].append(product)
    selected = []
    while brands and len(selected) < maximum:
        for brand in list(brands):
            selected.append(brands[brand].pop(0))
            if not brands[brand]:
                del brands[brand]
            if len(selected) == maximum:
                break
    return selected


def product_ids(text):
    patterns = (
        ("jd", r"(?:item\.jd\.com/|item\.m\.jd\.com/product/)(\d+)"),
        ("taobao", r"(?:item\.taobao\.com|detail\.tmall\.com)/[^\s\"<>]*?[?&](?:amp;)?id=(\d+)"),
        ("pdd", r"(?:mobile\.yangkeduo\.com|mobile\.pinduoduo\.com)/[^\s\"<>]*?[?&](?:amp;)?goods_id=(\d+)"),
    )
    return sorted({prefix + ":" + match for prefix, pattern in patterns for match in re.findall(pattern, text)})


def comparable_quantity(current, other):
    q = other["quantity"]
    target = current["quantity"]
    if q["unit"] == target["unit"]:
        return q
    if (re.search(r"皮带|腰带", current["title"]) and {q["unit"], target["unit"]} <= {"件", "条"}
            or re.search(r"鞋|靴", current["title"]) and {q["unit"], target["unit"]} <= {"件", "双"}):
        return {**q, "unit": target["unit"]}
    # A known 200ml*24盒 can supply a per-box quote even when the current
    # headline only says 24盒. Do not invent the current box's missing volume.
    if (target["unit"] in ("盒", "瓶", "袋", "包", "桶")
            and q["unit"] in ("ml", "g", "抽", "张", "片")
            and q.get("outer_unit") == target["unit"]):
        return {**q, "amount": q["outer_count"], "unit": target["unit"],
                "physical_quantity": other["quantity"], "comparison_basis": "pack_count_unknown_current_size"}
    return q
