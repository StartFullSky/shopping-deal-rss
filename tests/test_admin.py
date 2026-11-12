import copy
import datetime
import http.client
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from admin_server import AdminApp, create_server
from deal_config import ConfigConflict, ConfigError, load_config, save, view
from deal_pipeline import execute
from run_history import RunBusy, display_rows

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / "product-filter-config.json").read_text(encoding="utf-8"))


def item(ident=1, title="洗发水20元", published=None):
    return {"id": str(ident), "title": title, "content": "商品原文", "source_feed": "kuan",
            "category": {"id": 17}, "url": f"https://new.xianbao.fun/kuan/{ident}.html",
            "published_at": time.time() if published is None else published}


class LocalFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = pathlib.Path(self.temp.name)
        self.path = self.directory / "product.json"
        self.monitor = self.directory / "monitor.json"
        self.config = copy.deepcopy(BASE)
        self.config["provider_state_path"] = None
        self.config["ai"].update(enabled=False, api_key_secret_path="missing-secret", api_key_env="TEST_ABSENT_API_KEY")
        self.config["delivery"].update(enabled=True, cold_start="send")
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.monitor.write_text(json.dumps({"delivery": {"enabled": False, "notificationStatePath": ".state/notify.db"},
                                            "monitor": {"maxAlertsPerRun": 1}}), encoding="utf-8")
        self.app = AdminApp(self.path, self.monitor)

    def record(self, rows=None, captured=None):
        rows = [item(), item(2, "纯红包活动")] if rows is None else rows
        snapshot = {"items": rows, "captured_at": captured or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "feed_counts": {"kuan": len(rows)}, "feed_categories": ["kuan", "weibo-chaohua"], "feed_errors": []}
        config = load_config(self.path, self.monitor)
        execute(config, self.directory / "cache.db", snapshot=snapshot, dry_run=True, history=self.app.history)
        return self.app.history.list()[0]["id"]


class ConfigTests(LocalFixture):
    def test_effective_root_overrides_are_shared_and_credentials_not_exposed(self):
        result = view(self.path, self.monitor)
        self.assertFalse(result["delivery_enabled"])
        self.assertNotIn("secret_path", json.dumps(result))
        self.assertNotIn("api_key", result["settings"]["ai"])
        self.assertEqual(load_config(self.path, self.monitor)["delivery"]["max_per_run"], 1)
        self.assertFalse(result["credential"]["configured"])

    def test_atomic_save_rejects_stale_revision_and_preserves_delivery_and_secret_fields(self):
        first = view(self.path, self.monitor)
        saved = save(self.path, self.monitor, {"exclude_keywords": ["抽奖", " 抽奖 ", "二手"]}, first["revision"])
        self.assertEqual(saved["settings"]["exclude_keywords"], ["抽奖", "二手"])
        raw = json.loads(self.path.read_text())
        self.assertTrue(raw["delivery"]["enabled"])
        self.assertEqual(raw["ai"]["api_key_secret_path"], "missing-secret")
        with self.assertRaises(ConfigConflict):
            save(self.path, self.monitor, {"include_keywords": ["纸巾"]}, first["revision"])

    def test_invalid_edits_cannot_enable_delivery_change_sources_or_remove_rate_limits(self):
        for patch_value in ({"delivery": {"enabled": True}}, {"feed_categories": ["haodan"]},
                            {"search_interval_seconds": 0}, {"max_items": True},
                            {"ai": {"api_key": "test-secret"}}, {"ai": {"base_url": "https://api.test/v1?key=secret"}}):
            with self.subTest(patch_value=patch_value), self.assertRaises(ConfigError):
                save(self.path, self.monitor, patch_value, view(self.path, self.monitor)["revision"])


class HistoryTests(LocalFixture):
    def test_audit_matches_normalized_titles_to_the_original_filter_reason(self):
        snapshot = {"items": [item(1, "洗发水　２０元")]}
        report = {"skipped": [{"id": "1", "title": "洗发水 20元", "reason": "keyword_excluded"}]}
        self.assertEqual(display_rows(snapshot, report)[0]["reason"], "keyword_excluded")

    def test_preview_archives_full_input_and_filtered_rows_without_notification_markers(self):
        config = load_config(self.path, self.monitor)
        config["exclude_keywords"] = ["红包"]
        execute(config, self.directory / "cache.db", snapshot={"items": [item(), item(2, "红包")]},
                dry_run=True, history=self.app.history)
        result = self.app.history.get(self.app.history.list()[0]["id"])
        self.assertEqual(result["phase"], "completed")
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][1]["reason"], "keyword_excluded")
        self.assertEqual(result["summary"]["sent_count"], 0)
        self.assertFalse(pathlib.Path(config["delivery"]["state_path"]).exists())

    def test_rule_preview_uses_capture_time_and_does_not_call_model_collect_or_save(self):
        old = time.time() - 86400
        captured = datetime.datetime.fromtimestamp(old, datetime.timezone.utc).isoformat()
        ident = self.record([item(1, "洗发水", old), item(2, "会员价洗发水", old)], captured)
        original_revision = view(self.path, self.monitor)["revision"]
        with patch("price_batch.collect", side_effect=AssertionError("no collection")), \
                patch("deal_pipeline.review_products", side_effect=AssertionError("no model")):
            result = self.app.preview_rules({"run_id": ident, "settings": {"exclude_keywords": ["会员"]}})
        self.assertEqual(result["before_kept"], 2)
        self.assertEqual(result["after_kept"], 1)
        self.assertEqual(result["newly_removed"], 1)
        self.assertEqual(result["rows"][1]["matched_keywords"], ["会员"])
        self.assertEqual(view(self.path, self.monitor)["revision"], original_revision)

    def test_run_claim_blocks_parallel_jobs_and_recovers_expired_jobs(self):
        ident = self.app.history.begin({}, "rev", exclusive=True)
        with self.assertRaises(RunBusy):
            self.app.history.begin({}, "rev", exclusive=True)
        with self.app.history.connection() as db:
            db.execute("UPDATE runs SET lease_until=0 WHERE id=?", (ident,))
        new = self.app.history.begin({}, "next", exclusive=True)
        self.assertNotEqual(ident, new)
        self.assertEqual(self.app.history.get(ident)["phase"], "interrupted")

    def test_reuse_trial_forces_preview_even_with_live_configuration(self):
        ident = self.record()
        self.monitor.write_text(json.dumps({"delivery": {"enabled": True}}), encoding="utf-8")
        with patch("deal_pipeline.BarkSender", side_effect=AssertionError("no actual send")), \
                patch("price_batch.collect", side_effect=AssertionError("no re-collection")):
            job = self.app.start_trial({"source": "reuse", "run_id": ident, "use_ai": False})
            for worker in list(self.app.workers):
                worker.join(3)
        run = self.app.history.get(job["run_id"])
        self.assertEqual(run["phase"], "completed")
        self.assertEqual(run["report"]["status"], "preview")
        self.assertEqual(run["summary"]["sent_count"], 0)
        self.assertFalse(run["config"]["delivery_enabled"])

    def test_cache_hits_are_summarized_by_namespace(self):
        ident = self.app.history.begin({}, "revision")
        self.app.history.finish(ident, {"metrics": {"cache": {"product_classification_v1_hit": 8, "other_miss": 2}}})
        self.assertEqual(self.app.history.get(ident)["summary"]["cache_hits"], 8)


class HttpTests(LocalFixture):
    def setUp(self):
        super().setUp()
        self.server = create_server(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.port = self.server.server_address[1]

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        values = {"Content-Type": "application/json", "X-Local-Token": self.app.token}
        values.update(headers or {})
        connection.request(method, path, json.dumps(body) if body is not None else None, values)
        response = connection.getresponse()
        status, content, result_headers = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return status, content, result_headers

    def test_static_page_and_assets_are_local_and_bootstrap_is_redacted(self):
        for path in ("/", "/assets/app.js", "/assets/style.css", "/api/bootstrap"):
            with self.subTest(path=path):
                status, content, headers = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        data = json.loads(self.request("GET", "/api/bootstrap")[1])
        self.assertTrue(data["preview_only"])
        self.assertNotIn("missing-secret", json.dumps(data))
        self.assertEqual(self.request("GET", "/product-filter-config.json")[0], 404)

    def test_host_origin_and_local_token_are_enforced(self):
        self.assertEqual(self.request("GET", "/api/bootstrap", headers={"Host": "evil.test"})[0], 403)
        self.assertEqual(self.request("POST", "/api/runs", {}, {"Origin": "https://evil.test"})[0], 403)
        self.assertEqual(self.request("POST", "/api/runs", {}, {"X-Local-Token": ""})[0], 403)
        self.assertEqual(self.request("POST", "/api/runs", {}, {"Content-Type": "text/plain"})[0], 415)

    def test_config_http_round_trip_and_conflict(self):
        config = json.loads(self.request("GET", "/api/config")[1])
        body = {"revision": config["revision"], "settings": {"include_keywords": ["纸巾"]}}
        self.assertEqual(self.request("POST", "/api/config", body)[0], 200)
        self.assertEqual(self.request("POST", "/api/config", body)[0], 409)
        self.assertEqual(self.request("POST", "/api/config", {"revision": view(self.path, self.monitor)["revision"], "settings": {"delivery": {"enabled": True}}})[0], 400)

    def test_rule_preview_http_returns_changed_rows_and_history_detail(self):
        ident = self.record()
        status, content, _ = self.request("POST", "/api/rules/preview", {"run_id": ident, "settings": {"exclude_keywords": ["红包"]}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["newly_removed"], 1)
        status, content, _ = self.request("GET", "/api/runs/" + ident)
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(content)["rows"]), 2)


if __name__ == "__main__":
    unittest.main()
