import json
import pathlib
import sys
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from deal_text import deal_facts, product_kind, quantity
from price_batch import evaluate, select
from xianbao_history import XianbaoHistoryLookup, category_kind, comparable_samples, group_products, history_reference

CONFIG = json.loads((pathlib.Path(__file__).resolve().parents[1] / "batch-price-config.json").read_text(encoding="utf-8"))
NOW = time.time()
BRANDS = ("六神", "多芬", "力士", "舒肤佳", "玉兰油")


def row(ident, brand, title=None, price=10):
    return {"id": ident, "title": title or f"{brand}沐浴露500ml {price}元", "price": price,
            "url": f"https://new.xianbao.fun/haodan/{ident}.html", "published_at": NOW - 86400 * (ident + 1)}


class PriceExtractionRepairTests(unittest.TestCase):
    def test_colloquial_unit_prices_without_api_price(self):
        for title, cash, unit, amount in (("金典纯牛奶1.6一盒", "1.6", "盒", "1"),
                                          ("君乐宝有机牛奶1.38r/瓶", "1.38", "瓶", "1"),
                                          ("牛奶24盒 1.6元/盒", "38.4", "盒", "24"),
                                          ("六神沐浴露500ml 💰3.8", "3.8", "ml", "500"),
                                          ("洗脸巾80抽 3R", "3", "抽", "80")):
            facts = deal_facts({"title": title})
            self.assertEqual(facts["status"], "ready", title)
            self.assertEqual(Decimal(facts["cash_total"]), Decimal(cash), title)
            self.assertEqual((facts["quantity"]["unit"], Decimal(facts["quantity"]["amount"])), (unit, Decimal(amount)))

    def test_body_can_supply_price_and_per_box_volume(self):
        facts = deal_facts({"title": "君乐宝纯牛奶24盒", "content_html": "<p>每盒200ml，到手1.6元/盒</p>"})
        self.assertEqual(Decimal(facts["cash_total"]), Decimal("38.4"))
        self.assertEqual(Decimal(facts["quantity"]["amount"]), Decimal(4800))
        self.assertEqual(facts["quantity"]["unit"], "ml")

    def test_reversed_explicit_pack_sizes_are_not_single_bottles(self):
        for title in ("纯牛奶24盒200ml", "纯牛奶24盒 200ml"):
            result = quantity(title)
            self.assertEqual(Decimal(result["amount"]), Decimal(4800))
            self.assertEqual(result["outer_unit"], "盒")
        self.assertEqual(Decimal(quantity("牛奶200ml赠2盒其他商品")["amount"]), Decimal(200))

    def test_nutrition_and_discount_numbers_are_not_cash_quotes(self):
        for title in ("君乐宝牛奶4.0蛋白24盒", "玩具5折", "沐浴露500ml"):
            self.assertIsNone(deal_facts({"title": title})["cash_total"])

    def test_feed_prefilter_keeps_colloquial_prices_without_indexed_price(self):
        current = {**row(999, "金典", title="金典纯牛奶1.6一盒", price=None), "published_at": NOW}
        chosen, _ = select([current], CONFIG)
        self.assertEqual(len(chosen), 1)

    def test_milk_powder_and_chocolate_are_not_drinking_milk(self):
        self.assertEqual(product_kind("伊利高钙低脂牛奶粉700g"), "奶粉")
        self.assertEqual(product_kind("好时牛奶巧克力500g"), "巧克力")

    def test_multi_piece_set_is_one_set_and_bulk_pack_price_is_multiplied(self):
        facts = deal_facts({"title": "韩束水乳礼盒5件套 82.28元", "price": 82.28})
        self.assertEqual((facts["quantity"]["amount"], facts["quantity"]["unit"]), ("1", "套"))
        facts = deal_facts({"title": "纸尿裤拍4件 45.9元", "price": 45.9, "content": "45.9元/包"})
        self.assertEqual(Decimal(facts["cash_total"]), Decimal("183.6"))

    def test_rebate_net_without_cash_payment_remains_ambiguous(self):
        facts = deal_facts({"title": "纸尿裤拍4件 45.9元", "price": 45.9, "content": "饭16卡45.9元/包"})
        self.assertEqual(facts["status"], "cash_quantity_ambiguous")

    def test_cup_capacity_is_not_a_consumable_volume(self):
        facts = deal_facts({"title": "膳魔师保温杯500ml 66.1元", "price": 66.1})
        self.assertEqual((facts["quantity"]["amount"], facts["quantity"]["unit"]), ("1", "件"))

    def test_watch_with_a_leather_strap_is_not_a_waist_belt(self):
        facts = deal_facts({"title": "西铁城光动能休闲皮带男表AO9000", "price": 850})
        self.assertEqual(category_kind(facts, CONFIG), "手表")

    def test_cosmetic_set_component_volume_does_not_turn_it_into_a_bottle_quote(self):
        facts = deal_facts({"title": "韩束水乳套装140ml+140ml 99元", "price": 99})
        self.assertEqual((facts["quantity"]["amount"], facts["quantity"]["unit"]), ("1", "套"))

    def test_subscription_total_is_not_a_single_litre_price(self):
        facts = deal_facts({"title": "每日鲜语4.0鲜牛奶1L定期购", "price": 897.3})
        self.assertEqual(facts["status"], "cash_quantity_ambiguous")


class HistoricalLookupRepairTests(unittest.TestCase):
    def test_couples_marketing_prefix_does_not_make_a_new_sweatshirt(self):
        current = {**row(999, "乔丹", title="乔丹运动卫衣34.9元", price=34.9), "published_at": NOW}
        history = [row(1, "斐乐", title="斐乐情侣款出彩卫衣175元", price=175),
                   row(2, "斐乐", title="斐乐出彩卫衣170元", price=170)]
        samples, _ = comparable_samples(current, deal_facts(current), history, CONFIG)
        self.assertEqual(len(group_products(samples)), 1)

    def test_five_distinct_products_stop_without_waiting_for_ten(self):
        current = {**row(999, "六神", price=2), "published_at": NOW}
        page = [row(i, b) for i, b in enumerate(BRANDS, 1)]
        page += [page[0]] * 45
        lookup = XianbaoHistoryLookup(CONFIG, None, None)
        with patch.object(lookup, "search", return_value=page) as search:
            result = evaluate(current, CONFIG, lookup, time.monotonic() + 30)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(result["reference"]["product_count"], 5)

    def test_noisy_results_switch_to_a_separate_synonym(self):
        current = {**row(999, "全棉", title="全棉时代洗脸巾80抽 2元", price=2), "published_at": NOW}
        calls = []
        def search(query, budget, page=1):
            budget.take("search")
            calls.append((query, page))
            if len(calls) == 1:
                return [row(i, "六神") for i in range(1, 51)]
            return [{**row(i + 100, b, title=f"{b}棉柔巾80抽 5元", price=5), "published_at": NOW - 86400 * 2}
                    for i, b in enumerate(BRANDS, 1)]
        lookup = XianbaoHistoryLookup(CONFIG, None, None)
        with patch.object(lookup, "search", side_effect=search):
            result = evaluate(current, CONFIG, lookup, time.monotonic() + 30)
        self.assertEqual(calls, [("洗脸巾", 1), ("棉柔巾", 1)])
        self.assertEqual(result["reference"]["product_count"], 5)

    def test_known_pack_counts_are_available_as_explicitly_coarse_references(self):
        current = {**row(999, "六神", title="六神沐浴露2瓶 4元", price=4), "published_at": NOW}
        history = [row(i, b, title=f"{b}沐浴露250ml*2瓶 6元", price=6) for i, b in enumerate(BRANDS, 1)]
        facts = deal_facts(current)
        samples, _ = comparable_samples(current, facts, history, CONFIG)
        ref = history_reference(facts, samples, CONFIG)
        self.assertEqual(ref["product_count"], 5)
        self.assertEqual(Decimal(ref["equivalent_price"]), Decimal(6))
        self.assertEqual(ref["comparison_basis"], "pack_count_unknown_current_size")
        self.assertTrue(ref["requires_review"])

    def test_unknown_pack_volume_is_never_invented_for_volume_comparison(self):
        current = {**row(999, "六神", price=2), "published_at": NOW}
        history = [row(i, b, title=f"{b}沐浴露1.6元/瓶", price=1.6) for i, b in enumerate(BRANDS, 1)]
        samples, _ = comparable_samples(current, deal_facts(current), history, CONFIG)
        self.assertEqual(samples, [])


if __name__ == "__main__":
    unittest.main()
