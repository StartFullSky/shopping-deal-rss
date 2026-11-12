import json
import pathlib
import sys
import tempfile
import time
import unittest
from contextlib import closing
from decimal import Decimal
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from deal_text import deal_facts, quantity
from price_lookup import Budget, Cache, Lookup, canonical_catalog, canonical_wiki, parse_search
from price_snippets import exa_records, snippet_reference
from price_batch import evaluate


CONFIG = json.loads((pathlib.Path(__file__).resolve().parents[1] / "batch-price-config.json").read_text(encoding="utf-8"))
CONFIG["provider_state_path"] = None


def record(snippet, title="六神 芦荟沐浴露 200ml×2瓶", **extra):
    return {"title": title, "raw_title": title, "url": "https://wiki.smzdm.com/p/abc/", "type": "product",
            "snippet": snippet, "captured_at": time.time(), "search_provider": "360", **extra}


class SnippetExtractionTests(unittest.TestCase):
    def test_360_keeps_only_the_result_summary_and_its_date(self):
        html = '<li class="res-list"><h3><a data-mdurl="https://wiki.smzdm.com/p/abc/">六神沐浴露200ml【报价 价格 评测 怎么样】</a></h3><p class="res-desc"><span class="gray">2026年11月11日 - </span><span data-result-summary>现价29.9元</span></p><aside>推荐牙膏1元起</aside></li>'
        row = parse_search(html)[0]
        self.assertEqual(row["title"], "六神沐浴露200ml")
        self.assertEqual(row["snippet"], "现价29.9元")
        self.assertEqual(row["source_date"], "2026-11-11")
        page = snippet_reference(row, 1200)
        self.assertEqual(page["prices"][0]["amount"], "29.9")
        self.assertIsNone(page["source_updated_at"])

    def test_exa_splits_results_and_excludes_recommendation_prices(self):
        text = 'Title: 六神 沐浴露200ml【报价 价格】\nURL: https://wiki.smzdm.com/p/abc/\nPublished: N/A\nHighlights:\n# 商品 六神 沐浴露200ml\n\n19.9元起\n\n同品牌热门商品\n- 其他沐浴露2元起\n\n---\nTitle: 奶粉\nURL: https://wiki.smzdm.com/p/def/\nPublished: 2026-11-10\nText:\n# 商品 奶粉\n\n商品口碑\n推荐洗发水0.1元起'
        rows = exa_records(text, canonical_wiki, canonical_catalog)
        self.assertEqual(len(rows), 2)
        page = snippet_reference(rows[0], 1200)
        self.assertEqual([p["amount"] for p in page["prices"]], ["19.9"])
        self.assertTrue(page["prices"][0]["from_price"])
        self.assertIsNone(snippet_reference(rows[1], 1200))

    def test_history_coupon_rebate_shipping_and_ranges_are_not_quotes(self):
        for text in ("历史价格99元起", "原价￥99", "优惠券￥10", "满￥99减20", "返卡后￥5", "运费￥8",
                     "分期￥12", "￥20-30元起", "价格10至20元", "售价20元/100ml", "每瓶￥10", "价格5折"):
            self.assertIsNone(snippet_reference(record(text), 1200), text)

    def test_explicit_cash_wins_over_a_separate_original_price_or_coupon(self):
        page = snippet_reference(record("原价￥99；售价￥29.9，优惠券￥10"), 1200)
        self.assertEqual([p["amount"] for p in page["prices"]], ["29.9"])

    def test_unlabelled_numbers_and_nutrition_are_not_cash(self):
        self.assertIsNone(snippet_reference(record("200ml 4.0g蛋白 100人评价 价格评测"), 1200))

    def test_unit_bulk_and_thousands_prices_do_not_change_the_price_basis(self):
        for text in ("售价20元/斤", "售价20元/kg", "售价20元/抽", "拍2件售价20元"):
            self.assertIsNone(snippet_reference(record(text), 1200), text)
        page = snippet_reference(record("售价￥1,299.00"), 1200)
        self.assertEqual(page["prices"][0]["amount"], "1299.00")

    def test_another_products_or_variants_price_is_not_attributed_to_the_title(self):
        for text in ("好来牙膏200g 售价29元", "六神沐浴露100ml 售价99元", "六神沐浴露400g 售价20元"):
            self.assertIsNone(snippet_reference(record(text), 1200), text)

    def test_multisize_family_catalog_and_expired_quote_are_not_compared(self):
        self.assertIsNone(snippet_reference(record("￥20", title="六神 沐浴露 多规格"), 1200))
        self.assertIsNone(snippet_reference(record("￥20", type="catalog"), 1200))
        self.assertIsNone(snippet_reference(record("￥20", captured_at=time.time() - 1201), 1200))

    def test_membership_conditions_and_lowest_price_are_preserved(self):
        page = snippet_reference(record("PLUS会员售价￥19.9；售价29.9元"), 1200)
        self.assertEqual(min(Decimal(p["amount"]) for p in page["prices"]), Decimal("19.9"))
        self.assertTrue(page["prices"][0]["member"])

    def test_unknown_quantity_does_not_invent_a_physical_package_size(self):
        facts = deal_facts({"title": "六神 沐浴露200ml 5元", "price": 5})
        page = snippet_reference(record("售价20元", title="六神 沐浴露"), 1200)
        self.assertIsNone(Lookup(CONFIG, None, None).compare(facts, page))


class SnippetPipelineTests(unittest.TestCase):
    def test_recharge_and_obfuscated_coupon_conditions_are_not_unconditional(self):
        facts = deal_facts({"title": "361丁威迪3 116元", "price": 116, "content": "凑消费券 充970购勿唫97折 退"})
        self.assertTrue({"凑单", "消费券", "购物金充值"} <= set(facts["conditions"]))
        facts = deal_facts({"title": "斯凯奇运动鞋126.3元", "price": 126.3, "content": "进淘淦币拍，领80-30劵"})
        self.assertTrue({"淘金币", "领券"} <= set(facts["conditions"]))

    def test_usable_excerpt_skips_all_pages_and_replay_recomputes_discount(self):
        with tempfile.TemporaryDirectory() as directory, closing(Cache(pathlib.Path(directory) / "cache.db")) as cache:
            http = Mock()
            http.disabled.return_value = False
            lookup = Lookup(CONFIG, cache, http)
            row = record("现价20元")
            with patch.object(lookup, "search", return_value=[row]) as search, patch.object(lookup, "page") as page:
                first = lookup.run(deal_facts({"title": "六神 芦荟沐浴露200ml×2瓶 5元", "price": 5}), Budget(CONFIG, time.monotonic() + 30))
                self.assertEqual(Decimal(first["reference"]["ratio"]), Decimal("0.25"))
                self.assertEqual(first["reference"]["evidence_type"], "search_snippet")
                self.assertEqual(first["usable_references"], 1)
                self.assertTrue(first["reference"]["requires_review"])
                page.assert_not_called()
                search.reset_mock()
                second = lookup.run(deal_facts({"title": "六神 芦荟沐浴露200ml×2瓶 15元", "price": 15}), Budget(CONFIG, time.monotonic() + 30))
                self.assertEqual(Decimal(second["reference"]["ratio"]), Decimal("0.75"))
                search.assert_not_called()

    def test_blocked_wiki_still_allows_a_second_category_search(self):
        with tempfile.TemporaryDirectory() as directory, closing(Cache(pathlib.Path(directory) / "cache.db")) as cache:
            http = Mock()
            http.disabled.side_effect = lambda host: host == "wiki.smzdm.com"
            lookup = Lookup(CONFIG, cache, http)
            queries = []
            def search(query, budget):
                budget.take("search")
                queries.append(query)
                return [] if len(queries) == 1 else [record("售价20元", title="舒肤佳 沐浴露400ml")]
            budget = Budget(CONFIG, time.monotonic() + 30)
            with patch.object(lookup, "search", side_effect=search), patch.object(lookup, "page") as page:
                result = lookup.run(deal_facts({"title": "六神 芦荟沐浴露200ml×2瓶 1元", "price": 1}), budget)
            self.assertEqual(queries[1], "沐浴露")
            self.assertEqual(budget.searches, CONFIG["max_searches_per_item"])
            self.assertEqual(result["reference"]["match"], "category_estimate")
            self.assertEqual(Decimal(result["reference"]["equivalent_price"]), Decimal("20"))
            page.assert_not_called()

    def test_incompatible_excerpt_does_not_prevent_detail_fallback(self):
        with tempfile.TemporaryDirectory() as directory, closing(Cache(pathlib.Path(directory) / "cache.db")) as cache:
            http = Mock()
            http.disabled.return_value = False
            lookup = Lookup(CONFIG, cache, http)
            row = record("未知价格")
            detail = {"title": row["title"], "quantity": quantity(row["title"]), "prices": [{"amount": "20"}], "url": row["url"], "captured_at": time.time()}
            with patch.object(lookup, "search", return_value=[row]), patch.object(lookup, "page", return_value=detail) as page:
                result = lookup.run(deal_facts({"title": "六神 芦荟沐浴露200ml×2瓶 5元", "price": 5}), Budget(CONFIG, time.monotonic() + 30))
            page.assert_called_once()
            self.assertEqual(result["reference"]["evidence_type"], "product_page")

    def test_snippet_below_half_is_a_review_candidate_not_a_verified_alert(self):
        lookup = Mock()
        actual = Lookup(CONFIG, None, None)
        reference = actual.compare(deal_facts({"title": "六神 芦荟沐浴露200ml×2瓶 5元", "price": 5}), snippet_reference(record("20元起"), 1200))
        lookup.run.return_value = {"reference": reference}
        result = evaluate({"id": 1, "title": "六神 芦荟沐浴露200ml×2瓶 5元", "price": 5}, CONFIG, lookup, time.monotonic() + 30)
        self.assertTrue(result["price_gate_passed"])
        self.assertEqual(result["decision"], "estimated_candidate")
        self.assertIn("starting_price", result["reference"]["review_reasons"])
        self.assertIn("未核实", result["reference_label"])


if __name__ == "__main__":
    unittest.main()
