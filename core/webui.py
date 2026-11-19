"""本地 Web UI 服务：给界面提供静态文件与 JSON API

不用 pywebview 的 js_api 桥（在部分 WebView2 版本上偶发调用不返回），
改成同源 HTTP 接口，稳定、可 curl 调试，也方便将来换任何前端壳。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

ALLOWED = {
    "get_state", "poll", "start", "stop", "apply_system_proxy", "save_config",
    "test_upstream", "test_gateway", "update_rule_list", "export_pac",
    "export_env_script", "open_folder", "set_winhttp", "set_user_env",
    "set_autostart", "quit_app", "log_js",
    "pick_bat_file", "start_upstream",
}

CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "SocksGatewayUI/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):      # 静音访问日志
        pass

    # ---------- 静态文件 ----------
    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path in ("/", ""):
            path = "/index.html"
        root = self.server.ui_dir
        target = os.path.normpath(os.path.join(root, path.lstrip("/")))
        if not target.startswith(root) or not os.path.isfile(target):
            self.send_error(404, "Not Found")
            return
        ctype = CTYPES.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        try:
            with open(target, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(500, "Read Error")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------- JSON API ----------
    def do_POST(self):
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            self.send_error(404, "Not Found")
            return
        if self.headers.get("X-SG-Token", "") != self.server.token:
            self._json({"ok": False, "message": "会话令牌无效"}, 403)
            return
        name = path[len("/api/"):]
        if name not in ALLOWED or not callable(getattr(self.server.api, name, None)):
            self._json({"ok": False, "message": f"未知接口 {name}"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(min(length, 4 * 1024 * 1024)) if length else b"[]"
        try:
            args = json.loads(raw.decode("utf-8") or "[]")
        except (ValueError, UnicodeDecodeError):
            self._json({"ok": False, "message": "请求体不是合法 JSON"}, 400)
            return
        if not isinstance(args, list):
            args = [args]
        try:
            result = getattr(self.server.api, name)(*args)
        except TypeError as e:
            result = {"ok": False, "message": f"参数不匹配：{e}"}
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "message": f"{e.__class__.__name__}: {e}"}
        if not isinstance(result, dict):
            result = {"ok": True, "data": result}
        self._json(result)

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class UiServer:
    def __init__(self, api, ui_dir: str, port: int = 0):
        self.api = api
        self.ui_dir = os.path.abspath(ui_dir)
        # 支持用环境变量固定令牌（自动化测试/排障时方便）
        self.token = os.environ.get("SG_UI_TOKEN") or secrets.token_urlsafe(18)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.api = api
        self.httpd.token = self.token
        self.httpd.ui_dir = self.ui_dir
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="ui-server",
                                        daemon=True)

    def start(self) -> str:
        self._thread.start()
        return self.url

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
