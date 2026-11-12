"""Shared effective configuration and atomic, revision-checked local editing."""

import copy
import hashlib
import json
import os
import pathlib
import tempfile
import threading
from urllib.parse import urlparse


ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCK = threading.RLock()
DELIVERY_DEFAULTS = {
    "enabled": False, "state_path": ".state/notifications.sqlite3", "cold_start": "baseline",
    "max_per_run": 2, "max_attempts": 3, "retry_delay_seconds": 60,
    "lease_seconds": 180, "dedup_seconds": 604800, "timeout_seconds": 10,
    "request_interval_seconds": 5, "endpoint_env": "BARK_URL",
    "secret_path": "%USERPROFILE%/.codex/secrets/shopping-deal-rss-bark.bin",
}
EDITABLE = {"include_keywords", "exclude_keywords", "max_age_minutes", "max_items",
            "feed_limit", "search_interval_seconds", "ai"}
AI_EDITABLE = {"enabled", "base_url", "model", "on_error", "on_unknown"}


class ConfigError(ValueError):
    pass


class ConfigConflict(ConfigError):
    pass


def resolve_path(value, root):
    path = pathlib.Path(os.path.expandvars(str(value))).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def revision(config_path, monitor_path=None):
    digest = hashlib.sha256(pathlib.Path(config_path).read_bytes())
    if monitor_path:
        digest.update(pathlib.Path(monitor_path).read_bytes())
    return digest.hexdigest()


def load_config(config_path, monitor_path=None):
    path = pathlib.Path(config_path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    config["delivery"] = {**DELIVERY_DEFAULTS, **config.get("delivery", {})}
    if monitor_path:
        monitor = json.loads(pathlib.Path(monitor_path).read_text(encoding="utf-8-sig"))
        delivery = monitor.get("delivery", {})
        config["delivery"]["enabled"] = bool(delivery.get("enabled", False))
        for source, target in (("barkSecretPath", "secret_path"), ("notificationStatePath", "state_path")):
            if delivery.get(source):
                config["delivery"][target] = delivery[source]
        config["delivery"]["max_per_run"] = monitor.get("monitor", {}).get("maxAlertsPerRun", config["delivery"]["max_per_run"])
    for key in ("state_path", "secret_path"):
        config["delivery"][key] = str(resolve_path(config["delivery"][key], path.parent))
    for key, default in (("history_path", ".state/run-history.sqlite3"), ("provider_state_path", None)):
        if config.get(key, default):
            config[key] = str(resolve_path(config.get(key, default), path.parent))
    return config


def settings(config):
    result = {key: copy.deepcopy(config[key]) for key in EDITABLE - {"ai"}}
    result["ai"] = {key: config.get("ai", {}).get(key, "bypass" if key in ("on_error", "on_unknown") else None)
                    for key in AI_EDITABLE}
    return result


def validate_patch(patch):
    if not isinstance(patch, dict) or set(patch) - EDITABLE:
        raise ConfigError("仅支持修改页面中列出的规则字段")
    patch = copy.deepcopy(patch)
    for key in ("include_keywords", "exclude_keywords"):
        if key in patch:
            words = patch[key]
            if (not isinstance(words, list) or len(words) > 100
                    or any(not isinstance(w, str) or not w.strip() or len(w) > 80 for w in words)):
                raise ConfigError("关键词最多 100 个，每个 1–80 个字符")
            patch[key] = list(dict.fromkeys(w.strip() for w in words))
    for key, low, high in (("max_age_minutes", 1, 1440), ("max_items", 1, 100),
                           ("feed_limit", 1, 20), ("search_interval_seconds", 5, 60)):
        if key in patch and (type(patch[key]) is not int or not low <= patch[key] <= high):
            raise ConfigError(f"{key} 必须是 {low}–{high} 之间的整数")
    if "ai" in patch:
        ai = patch["ai"]
        if not isinstance(ai, dict) or set(ai) - AI_EDITABLE:
            raise ConfigError("不支持修改此模型字段")
        if "enabled" in ai and type(ai["enabled"]) is not bool:
            raise ConfigError("AI 开关必须是布尔值")
        for key in ("on_error", "on_unknown"):
            if key in ai and ai[key] not in ("bypass", "defer"):
                raise ConfigError("审核异常策略只能选择放行或暂缓")
        if "model" in ai and (not isinstance(ai["model"], str) or not ai["model"].strip() or len(ai["model"]) > 150):
            raise ConfigError("请输入有效的模型名")
        if "base_url" in ai:
            if not isinstance(ai["base_url"], str) or len(ai["base_url"]) > 500:
                raise ConfigError("请输入有效的 HTTPS API 地址")
            parsed = urlparse(ai["base_url"])
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                raise ConfigError("API 地址须为 HTTPS，且不含密钥、账号或查询参数")
    return patch


def apply_patch(config, patch):
    result = copy.deepcopy(config)
    patch = validate_patch(patch)
    for key, value in patch.items():
        if key == "ai":
            result["ai"] = {**result.get("ai", {}), **value}
        else:
            result[key] = value
    return result


def view(config_path, monitor_path=None):
    with LOCK:
        config = load_config(config_path, monitor_path)
        ai = config.get("ai", {})
        env_key = bool(os.environ.get(ai.get("api_key_env", "AGNES_API_KEY"), "").strip())
        secret = ai.get("api_key_secret_path")
        local_key = bool(secret and resolve_path(secret, pathlib.Path(config_path).resolve().parent).is_file())
        return {"revision": revision(config_path, monitor_path), "settings": settings(config),
                "sources": config["feed_categories"], "delivery_enabled": config["delivery"]["enabled"],
                "credential": {"configured": env_key or local_key, "source": "environment" if env_key else "local" if local_key else "none"},
                "effective_model": os.environ.get(ai.get("model_env", "AGNES_MODEL")) or ai.get("model"),
                "effective_base_url": os.environ.get(ai.get("base_url_env", "AGNES_BASE_URL")) or ai.get("base_url"),
                "environment_overrides": {key: bool(os.environ.get(ai.get(key + "_env", default)))
                                          for key, default in (("model", "AGNES_MODEL"), ("base_url", "AGNES_BASE_URL"))}}


def save(config_path, monitor_path, patch, expected_revision):
    with LOCK:
        if not expected_revision or revision(config_path, monitor_path) != expected_revision:
            raise ConfigConflict("配置已被修改，请重新加载后再保存")
        path = pathlib.Path(config_path)
        original = json.loads(path.read_text(encoding="utf-8-sig"))
        updated = apply_patch(original, patch)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary = pathlib.Path(stream.name)
                stream.write(json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
            os.replace(str(temporary), str(path))
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        return view(config_path, monitor_path)
