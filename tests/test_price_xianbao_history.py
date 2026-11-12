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
from deal_text import deal_facts
from price_batch import evaluate
from price_lookup import Budget, Cache, LookupStopped
from xianbao_history import XianbaoHistoryLookup, category_kind, comparable_samples, historical_queries, history_reference, group_products

CONFIG = json.loads((pathlib.Path(__file__).resolve().parents[1] / "batch-price-config.json").read_text(encoding="utf-8"))
CONFIG["provider_state_path"] = None
NOW = time.time()
BRANDS = ("六神", "舒肤佳", "多芬", "力士", "阿道夫", "玉兰油", "滴露", "安利", "强生", "澳宝", "威露士", "施巴")


def row(ident, price, size="500ml", brand="六神", days=2, **extra):
    return {"id": ident, "title": f"{brand}芦荟沐浴露{size} {price}元", "price": price,
            "url": f"https://new.xianbao.fun/haodan/{ident}.html", "published_at": NOW - days * 86400, **extra}


def products(prices=(10, 10, 10, 10, 10), size="500ml"):
    return [row(i, price, size, brand=BRANDS[i - 1], days=i + 1) for i, price in enumerate(prices, 1)]


class HistoricalPriceTests(unittest.TestCase):
    def compare(self, current, history, config=None):
        config = config or CONFIG
        samples, reasons = comparable_samples(current, deal_facts(current), history, config)
        return history_reference(deal_facts(current), samples, config), reasons

    def test_queries_use_category_and_all_synonyms_without_brand_or_pack_size(self):
        facts = deal_facts({"title": "全棉时代洗脸巾80抽*12包 46元", "price": 46})
        query = historical_queries(facts, CONFIG)
        self.assertEqual(set(query), {"洗脸巾", "棉柔巾", "洁面巾"})
        self.assertNotIn("全棉", query[0])
        self.assertNotIn("80", query[0])

    def test_capacity_normalisation_across_five_different_products(self):
        history = products((2, 6, 10, 12, 7))
        history[0]["title"] = "六神芦荟沐浴露200ml 2元"
        history[2]["title"] = "多芬芦荟沐浴露1L 10元"
        history[3]["title"] = "力士芦荟沐浴露1L 12元"
        ref, _ = self.compare(row(999, 2, days=0), history)
        self.assertEqual(ref["product_count"], 5)
        self.assertEqual(Decimal(ref["equivalent_price"]), Decimal(6))
        self.assertEqual(ref["history_scope"], "category_distinct_products")

    def test_current_self_future_and_old_records_never_set_the_baseline(self):
        current = row(999, 2, days=0)
        ref, reasons = self.compare(current, [current, row(1, 200, days=-1), row(2, 200, days=31), row(3, 10)])
        self.assertIsNone(ref)
        self.assertEqual(reasons["current_or_missing_id"], 1)
        self.assertEqual(reasons["outside_history_window"], 2)

    def test_current_offer_crossposts_do_not_compare_the_offer_with_itself(self):
        _, reasons = self.compare(row(999, 2, days=0), [row(1, 2, days=.1), row(2, 10), row(3, 12)])
        self.assertEqual(reasons["current_offer_crosspost"], 1)

    def test_duplicate_ids_and_same_day_crossposts_are_not_extra_quotes(self):
        one = row(1, 10, days=2)
        ref, reasons = self.compare(row(999, 2, days=0), [one, one, row(2, 10, days=2.01), row(3, 10, days=2.02)])
        self.assertIsNone(ref)
        self.assertEqual(reasons["duplicate_id"], 1)
        self.assertEqual(reasons["duplicate_offer"], 2)

    def test_one_product_on_different_days_is_still_only_one_product(self):
        current = row(999, 2, days=0)
        history = [row(i, 10, days=i * 2) for i in range(1, 7)]
        samples, _ = comparable_samples(current, deal_facts(current), history, CONFIG)
        self.assertEqual(len(samples), 6)
        self.assertEqual(len(group_products(samples)), 1)
        self.assertIsNone(history_reference(deal_facts(current), samples, CONFIG))

    def test_one_frequent_product_cannot_dominate_the_category_median(self):
        history = [row(i, 100 + i, days=i + 1) for i in range(1, 20)]
        history += [row(100 + i, price, brand=BRANDS[i], days=2 + i) for i, price in enumerate((4, 5, 6, 7), 1)]
        ref, _ = self.compare(row(999, 2, days=0), history)
        self.assertEqual(ref["product_count"], 5)
        self.assertEqual(ref["quote_count"], 23)
        self.assertEqual(Decimal(ref["equivalent_price"]), Decimal(6))

    def test_one_extreme_low_price_does_not_block_a_good_offer(self):
        ref, _ = self.compare(row(999, 3.8, days=0), products((2, 4, 5, 6, 8)))
        self.assertEqual(Decimal(ref["equivalent_price"]), Decimal(5))
        self.assertEqual(Decimal(ref["ratio"]), Decimal(".76"))

    def test_one_extreme_high_price_does_not_inflate_the_median(self):
        ref, _ = self.compare(row(999, 2, days=0), products((5, 6, 7, 8, 999)))
        self.assertEqual(Decimal(ref["equivalent_price"]), Decimal(7))

    def test_different_brands_with_the_same_price_are_not_crosspost_duplicates(self):
        ref, reasons = self.compare(row(999, 2, days=0), products())
        self.assertEqual(ref["product_count"], 5)
        self.assertEqual(reasons.get("duplicate_offer", 0), 0)

    def test_unknown_dimensions_dates_and_ambiguous_cash_are_excluded(self):
        ref, _ = self.compare(row(999, 2, days=0), [row(1, 10, "500g"), row(2, 10, published_at=None),
                               row(3, 5, content="返卡后5元，相当于5元购物卡")])
        self.assertIsNone(ref)

    def test_html_only_per_pack_price_is_converted_before_comparison(self):
        current = {**row(999, 2, days=0), "title": "澳贝棉柔巾1包 2元"}
        history = [{**r, "title": f"{BRANDS[i]}棉柔巾12包 3元/包", "price": 3,
                    "content_html": "<p>每包3元，拍12包</p>"} for i, r in enumerate(products())]
        ref, _ = self.compare(current, history)
        self.assertEqual(Decimal(ref["equivalent_price"]), Decimal(3))
        self.assertTrue(all(Decimal(s["cash_total"]) == 36 for s in ref["samples"]))

    def test_paper_aliases_and_sheet_units_allow_different_brands(self):
        current = {**row(999, 40, days=0), "title": "全棉时代洗脸巾80抽*12包 40元"}
        names = ("洗脸巾", "棉柔巾", "洁面巾", "棉柔巾", "洗脸巾")
        history = [{**r, "title": f"{BRANDS[i]}{names[i]}80张*12包 50元", "price": 50}
                   for i, r in enumerate(products())]
        ref, _ = self.compare(current, history)
        self.assertEqual(ref["product_count"], 5)
        self.assertEqual(Decimal(ref["equivalent_price"]), Decimal(50))

    def test_same_product_synonyms_and_pack_sizes_are_merged(self):
        current = {**row(999, 40, days=0), "title": "全棉时代洗脸巾80抽*12包 40元"}
        history = [{**row(1, 50, days=2), "title": "全棉时代洗脸巾80抽*12包 50元"},
                   {**row(2, 25, days=4), "title": "全棉时代棉柔巾80抽*6包 25元"}]
        samples, _ = comparable_samples(current, deal_facts(current), history, CONFIG)
        grouped = group_products(samples)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["quote_count"], 2)

    def test_same_merchant_product_id_merges_different_repost_titles(self):
        current = row(999, 2, days=0)
        history = [row(1, 10, content="https://item.jd.com/123456.html"),
                   row(2, 12, brand="力士", days=4, content="https://item.jd.com/123456.html")]
        samples, _ = comparable_samples(current, deal_facts(current), history, CONFIG)
        self.assertEqual(len(group_products(samples)), 1)

    def test_maximum_ten_products_and_brand_diversity_without_price_sorting(self):
        history = [row(i, 10, brand=f"六神A10{i}", days=2) for i in range(1, 10)]
        history += [row(100 + i, 1 + i, brand=BRANDS[i], days=3) for i in range(1, 4)]
        ref, _ = self.compare(row(999, 2, days=0), history)
        self.assertEqual(ref["product_count"], 10)
        self.assertEqual(ref["total_distinct_products"], 12)
        self.assertEqual(ref["brand_count"], 4)
        first = [s["id"] for s in ref["samples"]]
        for r in history:
            r["price"] = 999 - int(r["id"])
            r["title"] = r["title"].rsplit(" ", 1)[0] + f" {r['price']}元"
        other, _ = self.compare(row(999, 2, days=0), history)
        self.assertEqual(first, [s["id"] for s in other["samples"]])

    def test_appliance_capacity_is_not_package_weight_and_per_pack_is_not_total(self):
        for title in ("松下滚筒洗衣机12KG 3000元", "松下洗烘套装10KG 7000元"):
            facts = deal_facts({"title": title, "price": 3000})
            self.assertEqual((facts["quantity"]["amount"], facts["quantity"]["unit"]), ("1", "件"))
        facts = deal_facts({"title": "洗衣机12KG2台 6000元", "price": 6000})
        self.assertEqual((facts["quantity"]["amount"], facts["quantity"]["unit"]), ("2", "件"))
        facts = deal_facts({"title": "澳贝棉柔巾12包 3元/包", "price": 3})
        self.assertEqual(Decimal(facts["cash_total"]), Decimal(36))

    def test_speed_prefix_does_not_hide_the_brand(self):
        current = {**row(999, 8, days=0), "title": "速六神芦荟沐浴露500ml 8元", "content_html": "<p>速 六神芦荟沐浴露500ml 8元</p>"}
        self.assertEqual(deal_facts(current)["title"], "六神芦荟沐浴露500ml")

    def test_unit_price_without_yuan_and_bottle_count_without_separator(self):
        facts = deal_facts({"title": "立白小苍兰洗衣液16斤 2元", "price": 2, "content": "16斤洗衣液，2/斤"})
        self.assertEqual(Decimal(facts["cash_total"]), Decimal(32))
        for size in ("15ml 2瓶", "15ml2瓶", "15ml*2瓶"):
            facts = deal_facts({"title": f"兰蔻菁纯面霜{size} 182.5元", "price": 182.5})
            self.assertEqual(Decimal(facts["quantity"]["amount"]), Decimal(30))

    def test_softener_cannot_inflate_a_laundry_detergent_baseline(self):
        current = {**row(999, 40.2, days=0), "title": "立白大师香氛洗衣液13.4斤 40.2元"}
        history = [{**row(i, price, days=i * 2), "title": f"立白大师香氛洗衣液13.4斤 {price}元"}
                   for i, price in enumerate((44.22, 45.2, 45.48, 51.69, 55.71, 56.18), 1)]
        history.append({**row(88, .8), "title": "立白大师香氛柔顺剂梦幻玫瑰100g旅行装 2件"})
        ref, reasons = self.compare(current, history, {**CONFIG, "history_min_samples": 1})
        self.assertEqual(ref["quote_count"], 6)
        self.assertEqual(ref["product_count"], 1)
        self.assertEqual(Decimal(ref["equivalent_price"]), Decimal("48.585"))
        self.assertEqual(reasons["unrelated_product"], 1)

    def test_appliance_series_names_do_not_mix_categories_or_bundles(self):
        current = {**row(999, 7052, days=0), "title": "松下白月光5.0SE洗烘套装10KG 7052元"}
        titles = ("松下白月光2.0 515L五门法式多门冰箱", "松下白月光5.0滚筒洗衣机10kg",
                  "松下白月光5.0洗烘套装10kg", "松下白月光5.0滚筒洗烘一体机12KG")
        history = [{**row(i, 3000 + i), "title": title} for i, title in enumerate(titles, 1)]
        samples, reasons = comparable_samples(current, deal_facts(current), history, CONFIG)
        self.assertEqual([s["id"] for s in samples], ["3"])
        self.assertEqual(reasons["unrelated_product"], 3)

    def test_explicit_purchase_counts_apply_to_weight_and_piece_packs(self):
        facts = deal_facts({"title": "圣农鸡翅中2斤 3件110.7元", "price": 110.7})
        self.assertEqual(Decimal(facts["quantity"]["amount"]), Decimal(3000))
        facts = deal_facts({"title": "凯芮甜心小酪45粒拍2件 69.6元", "price": 69.6})
        self.assertEqual(Decimal(facts["quantity"]["amount"]), Decimal(90))

    def test_matching_body_pack_count_can_supply_missing_volume(self):
        facts = deal_facts({"title": "君乐宝纯牛奶24盒 75.7元", "price": 75.7,
                            "content_html": "<p>君乐宝纯牛奶200ml*24盒</p>"})
        self.assertEqual((Decimal(facts["quantity"]["amount"]), facts["quantity"]["unit"]), (Decimal(4800), "ml"))
        facts = deal_facts({"title": "君乐宝纯牛奶24盒 75.7元", "price": 75.7, "content": "牛奶200ml*12盒"})
        self.assertEqual(facts["quantity"]["unit"], "盒")

    def test_apple_brand_is_not_fruit_when_product_type_is_explicit(self):
        facts = deal_facts({"title": "苹果手机256GB 5000元", "price": 5000})
        self.assertEqual(historical_queries(facts, CONFIG), ["手机"])

    def test_gift_fruit_dried_fruit_and_banana_are_not_fresh_apples(self):
        for title in ("京鲜生猕猴桃24个送富士苹果", "春江月苹果干500g", "京鲜生广西苹果蕉4.5斤",
                      "山药煮苹果水105g*10袋", "苹果Mac mini M6 16G 512G"):
            facts = deal_facts({"title": title, "price": 30})
            self.assertNotEqual(category_kind(facts, CONFIG), "苹果")

    def test_memory_capacity_is_not_consumable_weight(self):
        facts = deal_facts({"title": "苹果Mac mini M6 16G 512G", "price": 3000})
        self.assertEqual(facts["quantity"]["unit"], "件")

    def test_vague_promotional_posts_do_not_prove_different_products(self):
        current = {**row(999, 20, days=0), "title": "Hape宝宝早教玩具开心农场"}
        history = [{**row(1, 1.1), "title": "还有 玩具助力5折 刚需"},
                   {**row(2, 39), "title": "昨天的小玩具39元 图里有hb 反28卡"}]
        samples, reasons = comparable_samples(current, deal_facts(current), history, CONFIG)
        self.assertEqual(samples, [])
        self.assertEqual(reasons["product_identity_unverified"], 2)

    def test_pet_toys_are_a_separate_category_from_childrens_toys(self):
        pet = deal_facts({"title": "华元宠具狗狗玩具发声乳胶橄榄球", "price": 27})
        child = deal_facts({"title": "Hape宝宝早教玩具开心农场", "price": 288})
        self.assertNotEqual(category_kind(pet, CONFIG), category_kind(child, CONFIG))

    def test_parent_brand_omission_does_not_make_a_new_product(self):
        current = {**row(999, 10, days=0), "title": "君乐宝纯牛奶12盒 10元"}
        history = [{**row(1, 20, days=2), "title": "金典纯牛奶12盒 20元"},
                   {**row(2, 22, days=4), "title": "伊利金典纯牛奶12盒 22元"}]
        samples, _ = comparable_samples(current, deal_facts(current), history, CONFIG)
        self.assertEqual(len(group_products(samples)), 1)

    def test_body_order_total_can_supply_two_case_quantity_without_doubling_explicit_title(self):
        body = "君乐宝4.0蛋白纯牛奶24盒plus拍2件💰75.7，才38一箱"
        facts = deal_facts({"title": "君乐宝纯牛奶24盒 75.7元", "price": 75.7, "content": body})
        self.assertEqual(Decimal(facts["quantity"]["amount"]), Decimal(48))
        facts = deal_facts({"title": "君乐宝纯牛奶24盒拍2件 75.7元", "price": 75.7, "content": body})
        self.assertEqual(Decimal(facts["quantity"]["amount"]), Decimal(48))
        facts = deal_facts({"title": "君乐宝纯牛奶24盒 75.7元", "price": 75.7,
                            "content": body.replace("plus", "")})
        self.assertEqual(Decimal(facts["quantity"]["amount"]), Decimal(48))


class HistoricalPipelineTests(unittest.TestCase):
    def test_twenty_percent_boundary_configurable_threshold_and_zero_price(self):
        for price, threshold, passes in ((8, 20, True), (8.01, 20, False), (8, 25, False), (0, 20, True)):
            config = {**CONFIG, "min_discount_pct": threshold}
            lookup = XianbaoHistoryLookup(config, None, None)
            with patch.object(lookup, "search", return_value=products()):
                result = evaluate(row(999, price, days=0), config, lookup, time.monotonic() + 30)
            self.assertEqual(result["price_gate_passed"], passes, (price, threshold))

    def test_searches_are_cached_and_new_current_prices_are_recomputed(self):
        with tempfile.TemporaryDirectory() as directory, closing(Cache(pathlib.Path(directory) / 'cache.db')) as cache:
            http = Mock()
            http.get.return_value = json.dumps({"code": 0, "data": {"items": products()}})
            lookup = XianbaoHistoryLookup(CONFIG, cache, http)
            cheap = evaluate(row(999, 5, days=0), CONFIG, lookup, time.monotonic() + 30)
            expensive = evaluate(row(999, 9, days=0), CONFIG, lookup, time.monotonic() + 30)
            self.assertTrue(cheap["price_gate_passed"])
            self.assertFalse(expensive["price_gate_passed"])
            self.assertEqual(http.get.call_count, 1)

    def test_search_page_is_part_of_cache_key(self):
        with tempfile.TemporaryDirectory() as directory, closing(Cache(pathlib.Path(directory) / 'cache.db')) as cache:
            http = Mock()
            http.get.return_value = json.dumps({"code": 0, "data": {"items": products()}})
            lookup = XianbaoHistoryLookup(CONFIG, cache, http)
            lookup.search('沐浴露', Budget(CONFIG, time.monotonic() + 30), page=1)
            lookup.search('沐浴露', Budget(CONFIG, time.monotonic() + 30), page=2)
            lookup.search('沐浴露', Budget(CONFIG, time.monotonic() + 30), page=2)
            self.assertEqual(http.get.call_count, 2)

    def test_second_page_seeks_more_products_even_when_one_brand_has_many_quotes(self):
        lookup = XianbaoHistoryLookup(CONFIG, None, None)
        pages = []
        def search(query, budget, page=1):
            budget.take("search")
            pages.append(page)
            return [row(i, i + 10, days=2) for i in range(1, 51)] if page == 1 else [{**r, "id": r["id"] + 100} for r in products()]
        with patch.object(lookup, "search", side_effect=search):
            result = evaluate(row(999, 2, days=0), CONFIG, lookup, time.monotonic() + 30)
        self.assertEqual(pages, [1, 2])
        self.assertEqual(result["reference"]["product_count"], 5)

    def test_insufficient_products_remain_unknown_and_keep_search_limit(self):
        lookup = XianbaoHistoryLookup(CONFIG, None, None)
        def search(query, budget, page=1):
            budget.take("search")
            return [row(i, i + 10, days=2) for i in range(1, 51)]
        with patch.object(lookup, "search", side_effect=search):
            result = evaluate(row(999, 2, days=0), CONFIG, lookup, time.monotonic() + 30)
        self.assertEqual(result["searches"], 2)
        self.assertEqual(result["distinct_products"], 1)
        self.assertEqual(result["reason"], "insufficient_distinct_products")
        self.assertNotIn("price_gate_passed", result)

    def test_business_errors_are_not_empty_history(self):
        with tempfile.TemporaryDirectory() as directory, closing(Cache(pathlib.Path(directory) / 'cache.db')) as cache:
            http = Mock()
            http.get.return_value = json.dumps({"code": 503, "error": "search_unavailable"})
            lookup = XianbaoHistoryLookup(CONFIG, cache, http)
            with self.assertRaisesRegex(LookupStopped, 'business_error'):
                lookup.search('沐浴露', Budget(CONFIG, time.monotonic() + 30))


if __name__ == '__main__':
    unittest.main()
