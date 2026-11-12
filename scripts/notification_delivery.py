"""Program-controlled Bark transport. The model never receives this capability."""

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


def bark_endpoint(config):
    value = os.environ.get(config.get("endpoint_env", "BARK_URL"), "").strip()
    if not value:
        path = pathlib.Path(os.path.expandvars(config.get("secret_path", ""))).expanduser()
        if not config.get("secret_path") or not path.is_file() or os.name != "nt":
            raise DeliveryError("bark_endpoint_not_configured")
        command = "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.Security; $b=[IO.File]::ReadAllBytes($env:SHOPPING_BARK_SECRET_PATH); $p=[Security.Cryptography.ProtectedData]::Unprotect($b,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser); [Console]::Out.Write([Text.Encoding]::UTF8.GetString($p))"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=15,
                                env={**os.environ, "SHOPPING_BARK_SECRET_PATH": str(path)})
        if result.returncode:
            raise DeliveryError("bark_secret_decryption_failed")
        value = result.stdout.strip()
    endpoint = urlparse(value)
    key = endpoint.path.strip("/")
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or not re.fullmatch(r"[A-Za-z0-9_-]+", key)):
        raise DeliveryError("invalid_bark_endpoint")
    return endpoint.scheme + "://" + endpoint.netloc + "/push", key


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
