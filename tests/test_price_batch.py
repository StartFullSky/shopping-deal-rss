import concurrent.futures
import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from deal_text import deal_facts, quantity, similarity
from price_lookup import Budget, Cache, Http, Lookup, LookupStopped, ProviderThrottle, canonical_wiki, parse_catalog, parse_search, parse_wiki
from price_batch import select


CONFIG = json.loads((pathlib.Path(__file__).resolve().parents[1] / "batch-price-config.json").read_text(encoding="utf-8"))
CONFIG["provider_state_path"] = None


class QuantityAndCashTests(unittest.TestCase):
    def test_explicit_packs_and_units(self):
        self.assertEqual(quantity("牙膏200g×4支")["amount"], "800")
        self.assertEqual(quantity("牛奶250ml*24盒")["amount"], "6000")
        self.assertEqual(quantity("洗衣液14斤")["amount"], "7000")
        self.assertEqual(quantity("510g*2包12只")["amount"], "1020")
        self.assertEqual(quantity("果粒7.5g买3件")["amount"], "22.5")

    def test_does_not_confuse_nutrition_with_package_weight(self):
        self.assertEqual(quantity("3.6g蛋白 牛奶250ml×16盒")["amount"], "4000")

    def test_weight_and_volume_remain_distinct(self):
        self.assertNotEqual(quantity("洗发水500g")["unit"], quantity("洗发水500ml")["unit"])

    def test_default_is_one_only_without_explicit_count(self):
        self.assertEqual(quantity("奇美高音六孔竖笛")["amount"], "1")
        self.assertEqual(quantity("面膜30片")["amount"], "30")

    def test_per_pack_price_beats_misleading_headline(self):
        facts = deal_facts({"title": "棉柔巾200抽×2提 17元", "price": 17, "content": "到手17元/提"})
        self.assertEqual(facts["cash_total"], "34")
        facts = deal_facts({"title": "面膜5片×3盒 10元", "price": 10, "content": "每盒10元"})
        self.assertEqual(facts["cash_total"], "30")

    def test_reward_is_not_a_cash_discount(self):
        facts = deal_facts({"title": "牙膏200g×4支 7.9元", "price": 7.9, "content": "拍下7.9返3猫超红包"})
        self.assertEqual(facts["cash_total"], "7.9")
        facts = deal_facts({"title": "芋头2250g 13.8元", "price": 13.8, "content": "拍16.8-返3红包=13.8元"})
        self.assertEqual(facts["cash_total"], "16.8")

    def test_actual_cash_precedes_rebate_equivalent(self):
        facts = deal_facts({"title": "洗地机Stretch Max 540元", "price": 540, "content": "付1073收货返533，相当于540"})
        self.assertEqual(facts["cash_total"], "1073")

    def test_gift_credit_net_and_sent_red_packet_are_not_cash_prices(self):
        facts = deal_facts({"title": "酸奶200g×16盒 18.91元", "price": 18.91, "content": "【38.91/返20】下单返20元赠金，返卡后18.91元"})
        self.assertEqual(facts["cash_total"], "38.91")
        facts = deal_facts({"title": "康师傅泡面1桶 2.3元", "price": 2.3, "content": "88付5.3 送3鸿包 到手2.3元"})
        self.assertEqual(facts["cash_total"], "5.3")
        self.assertEqual(facts["quantity"]["unit"], "桶")

    def test_conflicting_variant_price_is_not_automatically_accepted(self):
        facts = deal_facts({"title": "男士内裤3条24.9元", "price": 24.9, "content": "男士3条29.9，女士3条24.9"})
        self.assertEqual(facts["status"], "multiple_products_or_variants")

    def test_missing_wet_wipe_count_is_recovered_from_explicit_body(self):
        facts = deal_facts({"title": "手口湿巾12包96元", "price": 96, "content": "手口湿巾80抽拍1发12包96元"})
        self.assertEqual(facts["quantity"]["amount"], "960")

    def test_duplicate_crossposts_do_not_use_two_batch_slots(self):
        items = [{"id":1,"title":"芬腾可安冬新法兰绒睡衣服男开衫加厚家居服套装59元","price":59}, {"id":2,"title":"芬腾可安法兰绒睡衣男开衫家居服套装59元","price":59}]
        chosen, skipped = select(items, CONFIG)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(skipped[0]["reason"], "duplicate_product_in_batch")

    def test_same_name_and_price_but_different_pack_count_are_not_duplicates(self):
        chosen, _ = select([{"id":1,"title":"好来牙膏200g×2支20元","price":20}, {"id":2,"title":"好来牙膏200g×4支20元","price":20}], CONFIG)
        self.assertEqual(len(chosen), 2)

    def test_nutritional_number_from_feed_is_not_a_verified_cash_price(self):
        facts = deal_facts({"title": "明治酸奶150g*3 4.0元", "price": 4, "content": "鲜奶组合不错，认养一头牛4.0原生蛋白，新鲜牛奶200ml*6瓶"})
        self.assertEqual(facts["status"], "cash_price_unverified")

    def test_same_nutrient_number_with_independent_payment_evidence_is_allowed(self):
        for payment in ("到手4元", "实付4", "4元/瓶"):
            facts = deal_facts({"title": "高蛋白牛奶250ml 4元", "price": 4, "content": "4.0原生蛋白 " + payment})
            self.assertEqual(facts["status"], "ready", payment)

    def test_conditions_keep_html_only_membership_and_special_coupon_entry(self):
        facts = deal_facts({"title": "酸奶150g*3 4元", "price": 4, "content": "到手4元", "content_html": "<p>PLUS会员兑换111-11券</p>"})
        self.assertIn("PLUS", facts["conditions"])
        self.assertIn("领券", facts["conditions"])
        facts = deal_facts({"title": "领先狗粮500g 7.4元", "price": 7.4, "content": "试吃 跳APP点特价领8.9-8券 7.4元"})
        self.assertTrue({"试吃", "领券", "APP特价"} <= set(facts["conditions"]))


class ReferenceTests(unittest.TestCase):
    def test_explicit_no_quote_is_distinct_from_unrecognised_price_markup(self):
        explicit = parse_wiki('<h1 class="pd-title">商品 羽绒服</h1><div class="sku-pd-price"><span class="no-price">暂无报价</span></div><aside>推荐其他商品158元起</aside>', 'https://wiki.smzdm.com/p/abc/')
        self.assertEqual(explicit['quote_status'], 'source_no_quote')
        self.assertEqual(explicit['prices'], [])
        unknown = parse_wiki('<h1 class="pd-title">商品 羽绒服</h1><div class="price-new">待加载</div>', 'https://wiki.smzdm.com/p/abc/')
        self.assertEqual(unknown['quote_status'], 'quote_unavailable')

    def test_missing_product_page_is_not_cached_as_an_ordinary_no_quote(self):
        with self.assertRaisesRegex(LookupStopped, "wiki_invalid_or_challenge_page"):
            parse_wiki('<html><script>captcha_bootstrap()</script></html>', 'https://wiki.smzdm.com/p/abc/')
        valid = parse_wiki('<h1 class="pd-title">商品 好来牙膏200g</h1><div>暂无报价</div>', 'https://wiki.smzdm.com/p/abc/')
        self.assertEqual(valid["prices"], [])
        self.assertTrue(valid["title"])
    def test_weak_noun_category_can_compare_flavours_but_cannot_claim_same_sku(self):
        value = similarity("虎邦牛肉蒜蓉辣酱200g×2瓶", "虎邦 鲁西牛肉酱 200g×2瓶")
        self.assertEqual(value["match"], "category_estimate")
        self.assertNotEqual(similarity("家用面粉1kg", "儿童奶粉1kg")["match"], "category_estimate")

    def test_catalog_expansion_keeps_product_pages_and_discards_deal_posts(self):
        rows = parse_catalog('<a href="https://wiki.smzdm.com/p/abc/">同款牛奶250ml×24盒</a><a href="https://www.smzdm.com/p/123/">一元历史爆料</a><a href="https://post.smzdm.com/p/abc/">用户文章</a>')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://wiki.smzdm.com/p/abc/")

    def test_canonical_dedup_of_specs_and_comment_pages(self):
        self.assertEqual(canonical_wiki("https://wiki.smzdm.com/p/abc123/canshu/"), "https://wiki.smzdm.com/p/abc123/")
        self.assertIsNone(canonical_wiki("https://www.smzdm.com/p/123/"))

    def test_search_only_accepts_product_titles_not_snippet_recommendations(self):
        page = '<li class="res-list"><h3><a data-mdurl="https://wiki.smzdm.com/p/abc/" href="https://www.so.com/link">奶粉</a></h3><p>推荐牙膏200g</p></li>'
        self.assertEqual(parse_search(page)[0]["title"], "奶粉")
        self.assertNotIn(similarity("好来牙膏200g", parse_search(page)[0]["title"])["match"], ("same_product_or_series", "category_estimate"))

    def test_prices_exclude_history_and_other_products(self):
        page = '<h1 class="pd-title">商品 好来牙膏200g×4</h1><span id="J_sku_price">37.9元起</span><a class="compare-price-item"><div class="shop-name">天猫</div><div class="compare-price">¥59.9</div></a><p>历史1元，推荐别的商品0.1元</p>'
        result = parse_wiki(page, "https://wiki.smzdm.com/p/abc/")
        self.assertEqual(sorted(Decimal(p["amount"]) for p in result["prices"]), [Decimal("37.9"), Decimal("59.9")])

    def test_membership_is_kept_separate_from_other_displayed_price(self):
        page = '<h1 class="pd-title">商品 面膜5片</h1><a class="compare-price-item"><span class="minimum-price">¥49</span><div class="palin">¥99</div></a>'
        result = parse_wiki(page, "https://wiki.smzdm.com/p/abc/")
        self.assertEqual(result["prices"], [{"amount": "49", "member": True, "mall": None, "basis": "comparison"}])

    def test_mismatched_dimensions_cannot_compare_by_weight_unit(self):
        facts = deal_facts({"title": "洗发水500ml 5元", "price": 5})
        ref = {"title": "洗发水500g", "quantity": quantity("500g"), "prices": [{"amount": "20"}], "url": "x", "captured_at": 0}
        self.assertIsNone(Lookup(CONFIG, None, None).compare(facts, ref))


class CacheAndBudgetTests(unittest.TestCase):
    def test_http_200_tencent_waf_challenge_stops_only_this_batch(self):
        class Response:
            status_code = 200
            content = b'<script src="https://ssl.captcha.qq.com/TCaptcha.js"></script><script>new TencentCaptcha();loadXMLDoc("/WafCaptcha", content)</script>'
            def raise_for_status(self):
                pass
        class Session:
            headers = {}
            def get(self, *args, **kwargs):
                return Response()
        http = Http(CONFIG, time.monotonic() + 3)
        with patch('price_lookup.requests.Session', Session):
            with self.assertRaisesRegex(LookupStopped, 'provider_challenge'):
                http.get('https://wiki.smzdm.com/p/abc/')
        self.assertTrue(http.disabled('wiki.smzdm.com'))
        self.assertEqual(http.stats['requests'], 1)

    def test_captcha_redirect_is_a_provider_failure_not_an_empty_search(self):
        class Response:
            status_code = 302
            headers = {"Location": "https://qcaptcha.so.com/"}
        class Session:
            headers = {}
            def get(self, *args, **kwargs):
                return Response()
        http = Http(CONFIG, time.monotonic() + 3)
        with patch("price_lookup.requests.Session", Session):
            with self.assertRaisesRegex(LookupStopped, "provider_challenge"):
                http.get("https://www.so.com/s?q=test")
        self.assertTrue(http.disabled("www.so.com"))
        self.assertEqual(http.stats["requests"], 1)

    def test_expired_deadline_prevents_new_requests(self):
        http = Http(CONFIG, time.monotonic() - 1)
        with self.assertRaises(LookupStopped):
            http.get("https://www.so.com/s?q=test")
        self.assertEqual(http.stats["requests"], 0)

    def test_concurrency_and_batch_network_budget(self):
        class Response:
            status_code = 200
            content = b"ok"
            def raise_for_status(self):
                pass
        class Session:
            headers = {}
            def get(self, *args, **kwargs):
                time.sleep(.03)
                return Response()
        config = {**CONFIG, "search_interval_seconds": 0, "max_requests_per_batch": 3}
        http = Http(config, time.monotonic() + 3)
        def perform(_):
            try:
                return http.get("https://www.so.com/s?q=test")
            except LookupStopped:
                return None
        with patch("price_lookup.requests.Session", Session):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(perform, range(4)))
        self.assertEqual(sum(value is not None for value in values), 3)
        self.assertLessEqual(http.stats["peak_active"], 2)
        self.assertEqual(http.stats["requests"], 3)

    def test_provider_limit_opens_circuit_instead_of_retrying(self):
        class Response:
            status_code = 429
        class Session:
            headers = {}
            def get(self, *args, **kwargs):
                return Response()
        http = Http({**CONFIG, "search_interval_seconds": 0}, time.monotonic() + 3)
        with patch("price_lookup.requests.Session", Session):
            with self.assertRaises(LookupStopped):
                http.get("https://www.so.com/s?q=test")
            with self.assertRaises(LookupStopped):
                http.get("https://www.so.com/s?q=another")
        self.assertEqual(http.stats["requests"], 1)

    def test_singleflight_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Cache(pathlib.Path(directory) / "cache.db")
            count = []
            def produce():
                count.append(1)
                time.sleep(.03)
                return [{"url": "x"}]
            def load():
                return cache.load("search", "same", Budget(CONFIG, time.monotonic() + 3), produce, 30, 1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                values = list(pool.map(lambda _: load(), range(2)))
            self.assertEqual(len(count), 1)
            self.assertEqual(values[0], values[1])
            cache.put("search", "expired", [], -1)
            self.assertIsNone(cache.get("search", "expired"))
            cache.close()

    def test_cached_pages_still_consume_the_logical_page_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Cache(pathlib.Path(directory) / "cache.db")
            cache.put("price", "x", {"title": "有效商品暂无报价", "prices": []}, 30)
            lookup = Lookup(CONFIG, cache, None)
            budget = Budget(CONFIG, time.monotonic() + 30)
            for _ in range(CONFIG["max_pages_per_item"]):
                lookup.page("x", budget)
            with self.assertRaises(LookupStopped):
                lookup.page("x", budget)
            self.assertEqual(budget.network, 0)
            cache.close()

    def test_changed_deal_price_reuses_reference_but_recomputes_gate(self):
        lookup = Lookup(CONFIG, None, None)
        ref = {"title": "好来牙膏200g×4", "quantity": quantity("200g×4"), "prices": [{"amount": "40"}], "url": "x", "captured_at": 0}
        cheap = lookup.compare(deal_facts({"title": "好来牙膏200g×4 10元", "price": 10}), ref)
        expensive = lookup.compare(deal_facts({"title": "好来牙膏200g×4 30元", "price": 30}), ref)
        self.assertLessEqual(Decimal(cheap["ratio"]), Decimal(".5"))
        self.assertGreater(Decimal(expensive["ratio"]), Decimal(".5"))


class ProviderThrottleTests(unittest.TestCase):
    def test_random_wiki_gap_survives_a_new_http_batch(self):
        starts = []
        ends = []
        class Response:
            status_code = 200
            content = b"ok"
            def raise_for_status(self):
                pass
        class Session:
            headers = {}
            def get(self, *args, **kwargs):
                starts.append(time.perf_counter())
                ends.append(time.perf_counter())
                return Response()
        with tempfile.TemporaryDirectory() as directory:
            config = {**CONFIG, "provider_state_path": str(pathlib.Path(directory) / 'providers.db'), "wiki_interval_seconds": .03, "wiki_interval_max_seconds": .06}
            with patch('price_lookup.requests.Session', Session), patch('price_lookup.random.uniform', return_value=.06):
                first = Http(config, time.monotonic() + 3)
                first.get('https://wiki.smzdm.com/p/test/')
                second = Http(config, time.monotonic() + 3)
                second.get('https://wiki.smzdm.com/p/test/')
            self.assertGreaterEqual(starts[1] - ends[0], .055)
            # Batch construction/SQLite setup can consume part of the gap;
            # only the actual interval between requests is required to persist.

    def test_same_provider_waits_after_completion_even_with_two_workers(self):
        events = []
        class Response:
            status_code = 200
            content = b"ok"
            def raise_for_status(self):
                pass
        class Session:
            headers = {}
            def get(self, *args, **kwargs):
                events.append(("start", time.perf_counter()))
                time.sleep(.02)
                events.append(("end", time.perf_counter()))
                return Response()
        http = Http({**CONFIG, "search_interval_seconds": .05}, time.monotonic() + 3)
        with patch("price_lookup.requests.Session", Session):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: http.get("https://www.so.com/s?q=test"), range(2)))
        self.assertEqual([event[0] for event in events], ["start", "end", "start", "end"])
        self.assertGreaterEqual(events[2][1] - events[1][1], .045)
        self.assertEqual(http.stats["peak_active"], 1)

    def test_different_providers_can_use_the_two_network_slots(self):
        barrier = threading.Barrier(2)
        class Response:
            status_code = 200
            content = b"ok"
            def raise_for_status(self):
                pass
        class Session:
            headers = {}
            def get(self, *args, **kwargs):
                barrier.wait(timeout=2)
                return Response()
        http = Http(CONFIG, time.monotonic() + 3)
        with patch("price_lookup.requests.Session", Session):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(http.get, ["https://www.so.com/s?q=test", "https://wiki.smzdm.com/p/test/"]))
        self.assertEqual(http.stats["peak_active"], 2)

    def test_provider_gap_survives_a_new_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "providers.db"
            first = ProviderThrottle(path)
            lease, _ = first.acquire("www.so.com", time.monotonic() + 3)
            # Start at request completion; release also persists the next time.
            # Windows monotonic() is too coarse for an 80 ms assertion.
            finished = time.perf_counter()
            first.release("www.so.com", lease, .08)
            second = ProviderThrottle(path)
            lease, _ = second.acquire("www.so.com", time.monotonic() + 3)
            second.release("www.so.com", lease, 0)
            self.assertGreaterEqual(time.perf_counter() - finished, .07)

    def test_another_process_cannot_bypass_the_provider_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "providers.db"
            first = ProviderThrottle(path)
            lease, _ = first.acquire("www.so.com", time.monotonic() + 3)
            code = "from price_lookup import ProviderThrottle, LookupStopped; import sys,time\nt = ProviderThrottle(sys.argv[1])\ntry:\n t.acquire('www.so.com', time.monotonic() + .15)\nexcept LookupStopped as error:\n print(str(error))\nelse:\n raise SystemExit('unexpected acquisition')"
            try:
                result = subprocess.run([sys.executable, "-c", code, str(path)], cwd=str(pathlib.Path(__file__).resolve().parents[1] / "scripts"), capture_output=True, text=True, timeout=10)
            finally:
                first.release("www.so.com", lease, 0)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "rate_queue_timeout")

    def test_rate_limit_stops_queued_work_and_persists_retry_after(self):
        class Response:
            status_code = 429
            headers = {"Retry-After": "10"}
        class Session:
            headers = {}
            def get(self, *args, **kwargs):
                time.sleep(.03)
                return Response()
        with tempfile.TemporaryDirectory() as directory:
            config = {**CONFIG, "provider_state_path": str(pathlib.Path(directory) / "providers.db")}
            http = Http(config, time.monotonic() + 3)
            def perform(_):
                with self.assertRaises(LookupStopped):
                    http.get("https://www.so.com/s?q=test")
            with patch("price_lookup.requests.Session", Session):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(perform, range(2)))
                restarted = Http(config, time.monotonic() + 3)
                self.assertFalse(restarted.disabled("www.so.com"))
                with self.assertRaisesRegex(LookupStopped, "rate_queue_timeout"):
                    restarted.get("https://www.so.com/s?q=again")
            self.assertGreaterEqual(restarted.throttle._read("www.so.com") - time.time(), 9)
            self.assertEqual(http.stats["requests"], 1)
            self.assertEqual(restarted.stats["requests"], 0)

    def test_new_batch_can_fetch_after_captcha_without_a_cooldown(self):
        starts = []
        class Response:
            status_code = 200
            def __init__(self, challenge):
                self.content = b'<script>new TencentCaptcha();loadXMLDoc("/WafCaptcha", content)</script>' if challenge else b'ok'
            def raise_for_status(self):
                pass
        class Session:
            headers = {}
            def get(self, *args, **kwargs):
                starts.append(time.monotonic())
                return Response(len(starts) == 1)
        with tempfile.TemporaryDirectory() as directory:
            config = {**CONFIG, 'provider_state_path': str(pathlib.Path(directory) / 'providers.db'), 'wiki_interval_seconds': .04, 'wiki_interval_max_seconds': .04}
            with patch('price_lookup.requests.Session', Session):
                first = Http(config, time.monotonic() + 3)
                with self.assertRaisesRegex(LookupStopped, 'provider_challenge'):
                    first.get('https://wiki.smzdm.com/p/test/')
                with self.assertRaisesRegex(LookupStopped, 'provider_stopped_for_batch'):
                    first.get('https://wiki.smzdm.com/p/test/')
                second = Http(config, time.monotonic() + 3)
                self.assertFalse(second.disabled('wiki.smzdm.com'))
                self.assertEqual(second.get('https://wiki.smzdm.com/p/test/'), 'ok')
            self.assertEqual(len(starts), 2)

    def test_legacy_cooldown_and_health_cache_do_not_block_new_batches(self):
        starts = []
        class Response:
            status_code = 200
            content = b'ok'
            def raise_for_status(self):
                pass
        class Session:
            headers = {}
            def get(self, *args, **kwargs):
                starts.append(time.time())
                return Response()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'providers.db'
            next_at = time.time() + .15
            legacy_until = time.time() + 7200
            db = sqlite3.connect(str(path))
            try:
                db.execute('CREATE TABLE providers (host TEXT PRIMARY KEY,next_at REAL,blocked_until REAL,failures INTEGER,failed_at REAL)')
                db.execute('INSERT INTO providers VALUES(?,?,?,?,?)', ('www.so.com', next_at, legacy_until, 4, time.time()))
                db.commit()
            finally:
                db.close()
            cache = Cache(pathlib.Path(directory) / 'cache.db')
            try:
                cache.put('health', 'www.so.com', {'reason': 'old_challenge'}, 7200)
                http = Http({**CONFIG, 'provider_state_path': str(path), 'search_interval_seconds': .01}, time.monotonic() + 3)
                http.cache = cache
                self.assertFalse(http.disabled('www.so.com'))
                self.assertEqual(http.throttle._read('www.so.com'), next_at)
                with patch('price_lookup.requests.Session', Session):
                    self.assertEqual(http.get('https://www.so.com/s?q=test'), 'ok')
                self.assertGreaterEqual(starts[0], next_at - .005)
                self.assertEqual(http.stats['requests'], 1)
                self.assertTrue(cache.get('health', 'www.so.com', track=False))
            finally:
                cache.close()

    def test_retry_after_http_date_remains_a_server_rate_limit(self):
        from email.utils import formatdate
        with tempfile.TemporaryDirectory() as directory:
            config = {**CONFIG, 'provider_state_path': str(pathlib.Path(directory) / 'providers.db')}
            http = Http(config, time.monotonic() + 3)
            http.block('www.so.com', 'http_429', formatdate(time.time() + 30, usegmt=True))
            restarted = Http(config, time.monotonic() + 3)
            self.assertFalse(restarted.disabled('www.so.com'))
            with patch('price_lookup.requests.Session') as session:
                with self.assertRaisesRegex(LookupStopped, 'rate_queue_timeout'):
                    restarted.get('https://www.so.com/s?q=test')
                session.assert_not_called()
            self.assertGreaterEqual(restarted.throttle._read('www.so.com') - time.time(), 28)

    def test_waiting_past_deadline_does_not_fetch_or_hold_the_gate(self):
        http = Http(CONFIG, time.monotonic() + 3)
        lease, _ = http.throttle.acquire("www.so.com", time.monotonic() + 3)
        http.throttle.release("www.so.com", lease, 10)
        with patch("price_lookup.requests.Session") as session:
            with self.assertRaisesRegex(LookupStopped, "rate_queue_timeout"):
                http.get("https://www.so.com/s?q=test")
            session.assert_not_called()
        self.assertTrue(http.throttle.locks["www.so.com"].acquire(blocking=False))
        http.throttle.locks["www.so.com"].release()


if __name__ == "__main__":
    unittest.main()
