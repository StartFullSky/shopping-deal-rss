"""Loopback-only deal dashboard. All UI-triggered runs are previews."""

import argparse
import copy
import hmac
import json
import pathlib
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from deal_config import ConfigConflict, ConfigError, LOCK, ROOT, apply_patch, load_config, revision, save, settings, view
from deal_pipeline import execute, select_local
from product_filter import body_text
from deal_text import normalized
from run_history import RunBusy, RunHistory, captured_time


WEB = ROOT / "web" / "admin"


class AdminApp:
    def __init__(self, config_path, monitor_path=None, history_path=None):
        self.config_path, self.monitor_path = pathlib.Path(config_path), monitor_path
        config = load_config(config_path, monitor_path)
        self.history = RunHistory(history_path or config["history_path"])
        self.token = secrets.token_urlsafe(32)
        self.workers = set()
        self.worker_lock = threading.Lock()

    def bootstrap(self):
        return {"config": view(self.config_path, self.monitor_path), "runs": self.history.list(),
                "csrf_token": self.token, "preview_only": True}

    def start_trial(self, request):
        if set(request) - {"source", "use_ai", "run_id", "revision"}:
            raise ConfigError("不支持此试跑选项")
        source = request.get("source", "latest")
        if source not in ("latest", "reuse"):
            raise ConfigError("请选择获取最新线报或复用已有批次")
        with LOCK:
            config = load_config(self.config_path, self.monitor_path)
            current_revision = revision(self.config_path, self.monitor_path)
            if request.get("revision") and request["revision"] != current_revision:
                raise ConfigConflict("配置已变化，请刷新页面后试跑")
        enabled = request.get("use_ai", bool(config["ai"]["enabled"]))
        if type(enabled) is not bool:
            raise ConfigError("AI 开关必须是布尔值")
        snapshot = None
        if source == "reuse":
            previous = self.history.get(str(request.get("run_id", "")))
            if not previous or not previous["snapshot"].get("items"):
                raise ConfigError("请先选择一个有数据的历史批次")
            snapshot = copy.deepcopy(previous["snapshot"])
        config["ai"]["enabled"] = enabled
        config["delivery"]["enabled"] = False
        ident = self.history.begin({"settings": settings(config), "sources": config["feed_categories"],
                                    "delivery_enabled": False}, current_revision,
                                   origin="dashboard" if source == "latest" else "reuse", exclusive=True,
                                   timeout=config["batch_timeout_seconds"])

        def work():
            try:
                execute(config, ROOT / ".price-cache" / "products.sqlite3", snapshot=snapshot,
                        dry_run=True, history=self.history, run_id=ident)
            except Exception:
                # execute persists a sanitized failure code; never return exception text or credentials.
                pass
            finally:
                with self.worker_lock:
                    self.workers.discard(threading.current_thread())
        worker = threading.Thread(target=work, name="deal-preview-" + ident[:8], daemon=True)
        with self.worker_lock:
            self.workers.add(worker)
        worker.start()
        return {"run_id": ident}

    def preview_rules(self, request):
        if set(request) - {"run_id", "settings"}:
            raise ConfigError("不支持此试算选项")
        previous = self.history.get(str(request.get("run_id", "")))
        if not previous or not previous["snapshot"].get("items"):
            raise ConfigError("请先采集或选择一批线报")
        config = load_config(self.config_path, self.monitor_path)
        proposed = apply_patch(config, request.get("settings", {}))
        items = previous["snapshot"]["items"]
        observed_at = captured_time(previous)
        # Historical rules tests use the original observation time, never refresh item timestamps.
        before, _ = select_local(items, {**config, "max_items": max(len(items), config["max_items"])}, now_ts=observed_at)
        after, skipped = select_local(items, {**proposed, "max_items": max(len(items), proposed["max_items"])}, now_ts=observed_at)
        old_ids, new_ids = {str(i["id"]) for i in before}, {str(i["id"]) for i in after}
        skips = {str(i["id"]): i["reason"] for i in skipped}
        rows = []
        for item in items:
            ident = str(item.get("id") or item.get("url") or "")
            text = normalized(item.get("title")) + " " + body_text(item)
            rows.append({"source_id": ident, "title": item.get("title"), "before_kept": ident in old_ids,
                         "after_kept": ident in new_ids, "reason": skips.get(ident, "local_rules_passed"),
                         "matched_keywords": [w for w in proposed["exclude_keywords"] if w in text]})
        return {"input_count": len(items), "before_kept": len(before), "after_kept": len(after),
                "newly_removed": len(old_ids - new_ids), "newly_kept": len(new_ids - old_ids),
                "observed_at": observed_at, "rows": rows}


def create_server(app, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def headers_for(self, status, kind, length):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()

        def respond(self, data, status=200):
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.headers_for(status, "application/json; charset=utf-8", len(payload))
            self.wfile.write(payload)

        def valid_host(self):
            actual_port = self.server.server_address[1]
            allowed = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
            return self.headers.get("Host", "") in allowed

        def do_GET(self):
            if not self.valid_host():
                return self.respond({"error": "仅接受本地地址访问"}, 403)
            path = urlparse(self.path).path
            try:
                if path == "/api/bootstrap":
                    return self.respond(app.bootstrap())
                if path == "/api/config":
                    return self.respond(view(app.config_path, app.monitor_path))
                if path == "/api/runs":
                    return self.respond({"runs": app.history.list()})
                if re.fullmatch(r"/api/runs/[a-f0-9]{32}", path):
                    run = app.history.get(path.rsplit("/", 1)[1])
                    return self.respond(run if run else {"error": "记录不存在"}, 200 if run else 404)
                assets = {"/": ("index.html", "text/html; charset=utf-8"),
                          "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/assets/style.css": ("style.css", "text/css; charset=utf-8")}
                if path not in assets:
                    return self.respond({"error": "页面不存在"}, 404)
                filename, content_type = assets[path]
                payload = (WEB / filename).read_bytes()
                self.headers_for(200, content_type, len(payload))
                self.wfile.write(payload)
            except Exception as error:
                self.respond({"error": "读取失败", "code": type(error).__name__}, 500)

        def do_POST(self):
            if not self.valid_host():
                return self.respond({"error": "仅接受本地地址访问"}, 403)
            origin = self.headers.get("Origin")
            expected = "http://" + self.headers.get("Host", "")
            if origin and origin != expected:
                return self.respond({"error": "请求来源不匹配"}, 403)
            if not hmac.compare_digest(self.headers.get("X-Local-Token", ""), app.token):
                return self.respond({"error": "请刷新页面后重试"}, 403)
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                return self.respond({"error": "请使用 JSON 请求"}, 415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    return self.respond({"error": "请求过大或为空"}, 413)
                request = json.loads(self.rfile.read(size))
                if not isinstance(request, dict):
                    raise ConfigError("请求须为 JSON 对象")
                if self.path == "/api/config":
                    return self.respond(save(app.config_path, app.monitor_path, request.get("settings", {}), request.get("revision")))
                if self.path == "/api/runs":
                    return self.respond(app.start_trial(request), 202)
                if self.path == "/api/rules/preview":
                    return self.respond(app.preview_rules(request))
                return self.respond({"error": "接口不存在"}, 404)
            except (ConfigConflict, RunBusy) as error:
                self.respond({"error": str(error)}, 409)
            except (ConfigError, ValueError, UnicodeError) as error:
                self.respond({"error": str(error) if isinstance(error, ConfigError) else "请求格式错误"}, 400)
            except Exception as error:
                self.respond({"error": "操作失败", "code": type(error).__name__}, 500)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="Local deal dashboard (preview only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--config", type=pathlib.Path, default=ROOT / "product-filter-config.json")
    parser.add_argument("--monitor-config", type=pathlib.Path, default=ROOT / "monitor-config.json")
    args = parser.parse_args()
    app = AdminApp(args.config, args.monitor_config)
    server = create_server(app, args.port)
    address = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Local dashboard: {address} (preview only)", flush=True)
    if args.open_browser:
        import webbrowser
        webbrowser.open(address, new=2)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for worker in list(app.workers):
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
