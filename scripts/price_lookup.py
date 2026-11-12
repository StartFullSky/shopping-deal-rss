import collections
import hashlib
import json
import os
import pathlib
import random
import re
import sqlite3
import threading
import time
from contextlib import closing
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from deal_text import normalized, quantity, similarity
from price_snippets import exa_records, search_title, snippet_reference, source_date


class LookupStopped(Exception):
    pass


class Redirect(Exception):
    def __init__(self, url):
        self.url = url


class Budget:
    def __init__(self, config, batch_deadline):
        self.config = config
        self.deadline = min(time.monotonic() + config["item_timeout_seconds"], batch_deadline)
        self.searches = 0
        self.pages = 0
        self.network = 0
        self.timings = collections.Counter()

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise LookupStopped("item_or_batch_timeout")
        return value

    def take(self, kind):
        self.remaining()
        attr, limit = ("searches", "max_searches_per_item") if kind == "search" else ("pages", "max_pages_per_item")
        if getattr(self, attr) >= self.config[limit]:
            raise LookupStopped(kind + "_budget_exhausted")
        setattr(self, attr, getattr(self, attr) + 1)


class Cache:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("CREATE TABLE IF NOT EXISTS cache (namespace TEXT, key TEXT, expires REAL, value TEXT, PRIMARY KEY(namespace,key))")
        self.lock = threading.RLock()
        self.key_locks = collections.defaultdict(threading.Lock)
        self.stats = collections.Counter()

    def get(self, namespace, key, track=True):
        with self.lock:
            row = self.connection.execute("SELECT expires,value FROM cache WHERE namespace=? AND key=?", (namespace, key)).fetchone()
            if row and row[0] > time.time():
                if track:
                    self.stats[namespace + "_hit"] += 1
                return json.loads(row[1])
            if track:
                self.stats[namespace + "_miss"] += 1
            return None

    def put(self, namespace, key, value, ttl):
        with self.lock:
            self.connection.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)", (namespace, key, time.time() + ttl, json.dumps(value, ensure_ascii=False)))
            self.connection.commit()

    def load(self, namespace, key, budget, producer, ttl, negative_ttl):
        value = self.get(namespace, key)
        if value is not None:
            return value
        lock = self.key_locks[(namespace, key)]
        if not lock.acquire(timeout=budget.remaining()):
            raise LookupStopped("cache_singleflight_timeout")
        try:
            value = self.get(namespace, key, track=False)
            if value is not None:
                return value
            value = producer()
            empty = (isinstance(value, list) and not value) or (isinstance(value, dict) and not value.get("prices"))
            self.put(namespace, key, value, negative_ttl if empty else ttl)
            return value
        finally:
            lock.release()

    def close(self):
        self.connection.close()


class Http:
    def __init__(self, config, deadline):
        self.config = config
        self.deadline = deadline
        self.lock = threading.Lock()
        self.semaphore = threading.BoundedSemaphore(config["workers"])
        self.local = threading.local()
        self.throttle = ProviderThrottle(config.get("provider_state_path"))
        self.stopped_sources = {}
        self.cache = None
        self.stats = collections.Counter()

    def disabled(self, host):
        with self.lock:
            return host in self.stopped_sources

    def block(self, host, reason, retry_after=None):
        with self.lock:
            self.stopped_sources[host] = reason
            self.stats["blocked_" + host] += 1
        minimum = 0
        if retry_after:
            try:
                minimum = max(0, float(retry_after))
            except (TypeError, ValueError):
                try:
                    minimum = max(0, parsedate_to_datetime(str(retry_after)).timestamp() - time.time())
                except (TypeError, ValueError, OverflowError):
                    pass
        # Server-directed Retry-After is a request-rate limit, not local backoff.
        if minimum:
            self.throttle.defer(host, minimum)

    def get(self, url, budget=None):
        return self.request(url, budget)

    def interval_for(self, host):
        if host == "wiki.smzdm.com":
            minimum = self.config["wiki_interval_seconds"]
            return random.uniform(minimum, self.config.get("wiki_interval_max_seconds", minimum))
        return self.config["search_interval_seconds"] if host in ("www.so.com", "mcp.exa.ai", "new.xianbao.fun") else 0.15

    def post(self, url, payload, headers, budget):
        return self.request(url, budget, payload, headers)

    def request(self, url, budget=None, payload=None, headers=None):
        for _ in range(2):
            try:
                return self._get_once(url, budget, payload, headers)
            except Redirect as redirect:
                url = redirect.url
        raise LookupStopped("redirect_limit")

    def _get_once(self, url, budget=None, payload=None, headers=None):
        host = urlparse(url).hostname
        if host not in ("www.so.com", "wiki.smzdm.com", "new.xianbao.fun", "mcp.exa.ai"):
            raise LookupStopped("unsupported_source_host")
        remaining = min(self.deadline - time.monotonic(), budget.remaining() if budget else 30)
        if remaining <= 0:
            raise LookupStopped("batch_timeout")
        if self.disabled(host):
            raise LookupStopped("provider_stopped_for_batch")
        with self.lock:
            if self.stats["reservations"] >= self.config["max_requests_per_batch"]:
                raise LookupStopped("batch_request_budget_exhausted")
            self.stats["reservations"] += 1
            if budget:
                budget.network += 1
        interval = self.interval_for(host)
        # Keep each source serial, including requests from another batch/process.
        lease, waited = self.throttle.acquire(host, time.monotonic() + remaining)
        with self.lock:
            self.stats["rate_wait_seconds"] += waited
        acquired_network = False
        try:
            if self.disabled(host):
                raise LookupStopped("provider_stopped_for_batch")
            remaining = min(self.deadline - time.monotonic(), budget.remaining() if budget else 30)
            if remaining <= 0:
                raise LookupStopped("batch_timeout")
            if not self.semaphore.acquire(timeout=max(0, remaining)):
                raise LookupStopped("network_queue_timeout")
            acquired_network = True
            remaining = min(self.deadline - time.monotonic(), budget.remaining() if budget else 30)
            if remaining <= 0:
                raise LookupStopped("batch_timeout")
            if self.disabled(host):
                raise LookupStopped("provider_stopped_for_batch")
            if not hasattr(self.local, "session"):
                self.local.session = requests.Session()
                self.local.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"})
            started = time.monotonic()
            with self.lock:
                self.stats["active"] += 1
                self.stats["requests"] += 1
                self.stats["peak_active"] = max(self.stats["peak_active"], self.stats["active"])
                self.stats["host_" + host] += 1
            try:
                # One fixed-host response per budgeted request; no hidden redirect/retry loop.
                timeout = min(self.config["request_timeout_seconds"], max(0.1, remaining))
                response = self.local.session.get(url, timeout=timeout, allow_redirects=False) if payload is None else self.local.session.post(url, json=payload, headers=headers, timeout=timeout, allow_redirects=False)
                self.local.last_headers = getattr(response, "headers", {})
                if response.status_code in (301, 302, 303, 307, 308):
                    target = response.headers.get("Location")
                    if not target:
                        raise LookupStopped("redirect_without_location")
                    target = urljoin(url, target)
                    if re.search(r"captcha|wappass|/verify", target, re.I):
                        self.block(host, "challenge_redirect")
                        raise LookupStopped("provider_challenge")
                    raise Redirect(target)
                if response.status_code in (429, 403):
                    self.block(host, "http_" + str(response.status_code), getattr(response, "headers", {}).get("Retry-After"))
                    raise LookupStopped("provider_http_" + str(response.status_code))
                response.raise_for_status()
                if response.status_code == 202 and payload and payload.get("method", "").startswith("notifications/"):
                    return "{}"
                if response.status_code != 200:
                    raise LookupStopped("unexpected_http_" + str(response.status_code))
                content = response.content.decode("utf-8", errors="replace")
                waf_challenge = "/WafCaptcha" in content and re.search(r"TencentCaptcha|TCaptcha\.js|__captcha", content)
                if waf_challenge or re.search(r"<title[^>]*>[^<]*(?:访问异常|安全验证|captcha)|请输入验证码|verify you are human", content, re.I):
                    self.block(host, "challenge_page")
                    raise LookupStopped("provider_challenge")
                return content
            finally:
                with self.lock:
                    self.stats["active"] -= 1
                    self.stats["network_seconds"] += time.monotonic() - started
        except requests.RequestException as error:
            with self.lock:
                self.stats["errors"] += 1
            raise LookupStopped("network_" + type(error).__name__) from error
        finally:
            if acquired_network:
                self.semaphore.release()
            self.throttle.release(host, lease, interval)


def canonical_wiki(url):
    parsed = urlparse(urljoin("https://wiki.smzdm.com/", url))
    match = re.fullmatch(r"/p/([a-z0-9]+)/(?:canshu/|p\d+/)?", parsed.path)
    return "https://wiki.smzdm.com/p/" + match[1] + "/" if parsed.hostname == "wiki.smzdm.com" and match else None


def canonical_catalog(url):
    parsed = urlparse(urljoin("https://wiki.smzdm.com/", url))
    if parsed.hostname == "wiki.smzdm.com" and re.fullmatch(r"/(?!p/)[a-z0-9_/-]{2,100}/?", parsed.path):
        return "https://wiki.smzdm.com" + parsed.path
    return None


def parse_catalog(html):
    soup = BeautifulSoup(html, "html.parser")
    rows, seen = [], set()
    for anchor in soup.select("a[href]"):
        url = canonical_wiki(anchor.get("href", ""))
        title = normalized(anchor.get_text(" ", strip=True) or anchor.get("title", ""))
        if url and url not in seen and len(title) >= 6:
            seen.add(url)
            rows.append({"title": title, "url": url, "type": "product"})
    return rows


def parse_search(html):
    soup = BeautifulSoup(html, "html.parser")
    results, seen = [], set()
    for row in soup.select("li.res-list"):
        anchor = row.select_one("h3 a")
        if not anchor:
            continue
        target = anchor.get("data-mdurl") or anchor.get("href", "")
        url = canonical_wiki(target) or canonical_catalog(target)
        if url and url not in seen:
            seen.add(url)
            summary = row.select_one("[data-result-summary]") or row.select_one(".res-desc, .res-summary, .res-descr")
            dated = row.select_one(".res-desc .gray, .res-desc time, .res-summary time")
            raw_title = normalized(anchor.get_text(" ", strip=True))
            results.append({
                "title": search_title(raw_title), "raw_title": raw_title, "url": url,
                "type": "product" if canonical_wiki(url) else "catalog",
                "snippet": summary.get_text(" ", strip=True)[:6000] if summary else "",
                "source_date": source_date(dated.get_text()) if dated else None,
                "captured_at": time.time(),
            })
    return results


def money(text):
    match = re.search(r"(?:¥|￥)\s*(\d+(?:\.\d+)?)", text)
    return str(Decimal(match[1])) if match else None


def parse_wiki(html, url):
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1.pd-title")
    if not heading:
        raise LookupStopped("wiki_invalid_or_challenge_page")
    name = normalized(heading.get_text(" ", strip=True)) if heading else ""
    name = re.sub(r"^商品\s*", "", name)
    size = quantity(name, default=False)
    # Read the current product name, never unrelated recommendations or history.
    if not size:
        # Plain item counts default to one; a multi-size family cannot be normalised.
        family = any(term in name for term in ("多规格", "组合", "礼盒"))
        size = None if family else quantity(name)
    prices = []
    for row in soup.select("a.compare-price-item"):
        regular, member = row.select_one(".compare-price"), row.select_one(".minimum-price")
        target = regular or member
        if not target:
            continue
        amount = money(target.get_text(" ", strip=True))
        if amount is not None and Decimal(amount) > 0:
            shop = row.select_one(".shop-name")
            prices.append({"amount": amount, "member": regular is None, "mall": shop.get_text(" ", strip=True) if shop else None, "basis": "comparison"})
    header = soup.select_one("#J_sku_price")
    match = re.search(r"(\d+(?:\.\d+)?)\s*元起", header.get_text(" ", strip=True)) if header else None
    if match and Decimal(match[1]) > 0 and size:
        prices.append({"amount": str(Decimal(match[1])), "member": None, "mall": None, "basis": "header_from_price"})
    no_quote = soup.select_one(".sku-pd-price .no-price")
    explicitly_empty = bool(no_quote and "暂无报价" in no_quote.get_text())
    quote_status = "quoted" if prices else "source_no_quote" if explicitly_empty else "quote_unavailable"
    return {"title": name, "url": url, "quantity": size, "prices": prices, "quote_status": quote_status, "captured_at": time.time(), "source_updated_at": None}


def rpc_json(text):
    try:
        return json.loads(text)
    except ValueError:
        events = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        return json.loads(events[-1]) if events else {}


class ExaFree:
    def __init__(self, http):
        self.http = http
        self.initialized = False
        self.lock = threading.Lock()
        self.counter = 0
        self.id_lock = threading.Lock()
        self.headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": "shopping-deal-rss-price-batch/1.0"}

    def rpc(self, method, params, budget, notification=False):
        with self.id_lock:
            self.counter += 1
            request_id = self.counter
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = request_id
        value = rpc_json(self.http.post("https://mcp.exa.ai/mcp", payload, self.headers, budget))
        if "error" in value:
            raise LookupStopped("exa_rpc_error")
        return value.get("result", {})

    def search(self, query, budget):
        with self.lock:
            if not self.initialized:
                result = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "shopping-deal-rss-price-batch", "version": "1.0"}}, budget)
                self.headers["MCP-Protocol-Version"] = result.get("protocolVersion", "2025-03-26")
                for key, value in self.http.local.last_headers.items():
                    if key.lower() == "mcp-session-id":
                        self.headers["Mcp-Session-Id"] = value
                self.rpc("notifications/initialized", {}, budget, notification=True)
                self.initialized = True
        result = self.rpc("tools/call", {"name": "web_search_exa", "arguments": {"query": "site:wiki.smzdm.com " + query, "objective": "寻找该商品及可比较同类商品在wiki.smzdm.com上的商品百科，优先具体商品详情页，其次同品牌商品分类页，不要www.smzdm.com好价爆料文章", "numResults": 8}}, budget)
        if result.get("isError"):
            self.http.block("mcp.exa.ai", "free_search_error")
            raise LookupStopped("exa_free_search_error")
        text = "\n".join(block.get("text", "") for block in result.get("content", []) if block.get("type") == "text")
        return exa_records(text, canonical_wiki, canonical_catalog)


class Lookup:
    def __init__(self, config, cache, http):
        self.config, self.cache, self.http = config, cache, http
        self.exa = ExaFree(http)

    def search(self, query, budget):
        started = time.monotonic()
        key = normalized(query).lower()
        def produce():
            url = requests.Request("GET", "https://www.so.com/s", params={"q": "site:wiki.smzdm.com " + query}).prepare().url
            return [{**record, "search_provider": "360"} for record in parse_search(self.http.get(url, budget))]
        try:
            # New namespaces keep old URL-only results from hiding the new
            # excerpts. The existing price TTL bounds cached indexed quotes.
            ttl = min(self.config["search_cache_seconds"], self.config["price_cache_seconds"])
            cached = self.cache.get("search_snippets", key, track=False)
            if cached is not None or not self.http.disabled("www.so.com"):
                budget.take("search")
                try:
                    return self.cache.load("search_snippets", key, budget, produce, ttl, self.config["negative_cache_seconds"])
                except LookupStopped as error:
                    if self.config.get("fallback_search") != "exa_free" or not self.http.disabled("www.so.com"):
                        raise
            if self.config.get("fallback_search") != "exa_free":
                raise LookupStopped("provider_stopped_for_batch")
            budget.take("search")
            with self.http.lock:
                self.http.stats["fallback_lookups"] += 1
            return self.cache.load("search_exa_snippets", key, budget, lambda: self.exa.search(query, budget), ttl, self.config["negative_cache_seconds"])
        finally:
            budget.timings["search_seconds"] += time.monotonic() - started

    def page(self, url, budget):
        budget.take("page")
        started = time.monotonic()
        def produce():
            try:
                return parse_wiki(self.http.get(url, budget), url)
            except LookupStopped as error:
                if str(error) == "wiki_invalid_or_challenge_page":
                    self.http.block("wiki.smzdm.com", str(error))
                raise
        try:
            value = self.cache.load("price", url, budget, produce, self.config["price_cache_seconds"], self.config["negative_cache_seconds"])
            if not value.get("title"):
                self.http.block("wiki.smzdm.com", "cached_invalid_wiki_page")
                raise LookupStopped("wiki_invalid_or_challenge_page")
            return value
        finally:
            budget.timings["page_seconds"] += time.monotonic() - started

    def catalog(self, url, budget):
        budget.take("page")
        started = time.monotonic()
        try:
            return self.cache.load("catalog", url, budget, lambda: parse_catalog(self.http.get(url, budget)), self.config["search_cache_seconds"], self.config["negative_cache_seconds"])
        finally:
            budget.timings["page_seconds"] += time.monotonic() - started

    def compare(self, facts, page):
        match = similarity(facts["title"], page["title"])
        if match["match"] not in ("same_product_or_series", "category_estimate"):
            return None
        q1, q2 = facts["quantity"], page["quantity"]
        dimensions = lambda value: re.findall(r"(\d+)\s*[*x×]\s*(\d+)\s*cm", value.lower())
        d1, d2 = dimensions(facts["title"]), dimensions(page["title"])
        if d1 and d2 and d1 != d2:
            return None
        if not q2 or q1["unit"] != q2["unit"] or Decimal(q2["amount"]) <= 0:
            return None
        if not page["prices"]:
            return None
        chosen = min(page["prices"], key=lambda p: Decimal(p["amount"]))
        unit_price = Decimal(chosen["amount"]) / Decimal(q2["amount"])
        equivalent = unit_price * Decimal(q1["amount"])
        ratio = Decimal(facts["cash_total"]) / equivalent
        value = {"url": page["url"], "title": page["title"], "quantity": q2, "price": chosen, "all_prices": page["prices"], "equivalent_price": str(equivalent), "ratio": str(ratio), "match": match["match"], "score": match["score"], "captured_at": page["captured_at"], "evidence_type": page.get("evidence_type", "product_page"), "source_updated_at": page.get("source_updated_at")}
        if value["evidence_type"] == "search_snippet":
            value.update(snippet=page["snippet"], source_date=page.get("source_date"), requires_review=True,
                         review_reasons=["indexed_price_not_live_verified", "price_update_time_unknown"])
            if chosen.get("from_price"):
                value["review_reasons"].append("starting_price")
            if chosen.get("conditions"):
                value["review_reasons"].append("conditional_reference_price")
            if match["match"] == "category_estimate":
                value["review_reasons"].append("category_unit_price_estimate")
        return value

    def run(self, facts, budget):
        signature = hashlib.sha256((facts["title"] + "|" + facts["quantity"]["unit"]).encode()).hexdigest()
        known = self.cache.get("match_snippets", signature)
        candidates, examined, found = [], set(), []
        errors, snippet_examined, snippet_priced = [], set(), set()
        # A URL mapping can live for seven days; a price inside it cannot.
        if known and any(snippet_reference(row, self.config["price_cache_seconds"]) for row in known):
            candidates = known
        # A map is a ranked URL list, not a cached verdict: prices and deal cash are re-evaluated.
        for round_number in range(self.config["max_searches_per_item"]):
            if not candidates:
                query = facts["title"]
                if round_number:
                    # Drop specification and brand for the category fallback, when a noun is known.
                    query = facts["kind"] or re.sub(r"\d+(?:\.\d+)?\s*(?:g|kg|ml|抽|片|瓶|包|盒|件|斤|条)", " ", query, flags=re.I)
                try:
                    candidates = self.search(query, budget)
                except LookupStopped as error:
                    errors.append(str(error))
                    if known and round_number == 0 and not self.http.disabled("wiki.smzdm.com"):
                        candidates = known
                    else:
                        break
            def rank(row):
                score = similarity(facts["title"], row["title"])["score"]
                size = quantity(row["title"], default=False)
                target = facts["quantity"]
                if size and size["unit"] == target["unit"]:
                    a, b = Decimal(size["amount"]), Decimal(target["amount"])
                    if max(a, b) > 0:
                        score += .25 * float(min(a, b) / max(a, b))
                return score
            ranked = sorted(candidates, key=rank, reverse=True)
            # Inspect all comparable excerpts before opening any product page.
            # A blocked encyclopedia does not prevent searching its indexed text.
            for candidate in ranked:
                if candidate.get("type") != "product" or candidate["url"] in snippet_examined:
                    continue
                snippet_examined.add(candidate["url"])
                page = snippet_reference(candidate, self.config["price_cache_seconds"])
                if not page:
                    continue
                snippet_priced.add(candidate["url"])
                value = self.compare(facts, page)
                if value:
                    value["search_provider"] = candidate.get("search_provider", "cached_search")
                    found.append(value)
            if found:
                break
            if self.http.disabled("wiki.smzdm.com"):
                if "wiki_provider_stopped_for_batch" not in errors:
                    errors.append("wiki_provider_stopped_for_batch")
                candidates = []
                continue
            round_page_limit = max(1, self.config["max_pages_per_item"] - 1) if round_number == 0 else self.config["max_pages_per_item"]
            for index, candidate in enumerate(ranked):
                if candidate["url"] in examined:
                    continue
                hint = similarity(facts["title"], candidate["title"])
                if hint["match"] not in ("same_product_or_series", "category_estimate"):
                    continue
                if budget.pages >= round_page_limit:
                    break
                examined.add(candidate["url"])
                try:
                    if candidate.get("type") == "catalog":
                        children = self.catalog(candidate["url"], budget)
                        round_page_limit = self.config["max_pages_per_item"]
                        children = [{**row, "search_provider": candidate.get("search_provider", "cached_catalog")} for row in children]
                        ranked[index + 1:index + 1] = sorted(children, key=rank, reverse=True)[:self.config["max_pages_per_item"]]
                        continue
                    page = self.page(candidate["url"], budget)
                    value = self.compare(facts, page)
                    if value:
                        value["search_provider"] = candidate.get("search_provider", "cached_match")
                        found.append(value)
                except LookupStopped as error:
                    errors.append(str(error))
                    if any(word in str(error) for word in ("timeout", "stopped_for_batch", "http_", "challenge", "invalid_wiki")):
                        break
            if found:
                break
            candidates = []
        summary = {"references_examined": len(examined), "snippets_examined": len(snippet_examined),
                   "snippets_with_prices": len(snippet_priced), "usable_references": len(found), "errors": errors}
        if found:
            best_kind = "same_product_or_series" if any(r["match"] == "same_product_or_series" for r in found) else "category_estimate"
            comparable = [r for r in found if r["match"] == best_kind]
            # The cheapest comparable unit price avoids inflating a bulk deal's discount.
            best = min(comparable, key=lambda r: Decimal(r["equivalent_price"]))
            by_url = {row["url"]: row for row in candidates}
            self.cache.put("match_snippets", signature, [
                by_url.get(r["url"], {"title": r["title"], "url": r["url"], "type": "product", "search_provider": r["search_provider"]})
                for r in sorted(found, key=lambda r: Decimal(r["equivalent_price"]))
            ], self.config["match_cache_seconds"])
            return {"reference": best, "references": found, **summary}
        return {"reference": None, "references": [], **summary}


class ProviderThrottle:
    """Share completion-to-next-request gaps and server rate limits across batches."""

    def __init__(self, path=None):
        self.path = pathlib.Path(path) if path else None
        self.locks = collections.defaultdict(threading.Lock)
        self.state_lock = threading.RLock()
        self.memory = {}
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(str(self.path))) as db:
                db.execute("CREATE TABLE IF NOT EXISTS request_gaps (host TEXT PRIMARY KEY, next_at REAL)")
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='providers'").fetchone():
                    # Retain only the old request gap; ignore legacy cooldown/failure fields.
                    db.execute("INSERT OR IGNORE INTO request_gaps SELECT host,next_at FROM providers")
                db.commit()

    def _read(self, host):
        with self.state_lock:
            if not self.path:
                return self.memory.get(host, 0)
            with closing(sqlite3.connect(str(self.path))) as db:
                row = db.execute("SELECT next_at FROM request_gaps WHERE host=?", (host,)).fetchone()
                return row[0] if row else 0

    def defer(self, host, seconds):
        next_at = time.time() + max(0, seconds)
        with self.state_lock:
            if not self.path:
                self.memory[host] = max(self.memory.get(host, 0), next_at)
                return
            with closing(sqlite3.connect(str(self.path))) as db:
                with db:
                    db.execute("INSERT INTO request_gaps VALUES(?,?) ON CONFLICT(host) DO UPDATE SET next_at=MAX(request_gaps.next_at,excluded.next_at)", (host, next_at))

    @staticmethod
    def _unlock(handle):
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def acquire(self, host, deadline):
        started = time.monotonic()
        lock = self.locks[host]
        if not lock.acquire(timeout=max(0, deadline - started)):
            raise LookupStopped("rate_queue_timeout")
        handle, file_locked = None, False
        try:
            if self.path:
                filename = self.path.parent / (self.path.name + "." + hashlib.sha256(host.encode()).hexdigest()[:16] + ".lock")
                handle = filename.open("a+b")
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"0")
                    handle.flush()
                while not file_locked:
                    handle.seek(0)
                    try:
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        file_locked = True
                    except OSError:
                        if time.monotonic() + .05 >= deadline:
                            raise LookupStopped("rate_queue_timeout")
                        time.sleep(.05)
            while True:
                delay = max(0, self._read(host) - time.time())
                if time.monotonic() + delay >= deadline:
                    raise LookupStopped("rate_queue_timeout")
                if not delay:
                    break
                time.sleep(delay)
            return (lock, handle), time.monotonic() - started
        except Exception:
            try:
                if file_locked:
                    self._unlock(handle)
                elif handle:
                    handle.close()
            finally:
                lock.release()
            raise

    def release(self, host, lease, interval):
        lock, handle = lease
        try:
            self.defer(host, interval)
        finally:
            try:
                self._unlock(handle)
            finally:
                lock.release()
