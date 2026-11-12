import collections
import copy
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from deal_pipeline import main, run
from notification_delivery import BarkSender, DeliveryError, WxPusherSender
from notification_state import NotificationState, message_record

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / "product-filter-config.json").read_text(encoding="utf-8"))


def item(ident=1, title="某品牌商品20元", body="下单购买", feed="kuan"):
    return {"id": str(ident), "title": title, "content": body, "source_feed": feed,
            "category": {"id": 17 if feed == "kuan" else 14},
            "url": f"https://new.xianbao.fun/{feed}/{ident}.html", "published_at": time.time()}


class Sender:
    def __init__(self, error=None, receipt=None):
        self.calls = []
        self.error, self.receipt = error, receipt or {"confirmed": True}

    def send(self, message):
        self.calls.append(message)
        if self.error:
            raise self.error
        return self.receipt


class Reviewer:
    def __init__(self, verdict=True, error=None):
        self.verdict, self.error = verdict, error
        self.calls = []
        self.stats = collections.Counter()

    def review(self, batch):
        self.calls.append(batch)
        self.stats["requests"] += 1
        if self.error:
            raise RuntimeError(self.error)
        return {str(row["id"]): {"is_product": self.verdict, "product_name": row["title"], "reason": "审核结论"}
                for row in batch}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = pathlib.Path(self.temp.name)
        self.cache = self.directory / "review-cache.db"
        self.state = self.directory / "notifications.db"
        self.config = copy.deepcopy(BASE)
        self.config["provider_state_path"] = None
        self.config["ai"]["enabled"] = False
        self.config["delivery"].update(enabled=True, cold_start="send", retry_delay_seconds=0)

    def cycle(self, rows, sender=None, reviewer=None, **kwargs):
        return run(rows, self.config, self.cache, state_path=self.state,
                   sender=sender, review_client=reviewer, **kwargs)

    def test_disabled_ai_sends_without_model_settings_key_or_cache(self):
        self.config["ai"] = {"enabled": False, "base_url": "not-a-valid-url"}
        sender = Sender()
        with patch("deal_pipeline.review_products", side_effect=AssertionError("AI must be skipped")):
            result = self.cycle([item()], sender)
        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["decisions"], {"rule_eligible": 1})
        self.assertIsNone(result["items"][0]["is_product"])
        self.assertFalse(self.cache.exists())

    def test_cached_ai_rejection_is_ignored_when_ai_is_disabled(self):
        row = item()
        self.config["ai"]["enabled"] = True
        first = self.cycle([row], Sender(), Reviewer(False))
        self.assertEqual(first["sent_count"], 0)
        self.config["ai"]["enabled"] = False
        second = self.cycle([row], Sender())
        self.assertEqual(second["sent_count"], 1)

    def test_ai_rejection_prevents_send_and_is_not_recorded_as_sent(self):
        self.config["ai"]["enabled"] = True
        sender = Sender()
        result = self.cycle([item()], sender, Reviewer(False))
        self.assertFalse(sender.calls)
        self.assertEqual(result["items"][0]["delivery_status"], "ai_rejected")
        self.assertNotIn("sent", result["delivery_states"])

    def test_ai_api_failure_falls_back_to_local_rules(self):
        self.config["ai"]["enabled"] = True
        result = self.cycle([item()], Sender(), Reviewer(error="model_http_401"))
        self.assertEqual(result["sent_count"], 1)
        self.assertTrue(result["review_degraded"])
        self.assertIn("model_http_401", result["errors"])

    def test_invalid_ai_configuration_does_not_break_base_delivery(self):
        self.config["ai"].update(enabled=True, base_url="invalid", base_url_env="UNSET_TEST_PROVIDER")
        result = self.cycle([item()], Sender())
        self.assertEqual(result["sent_count"], 1)
        self.assertTrue(result["review_degraded"])

    def test_unknown_can_bypass_or_defer_and_does_not_permanently_block_no_ai(self):
        self.config["ai"]["enabled"] = True
        self.config["ai"]["on_unknown"] = "defer"
        row = item()
        first = self.cycle([row], Sender(), Reviewer(None))
        self.assertEqual(first["sent_count"], 0)
        self.assertEqual(first["items"][0]["decision"], "deferred")
        self.config["ai"]["enabled"] = False
        self.assertEqual(self.cycle([row], Sender())["sent_count"], 1)

    def test_disabled_delivery_preview_never_calls_sender_or_writes_sent_state(self):
        self.config["delivery"]["enabled"] = False
        sender = Sender()
        result = self.cycle([item()], sender)
        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["sent_count"], 0)
        self.assertFalse(sender.calls)
        self.assertFalse(self.state.exists())

    def test_dry_run_overrides_enabled_delivery(self):
        sender = Sender()
        result = self.cycle([item()], sender, dry_run=True)
        self.assertEqual(result["status"], "preview")
        self.assertFalse(sender.calls)
        self.assertFalse(self.state.exists())

    def test_repeated_run_is_deduplicated_before_model_review(self):
        row = item()
        self.cycle([row], Sender())
        self.config["ai"]["enabled"] = True
        sender, reviewer = Sender(), Reviewer()
        second = self.cycle([row], sender, reviewer)
        self.assertEqual(second["selected_count"], 0)
        self.assertFalse(sender.calls)
        self.assertFalse(reviewer.calls)
        self.assertIn("already_sent", [r["reason"] for r in second["skipped"]])

    def test_same_content_under_another_source_id_is_deduplicated(self):
        first = item()
        self.cycle([first], Sender())
        second = item(2, feed="weibo-chaohua")
        result = self.cycle([second], Sender())
        self.assertEqual(result["sent_count"], 0)
        self.assertIn("duplicate_content", [r["reason"] for r in result["skipped"]])

    def test_changed_price_or_body_is_a_new_notification_revision(self):
        self.assertEqual(self.cycle([item()], Sender())["sent_count"], 1)
        self.assertEqual(self.cycle([item(title="某品牌商品18元")], Sender())["sent_count"], 1)
        self.assertEqual(self.cycle([item(title="某品牌商品18元")], Sender())["sent_count"], 0)

    def test_confirmed_failure_is_retried_without_marking_sent(self):
        row = item()
        first = self.cycle([row], Sender(error=DeliveryError("bark_rejected")))
        self.assertEqual(first["delivery_states"].get("failed"), 1)
        self.assertEqual(first["sent_count"], 0)
        second = self.cycle([row], Sender())
        self.assertEqual(second["sent_count"], 1)

    def test_uncertain_delivery_is_not_blindly_retried(self):
        row = item()
        first = self.cycle([row], Sender(error=DeliveryError("timeout", uncertain=True)))
        self.assertEqual(first["delivery_states"].get("delivery_unknown"), 1)
        sender = Sender()
        second = self.cycle([row], sender)
        self.assertFalse(sender.calls)
        self.assertIn("delivery_unconfirmed", [r["reason"] for r in second["skipped"]])

    def test_receipt_must_explicitly_confirm_success(self):
        first = self.cycle([item()], Sender(receipt={"confirmed": False}))
        self.assertEqual(first["sent_count"], 0)
        self.assertEqual(first["delivery_states"].get("delivery_unknown"), 1)

    def test_send_budget_keeps_unsent_items_in_persistent_queue(self):
        self.config["delivery"]["max_per_run"] = 1
        rows = [item(i, f"商品{i}") for i in range(1, 4)]
        sender = Sender()
        results = [self.cycle(rows, sender), self.cycle([], sender), self.cycle([], sender)]
        self.assertEqual([r["sent_count"] for r in results], [1, 1, 1])
        self.assertEqual(len(sender.calls), 3)

    def test_first_live_run_can_initialize_baseline_without_ai_or_send(self):
        self.config["delivery"]["cold_start"] = "baseline"
        self.config["ai"]["enabled"] = True
        sender, reviewer = Sender(), Reviewer()
        first = self.cycle([item()], sender, reviewer)
        self.assertEqual(first["status"], "initialized_baseline")
        self.assertEqual(first["delivery_states"], {"baseline": 1})
        self.assertFalse(sender.calls)
        self.assertFalse(reviewer.calls)
        second = self.cycle([item(), item(2, "新商品")], sender, reviewer)
        self.assertEqual(second["sent_count"], 1)

    def test_attempt_budget_stops_repeated_known_failures(self):
        self.config["delivery"]["max_attempts"] = 1
        row = item()
        self.cycle([row], Sender(error=DeliveryError("rejected")))
        sender = Sender()
        second = self.cycle([row], sender)
        self.assertFalse(sender.calls)
        self.assertIn("retry_exhausted", [r["reason"] for r in second["skipped"]])

    def test_local_source_and_keyword_rules_do_not_depend_on_ai(self):
        self.config["exclude_keywords"] = ["红包"]
        bad_source = item(2)
        bad_source.update(source_feed="haodan", url="https://new.xianbao.fun/haodan/2.html")
        sender = Sender()
        result = self.cycle([item(1, "领红包"), bad_source], sender)
        self.assertFalse(sender.calls)
        self.assertEqual(result["selected_count"], 0)

    def test_concurrent_cycles_only_one_can_review_and_send_same_message(self):
        self.config["ai"]["enabled"] = True
        entered, release = threading.Event(), threading.Event()
        reviewer = Reviewer()
        original = reviewer.review
        def blocking(batch):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test_timeout")
            return original(batch)
        reviewer.review = blocking
        sender, output, errors = Sender(), [], []
        row = item()
        def first_cycle():
            try:
                output.append(self.cycle([row], sender, reviewer))
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=first_cycle)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            second = self.cycle([row], Sender(), Reviewer())
            self.assertEqual(second["selected_count"], 0)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(errors)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(output[0]["sent_count"], 1)

    def test_already_sent_rows_do_not_hide_queued_unsent_work_at_batch_limit(self):
        self.config["max_items"] = 2
        self.config["delivery"]["max_per_run"] = 1
        old = item(1, "旧待发商品")
        new = item(2, "新商品")
        self.cycle([old, new], Sender())
        result = self.cycle([new, item(3, "更新商品")], Sender())
        self.assertEqual(result["sent_count"], 1)

    def test_feed_rows_beyond_review_limit_are_enqueued_and_not_hidden_by_sent_rows(self):
        self.config["max_items"] = 1
        self.config["delivery"]["max_per_run"] = 1
        rows = [item(i, f"商品{i}") for i in range(1, 4)]
        sender = Sender()
        results = [self.cycle(rows, sender) for _ in range(3)]
        self.assertEqual([r["sent_count"] for r in results], [1, 1, 1])
        self.assertEqual(len(sender.calls), 3)

    def test_empty_cold_start_does_not_initialize_baseline_on_failed_collection(self):
        self.config["delivery"]["cold_start"] = "baseline"
        first = self.cycle([], Sender())
        self.assertEqual(first["status"], "baseline_waiting_for_data")
        state = NotificationState(self.state, writable=False)
        try:
            self.assertFalse(state.initialized())
        finally:
            state.close()
        self.assertEqual(self.cycle([item()], Sender())["status"], "initialized_baseline")

    def test_cli_monitor_disabled_override_and_no_ai_skip_both_external_services(self):
        self.config["ai"] = {"enabled": True, "base_url": "invalid"}
        config_path = self.directory / "config.json"
        monitor_path = self.directory / "monitor.json"
        input_path = self.directory / "input.json"
        output_path = self.directory / "output.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        monitor_path.write_text(json.dumps({"delivery": {"enabled": False,
            "notificationStatePath": str(self.state)}, "monitor": {"maxAlertsPerRun": 1}}), encoding="utf-8")
        input_path.write_text(json.dumps({"items": [item()]}), encoding="utf-8")
        with patch("deal_pipeline.BarkSender", side_effect=AssertionError("must not send")), \
                patch("deal_pipeline.review_products", side_effect=AssertionError("must not review")), \
                contextlib.redirect_stdout(io.StringIO()):
            main(["--config", str(config_path), "--monitor-config", str(monitor_path),
                  "--input", str(input_path), "--output", str(output_path),
                  "--cache", str(self.cache), "--no-ai"])
        report = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "preview")
        self.assertEqual(report["eligible_count"], 1)
        self.assertFalse(report["review_enabled"])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.cache.exists())


class LeaseTests(unittest.TestCase):
    def test_unsent_superseded_revision_can_become_current_again(self):
        with tempfile.TemporaryDirectory() as temp:
            state = NotificationState(pathlib.Path(temp) / "state.db")
            try:
                first = message_record(item(title="商品20元"))
                changed = message_record(item(title="商品18元"))
                state.enqueue([first])
                state.enqueue([changed])
                self.assertEqual(state.block_reason(first, 1000, 3), "superseded")
                state.enqueue([first])
                self.assertIsNone(state.claim(first, "worker", 10, 1000, 3))
            finally:
                state.close()

    def test_expired_review_lease_cannot_send_after_another_worker_claims(self):
        with tempfile.TemporaryDirectory() as temp:
            clock = [100.0]
            state = NotificationState(pathlib.Path(temp) / "state.db", clock=lambda: clock[0])
            try:
                record = message_record(item())
                state.enqueue([record])
                self.assertIsNone(state.claim(record, "first", 10, 1000, 3))
                clock[0] = 111
                self.assertIsNone(state.claim(record, "second", 10, 1000, 3))
                self.assertFalse(state.begin_send(record["key"], "first", 10))
                self.assertTrue(state.begin_send(record["key"], "second", 10))
                clock[0] = 999
                self.assertEqual(state.claim(record, "third", 10, 1000, 3), "delivery_unconfirmed")
            finally:
                state.close()


class BarkTransportTests(unittest.TestCase):
    def test_http_ack_is_validated_and_transport_does_not_retry_itself(self):
        config = copy.deepcopy(BASE)
        config["provider_state_path"] = None
        config["delivery"]["endpoint_env"] = "TEST_BARK_ENDPOINT"
        config["delivery"]["request_interval_seconds"] = .001
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {"code": 200}
        with patch.dict(os.environ, {"TEST_BARK_ENDPOINT": "https://api.example.test/test-device-key"}):
            sender = BarkSender(config, session=session)
            receipt = sender.send({"title": "商品", "body": "正文", "url": "https://new.xianbao.fun/kuan/1.html"})
            self.assertTrue(receipt["confirmed"])
            session.post.return_value.json.return_value = {}
            with self.assertRaises(DeliveryError) as error:
                sender.send({"title": "商品", "body": "正文", "url": ""})
            self.assertTrue(error.exception.uncertain)
        self.assertEqual(session.post.call_count, 2)
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])


class WxPusherTransportTests(unittest.TestCase):
    def test_all_followers_mode_discovers_app_followers_without_topic(self):
        config = copy.deepcopy(BASE)
        config["provider_state_path"] = None
        config["delivery"].update(timeout_seconds=1, request_interval_seconds=.001, channel="wxpusher",
                                  wxpusher={"endpoint": "https://wxpusher.zjiecode.com/api/send/message",
                                            "user_list_endpoint": "https://wxpusher.zjiecode.com/api/fun/wxuser/v2",
                                            "app_token_env": "TEST_WXPUSHER_TOKEN",
                                            "app_token_secret_path": "missing",
                                            "recipient_mode": "all_followers",
                                            "topic_ids": []})
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {
            "code": 1000,
            "data": {"total": 2, "records": [
                {"uid": "UID_one", "type": 0, "appOrTopicId": 146732, "reject": False},
                {"uid": "UID_two", "type": 1, "appOrTopicId": 12345, "reject": False},
            ]},
        }
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {"code": 1000, "data": []}
        with patch.dict(os.environ, {"TEST_WXPUSHER_TOKEN": "AT_test-token"}):
            sender = WxPusherSender(config, session=session)
            receipt = sender.send({"title": "商品", "body": "正文", "url": "https://example.test/item"})
        self.assertTrue(receipt["confirmed"])
        self.assertEqual(receipt["recipientCount"], 2)
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["uids"], ["UID_one", "UID_two"])
        self.assertNotIn("topicIds", payload)

    def test_topic_broadcast_is_accepted_without_exposing_token(self):
        config = copy.deepcopy(BASE)
        config["provider_state_path"] = None
        config["delivery"].update(timeout_seconds=1, request_interval_seconds=.001, channel="wxpusher",
                                  wxpusher={"endpoint": "https://wxpusher.zjiecode.com/api/send/message",
                                            "app_token_env": "TEST_WXPUSHER_TOKEN",
                                            "app_token_secret_path": "missing",
                                            "topic_ids": [12345]})
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {"code": 1000, "data": [{"topicId": 12345, "code": 1000}]}
        with patch.dict(os.environ, {"TEST_WXPUSHER_TOKEN": "AT_test-token"}):
            sender = WxPusherSender(config, session=session)
            receipt = sender.send({"title": "商品" * 80, "body": "正文", "url": "https://example.test/item"})
        self.assertTrue(receipt["confirmed"])
        self.assertEqual(receipt["channel"], "wxpusher")
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["topicIds"], [12345])
        self.assertEqual(payload["contentType"], 1)
        self.assertLessEqual(len(payload["summary"]), 100)
        self.assertNotIn("AT_test-token", str(receipt))

    def test_topic_subscription_is_required(self):
        config = copy.deepcopy(BASE)
        config["provider_state_path"] = None
        config["delivery"].update(channel="wxpusher", wxpusher={
            "endpoint": "https://wxpusher.zjiecode.com/api/send/message",
            "app_token_env": "TEST_WXPUSHER_TOKEN", "app_token_secret_path": "missing", "topic_ids": []})
        with patch.dict(os.environ, {"TEST_WXPUSHER_TOKEN": "AT_test-token"}):
            with self.assertRaises(DeliveryError):
                WxPusherSender(config, session=Mock())


if __name__ == "__main__":
    unittest.main()
