"""Program-controlled notification transports. The model never receives this capability."""

import os
import pathlib
import re
import subprocess
import time
from urllib.parse import urlparse

import requests

from price_lookup import LookupStopped, ProviderThrottle


class DeliveryError(RuntimeError):
    def __init__(self, code, uncertain=False):
        super().__init__(code)
        self.uncertain = uncertain


def dpapi_secret(path_value, error_code):
    path = pathlib.Path(os.path.expandvars(path_value or "")).expanduser()
    if not path_value or not path.is_file() or os.name != "nt":
        raise DeliveryError(error_code)
    command = "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.Security; $b=[IO.File]::ReadAllBytes($env:SHOPPING_DELIVERY_SECRET_PATH); $p=[Security.Cryptography.ProtectedData]::Unprotect($b,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser); [Console]::Out.Write([Text.Encoding]::UTF8.GetString($p))"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=15,
                            env={**os.environ, "SHOPPING_DELIVERY_SECRET_PATH": str(path)})
    if result.returncode:
        raise DeliveryError(error_code)
    return result.stdout.strip()


def bark_endpoint(config):
    value = os.environ.get(config.get("endpoint_env", "BARK_URL"), "").strip()
    if not value:
        value = dpapi_secret(config.get("secret_path", ""), "bark_secret_decryption_failed")
    endpoint = urlparse(value)
    key = endpoint.path.strip("/")
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or not re.fullmatch(r"[A-Za-z0-9_-]+", key)):
        raise DeliveryError("invalid_bark_endpoint")
    return endpoint.scheme + "://" + endpoint.netloc + "/push", key


def wxpusher_settings(config):
    token = os.environ.get(config.get("app_token_env", "WXPUSHER_APP_TOKEN"), "").strip()
    if not token:
        token = dpapi_secret(config.get("app_token_secret_path", ""), "wxpusher_secret_decryption_failed")
    if not re.fullmatch(r"AT_[A-Za-z0-9_-]+", token):
        raise DeliveryError("invalid_wxpusher_app_token")
    endpoint = urlparse(config.get("endpoint", ""))
    if (endpoint.scheme != "https" or endpoint.hostname != "wxpusher.zjiecode.com"
            or endpoint.path != "/api/send/message" or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment):
        raise DeliveryError("invalid_wxpusher_endpoint")
    raw_topics = config.get("topic_ids", [])
    if isinstance(raw_topics, str):
        raw_topics = [value.strip() for value in raw_topics.split(",") if value.strip()]
    try:
        topic_ids = list(dict.fromkeys(int(value) for value in raw_topics))
    except (TypeError, ValueError):
        raise DeliveryError("invalid_wxpusher_topic_ids") from None
    if len(topic_ids) > 5 or any(value <= 0 for value in topic_ids):
        raise DeliveryError("invalid_wxpusher_topic_ids") from None
    recipient_mode = config.get("recipient_mode", "topic")
    if recipient_mode not in {"topic", "all_followers"}:
        raise DeliveryError("invalid_wxpusher_recipient_mode")
    if recipient_mode == "topic" and not topic_ids:
        raise DeliveryError("wxpusher_topic_required")
    user_list_endpoint = urlparse(config.get("user_list_endpoint", "https://wxpusher.zjiecode.com/api/fun/wxuser/v2"))
    if (user_list_endpoint.scheme != "https" or user_list_endpoint.hostname != "wxpusher.zjiecode.com"
            or user_list_endpoint.path != "/api/fun/wxuser/v2" or user_list_endpoint.username
            or user_list_endpoint.password or user_list_endpoint.query or user_list_endpoint.fragment):
        raise DeliveryError("invalid_wxpusher_user_list_endpoint")
    return token, topic_ids, endpoint.geturl(), recipient_mode, user_list_endpoint.geturl()


def bounded_utf8(value, max_bytes):
    text = str(value or "")
    encoded = text.encode("utf-8")
    return text if len(encoded) <= max_bytes else encoded[:max_bytes].decode("utf-8", "ignore")


class BarkSender:
    def __init__(self, config, session=None):
        self.config = config["delivery"]
        self.endpoint, self.key = bark_endpoint(self.config)
        self.session = session or requests.Session()
        self.throttle = ProviderThrottle(config.get("provider_state_path"))

    def send(self, notification):
        host = urlparse(self.endpoint).hostname
        try:
            lease, _ = self.throttle.acquire(host, time.monotonic() + self.config["timeout_seconds"] + self.config["request_interval_seconds"] + 5)
        except LookupStopped:
            raise DeliveryError("bark_rate_wait_timeout") from None
        try:
            payload = {"device_key": self.key, "title": notification["title"], "body": notification["body"],
                       "url": notification["url"], "group": "shopping-deals", "level": "active"}
            try:
                response = self.session.post(self.endpoint, json=payload, timeout=self.config["timeout_seconds"], allow_redirects=False)
            except requests.ConnectTimeout:
                raise DeliveryError("bark_connect_timeout") from None
            except requests.RequestException:
                raise DeliveryError("bark_delivery_unconfirmed", uncertain=True) from None
            if response.status_code != 200:
                raise DeliveryError("bark_http_" + str(response.status_code), uncertain=response.status_code >= 500)
            try:
                data = response.json()
            except ValueError:
                raise DeliveryError("bark_ack_unconfirmed", uncertain=True) from None
            if not isinstance(data, dict) or type(data.get("code")) is not int:
                raise DeliveryError("bark_ack_unconfirmed", uncertain=True)
            if data["code"] != 200:
                raise DeliveryError("bark_rejected")
            return {"confirmed": True, "channel": "bark"}
        finally:
            self.throttle.release(host, lease, self.config["request_interval_seconds"])


class WxPusherSender:
    def __init__(self, config, session=None):
        self.config = config["delivery"]
        self.wxpusher = self.config["wxpusher"]
        self.token, self.topic_ids, self.endpoint, self.recipient_mode, self.user_list_endpoint = wxpusher_settings(self.wxpusher)
        self.session = session or requests.Session()
        self.throttle = ProviderThrottle(config.get("provider_state_path"))
        self.uids = []
        self.dynamic_topic_ids = []
        self._audience_loaded = False

    def _load_audience(self):
        if self._audience_loaded:
            return
        host = urlparse(self.user_list_endpoint).hostname
        all_rows = []
        for page in range(1, 101):
            try:
                lease, _ = self.throttle.acquire(host, time.monotonic() + self.config["timeout_seconds"] + self.config["request_interval_seconds"] + 5)
            except LookupStopped:
                raise DeliveryError("wxpusher_user_list_rate_wait_timeout") from None
            try:
                try:
                    response = self.session.get(
                        self.user_list_endpoint,
                        params={"appToken": self.token, "page": page, "pageSize": 100},
                        timeout=self.config["timeout_seconds"],
                        allow_redirects=False,
                    )
                except requests.ConnectTimeout:
                    raise DeliveryError("wxpusher_user_list_timeout") from None
                except requests.RequestException:
                    raise DeliveryError("wxpusher_user_list_unavailable") from None
                if response.status_code != 200:
                    raise DeliveryError("wxpusher_user_list_http_" + str(response.status_code))
                try:
                    data = response.json()
                except ValueError:
                    raise DeliveryError("wxpusher_user_list_invalid_response") from None
                if not isinstance(data, dict) or type(data.get("code")) is not int or data["code"] != 1000:
                    raise DeliveryError("wxpusher_user_list_rejected") from None
                payload = data.get("data")
                records = payload.get("records") if isinstance(payload, dict) else None
                if not isinstance(records, list):
                    raise DeliveryError("wxpusher_user_list_invalid_response") from None
                all_rows.extend(records)
                total = payload.get("total") if isinstance(payload, dict) else None
                if not records or (type(total) is int and len(all_rows) >= total):
                    break
            finally:
                self.throttle.release(host, lease, self.config["request_interval_seconds"])
        uids = []
        topic_ids = []
        for row in all_rows:
            if not isinstance(row, dict) or row.get("reject"):
                continue
            uid = str(row.get("uid") or "").strip()
            if uid:
                uids.append(uid)
            else:
                try:
                    topic_id = int(row.get("appOrTopicId"))
                except (TypeError, ValueError):
                    continue
                if topic_id > 0:
                    topic_ids.append(topic_id)
        self.uids = list(dict.fromkeys(uids))
        self.dynamic_topic_ids = list(dict.fromkeys(topic_ids))
        self._audience_loaded = True

    def _post(self, payload):
        host = urlparse(self.endpoint).hostname
        try:
            lease, _ = self.throttle.acquire(host, time.monotonic() + self.config["timeout_seconds"] + self.config["request_interval_seconds"] + 5)
        except LookupStopped:
            raise DeliveryError("wxpusher_rate_wait_timeout") from None
        try:
            try:
                response = self.session.post(self.endpoint, json=payload, timeout=self.config["timeout_seconds"], allow_redirects=False)
            except requests.ConnectTimeout:
                raise DeliveryError("wxpusher_connect_timeout") from None
            except requests.RequestException:
                raise DeliveryError("wxpusher_delivery_unconfirmed", uncertain=True) from None
            if response.status_code != 200:
                raise DeliveryError("wxpusher_http_" + str(response.status_code), uncertain=response.status_code >= 500)
            try:
                data = response.json()
            except ValueError:
                raise DeliveryError("wxpusher_ack_unconfirmed", uncertain=True) from None
            if not isinstance(data, dict) or type(data.get("code")) is not int:
                raise DeliveryError("wxpusher_ack_unconfirmed", uncertain=True)
            if data["code"] != 1000:
                raise DeliveryError("wxpusher_rejected")
            return {"confirmed": True, "channel": "wxpusher", "accepted": True,
                    "deliveryVerification": "unavailable"}
        finally:
            self.throttle.release(host, lease, self.config["request_interval_seconds"])

    def send(self, notification):
        rich_content = notification.get("content_type") == "html"
        base_payload = {
            "appToken": self.token,
            "content": bounded_utf8(notification["body"], 62000)[:39000],
            "summary": str(notification["title"] or "商品线报")[:100],
            "contentType": 2 if rich_content else 1,
        }
        if notification.get("url") and not rich_content:
            base_payload["url"] = str(notification["url"])[:1000]
        if self.recipient_mode == "topic":
            return self._post({**base_payload, "topicIds": self.topic_ids})

        self._load_audience()
        uids = self.uids
        topic_ids = self.dynamic_topic_ids
        if not uids and not topic_ids:
            raise DeliveryError("wxpusher_no_subscribers")
        requests_to_send = [{**base_payload, "uids": uids[start:start + 2000]}
                            for start in range(0, len(uids), 2000)]
        requests_to_send.extend({**base_payload, "topicIds": topic_ids[start:start + 5]}
                                for start in range(0, len(topic_ids), 5))
        accepted = 0
        for payload in requests_to_send:
            try:
                self._post(payload)
                accepted += 1
            except DeliveryError:
                if accepted:
                    raise DeliveryError("wxpusher_partial_delivery", uncertain=True) from None
                raise
        return {"confirmed": True, "channel": "wxpusher", "accepted": True,
                "deliveryVerification": "unavailable", "recipientCount": len(uids) + len(topic_ids)}
