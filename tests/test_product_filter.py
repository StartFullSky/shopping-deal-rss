import collections
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from model_auth import load_api_key, load_codex_provider_token
from product_filter import ProductClient, cache_key, model_settings, parse_verdicts, run, select_items
from price_batch import main as batch_main
from price_batch import collect

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "product-filter-config.json").read_text(encoding="utf-8"))
CONFIG["provider_state_path"] = None


def item(ident, title, content=""):
    return {"id": str(ident), "title": title, "content": content,
            "url": f"https://new.xianbao.fun/haodan/{ident}.html", "published_at": time.time()}


class FakeClient:
    def __init__(self, labels=None, error=None, missing=False):
        self.labels, self.error, self.missing = labels or {}, error, missing
        self.calls = []
        self.stats = collections.Counter()

    def review(self, batch):
        self.calls.append(batch)
        self.stats["requests"] += 1
        if self.error:
            raise RuntimeError(self.error)
        return {str(i["id"]): {"is_product": self.labels.get(i["id"], True), "product_name": i["title"], "reason": "商品消息"}
                for i in batch if not self.missing or i is not batch[-1]}


class ProductFilterTests(unittest.TestCase):
    def test_collector_requests_only_the_two_selected_categories(self):
        from urllib.parse import parse_qs, urlparse
        http = Mock()
        http.config = copy.deepcopy(CONFIG)
        def get(url):
            query = parse_qs(urlparse(url).query)
            alias = query["cate"][0]
            self.assertEqual(query["limit"], ["20"])
            self.assertEqual(query["with_html"], ["1"])
            return json.dumps({"code": 0, "data": {"items": [
                {**item(alias, "商品"), "price": None, "category": {"id": CONFIG["feed_category_ids"][alias]}}
            ]}})
        http.get.side_effect = get
        snapshot = collect(http)
        self.assertEqual([parse_qs(urlparse(call.args[0]).query)["cate"][0] for call in http.get.call_args_list], ["kuan", "weibo-chaohua"])
        self.assertEqual(snapshot["feed_counts"], {"kuan": 1, "weibo-chaohua": 1})
        self.assertEqual([row["source_feed"] for row in snapshot["items"]], ["kuan", "weibo-chaohua"])
        self.assertFalse(snapshot["feed_errors"])

    def test_other_category_rows_are_not_silently_accepted_or_replaced_with_full_feed(self):
        http = Mock()
        http.config = copy.deepcopy(CONFIG)
        http.get.return_value = json.dumps({"code": 0, "data": {"items": [
            {**item(1, "不属于指定栏目的商品"), "category": {"id": 30}}
        ]}})
        snapshot = collect(http)
        self.assertEqual(snapshot["items"], [])
        self.assertEqual(http.get.call_count, 2)
        self.assertEqual({error["error"] for error in snapshot["feed_errors"]}, {"feed_category_mismatch"})

    def test_empty_category_config_cannot_fall_back_to_all_site(self):
        http = Mock()
        http.config = {**CONFIG, "feed_categories": []}
        with self.assertRaises(ValueError):
            collect(http)
        http.get.assert_not_called()

    def test_missing_price_specification_and_unfamiliar_series_do_not_block_review(self):
        rows = [item(1, "骆驼王者铠甲386元"), item(2, "好奇尿裤返卡后45.9元"),
                item(3, "ROEK唇釉任选2件"), item(4, "全棉时代洗脸巾", "没有标价")]
        chosen, skipped = select_items(rows, CONFIG)
        self.assertEqual(len(chosen), 4)
        self.assertFalse(skipped)
        with tempfile.TemporaryDirectory() as temp, patch("price_lookup.Http.get", side_effect=AssertionError("no price requests")):
            result = run(rows, CONFIG, pathlib.Path(temp) / "cache.db", FakeClient())
        self.assertEqual(result["decisions"], {"product": 4})
        self.assertEqual(result["price_lookup"], "disabled")
        self.assertEqual(result["sent_count"], 0)
        self.assertNotIn("reference", result["items"][0])

    def test_real_goods_and_non_goods_are_separate_and_unknown_is_not_false(self):
        rows = [item(1, "牛奶56元"), item(2, "工行领红包"), item(3, "速度")]
        client = FakeClient({"1": True, "2": False, "3": None})
        with tempfile.TemporaryDirectory() as temp:
            result = run(rows, CONFIG, pathlib.Path(temp) / "cache.db", client)
        self.assertEqual(result["decisions"], {"needs_review": 1, "non_product": 1, "product": 1})

    def test_batch_limit_caps_requests_and_unreviewed_rows_remain_unknown(self):
        rows = [item(i, f"商品型号{i}") for i in range(45)]
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp:
            result = run(rows, CONFIG, pathlib.Path(temp) / "cache.db", client)
        self.assertEqual([len(batch) for batch in client.calls], [20, 20])
        self.assertEqual(result["selected_count"], 40)
        self.assertEqual(len(result["skipped"]), 5)

    def test_keyword_and_duplicate_filters_run_before_model(self):
        row = item(1, "牛奶20元")
        config = {**CONFIG, "exclude_keywords": ["红包"]}
        chosen, skipped = select_items([row, {**row, "id": "2"}, item(3, "领红包")], config)
        self.assertEqual(len(chosen), 1)
        self.assertEqual({r["reason"] for r in skipped}, {"duplicate_message", "keyword_excluded"})

    def test_cache_reuses_classification_but_changed_content_is_reviewed(self):
        row = item(1, "某品牌商品")
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "cache.db"
            run([row], CONFIG, path, client)
            result = run([row], CONFIG, path, client)
            self.assertEqual(result["items"][0]["review_source"], "cache")
            run([{**row, "content": "新增购买条件"}], CONFIG, path, client)
        self.assertEqual(len(client.calls), 2)

    def test_model_and_prompt_version_invalidate_cache(self):
        ai = model_settings(CONFIG)
        row = item(1, "商品")
        key = cache_key(row, ai)
        self.assertNotEqual(key, cache_key(row, {**ai, "model": "other-model"}))
        with patch("product_filter.PROMPT_VERSION", 999):
            self.assertNotEqual(key, cache_key(row, ai))

    def test_auth_failure_stops_remaining_batches_and_does_not_cache_negative_verdicts(self):
        rows = [item(i, f"商品{i}") for i in range(40)]
        client = FakeClient(error="model_http_401")
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "cache.db"
            result = run(rows, CONFIG, path, client)
            self.assertEqual(result["decisions"], {"needs_review": 40})
            second = FakeClient()
            run(rows, CONFIG, path, second)
            self.assertEqual(len(second.calls), 2)
        self.assertEqual(len(client.calls), 1)

    def test_partial_response_preserves_valid_rows_and_marks_missing_rows_unknown(self):
        rows = [item(1, "牛奶"), item(2, "鞋子")]
        with tempfile.TemporaryDirectory() as temp:
            result = run(rows, CONFIG, pathlib.Path(temp) / "cache.db", FakeClient(missing=True))
        self.assertEqual(result["decisions"], {"product": 1, "needs_review": 1})
        self.assertIn("partial_model_result", result["errors"])

    def test_json_validation_rejects_string_booleans_duplicates_and_unknown_ids(self):
        for rows in ([{"id": "1", "is_product": "false", "reason": "r"}],
                     [{"id": "2", "is_product": True, "reason": "r"}],
                     [{"id": "1", "is_product": True, "reason": "r"}] * 2):
            with self.assertRaises(ValueError):
                parse_verdicts(json.dumps({"items": rows}), {"1"})
        valid = parse_verdicts('```json\n{"items":[{"id":"1","is_product":false,"reason":"纯红包"}]}\n```', {"1"})
        self.assertIs(valid["1"]["is_product"], False)

    def test_client_posts_only_to_model_without_tools_or_redirects_and_sanitizes_errors(self):
        config = copy.deepcopy(CONFIG)
        config["ai"]["request_interval_seconds"] = .001
        config["ai"]["api_key_env"] = "PRODUCT_TEST_API_KEY"
        session = Mock()
        session.post.return_value.status_code = 401
        session.post.return_value.text = "secret-should-not-leak"
        with patch.dict(os.environ, {"PRODUCT_TEST_API_KEY": "secret-should-not-leak"}):
            client = ProductClient(config, model_settings(config), time.monotonic() + 5, session)
            with self.assertRaisesRegex(RuntimeError, "^model_http_401$"):
                client.review([item(1, "牛奶")])
        args, kwargs = session.post.call_args
        self.assertTrue(args[0].endswith("/responses"))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("tools", kwargs["json"])
        self.assertEqual([entry["role"] for entry in kwargs["json"]["input"]], ["system", "user"])
        self.assertNotIn("web_search", kwargs["json"])

    def test_chat_completions_adapter_remains_supported(self):
        config = copy.deepcopy(CONFIG)
        config["ai"].update(api_kind="chat_completions", api_key_env="PRODUCT_TEST_API_KEY", request_interval_seconds=.001)
        session = Mock()
        session.post.return_value.status_code = 401
        with patch.dict(os.environ, {"PRODUCT_TEST_API_KEY": "test-key"}):
            client = ProductClient(config, model_settings(config), time.monotonic() + 5, session)
            with self.assertRaisesRegex(RuntimeError, "^model_http_401$"):
                client.review([item(1, "牛奶")])
        args, kwargs = session.post.call_args
        self.assertTrue(args[0].endswith("/chat/completions"))
        self.assertFalse(kwargs["json"]["chat_template_kwargs"]["enable_thinking"])

    def test_environment_credential_takes_precedence_over_local_secret(self):
        with patch.dict(os.environ, {"PRODUCT_TEST_API_KEY": "test-key"}):
            self.assertEqual(load_api_key({"api_key_env": "PRODUCT_TEST_API_KEY", "api_key_secret_path": "missing"}), "test-key")

    def test_codex_provider_token_is_loaded_without_being_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            (home / "config.toml").write_text('[model_providers.xiaomi]\nexperimental_bearer_token = "local-test-token"\n', encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": temp, "XIAOMI_API_KEY": ""}):
                self.assertEqual(load_codex_provider_token("xiaomi"), "local-test-token")
                self.assertEqual(load_api_key({"api_key_source": "codex_config", "codex_provider": "xiaomi",
                                               "api_key_env": "XIAOMI_API_KEY", "api_key_secret_path": ""}), "local-test-token")

    def test_model_endpoint_does_not_accept_query_credentials_or_plain_http(self):
        for url in ("http://example.com/v1", "https://user:pass@example.com/v1", "https://example.com/v1?key=secret"):
            config = copy.deepcopy(CONFIG)
            config["ai"]["base_url_env"] = "UNSET_PRODUCT_TEST_BASE"
            config["ai"]["base_url"] = url
            with self.assertRaises(ValueError):
                model_settings(config)

    def test_previous_batch_command_defaults_to_product_mode_and_preserves_cache_override(self):
        with patch.object(sys, "argv", ["price_batch.py", "--output", "out.json", "--input", "input.json", "--cache", "isolated.db", "--state", "outbox.db", "--no-ai", "--dry-run"]), patch("product_filter.main") as product_main:
            batch_main()
        forwarded = product_main.call_args.args[0]
        self.assertEqual(forwarded[forwarded.index("--cache") + 1], "isolated.db")
        self.assertEqual(forwarded[forwarded.index("--state") + 1], "outbox.db")
        self.assertIn("--no-ai", forwarded)
        self.assertIn("--dry-run", forwarded)
        self.assertTrue(forwarded[forwarded.index("--config") + 1].endswith("product-filter-config.json"))

    @unittest.skipUnless(os.name == "nt", "Windows monitor entry point")
    def test_monitor_dispatches_product_mode_without_legacy_price_or_delivery_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = pathlib.Path(temp)
            stub = directory / "python-stub.cmd"
            stub.write_text('@echo off\necho {"mode":"product_only","sent_count":0}\n', encoding="ascii")
            config = {"mode": "product_only", "productFilter": {"pythonCommand": str(stub)},
                      "delivery": {"enabled": False, "resultPath": str(directory / "result.json")}}
            path = directory / "monitor.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                     str(ROOT / "scripts" / "monitor.ps1"), "-ConfigPath", str(path)],
                                    capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"mode": "product_only", "sent_count": 0})


if __name__ == "__main__":
    unittest.main()
