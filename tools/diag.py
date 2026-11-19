"""开发辅助：把前端运行时的关键状态打印出来（python tools/diag.py）"""
import os
import sys
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import webview  # noqa: E402

import app as appmod  # noqa: E402
from core import config as cfgmod  # noqa: E402
from core import webui  # noqa: E402


def worker(window, core):
    time.sleep(7)
    for expr in [
        "document.querySelectorAll('.switch input').length + ' / checked=' + document.querySelectorAll('.switch input:checked').length",
        "String(document.querySelector('#tg-dns').checked) + ' ' + getComputedStyle(document.querySelector('#tg-dns').nextElementSibling).backgroundColor",
        "String(document.querySelector('#tg-applystart').checked)",
        "String(window.__sgLastError || 'none')",
        "document.title",
        "String(Object.keys(window.pywebview ? window.pywebview.api : {}).length)",
    ]:
        try:
            print(f"--- {expr}\n    = {window.evaluate_js(expr)}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"--- {expr}\n    ERR {e}", flush=True)
    core.shutdown()
    window.destroy()


def main():
    cfg = cfgmod.load()
    core = appmod.Core(cfg)
    api = appmod.Api(core)
    srv = webui.UiServer(api, os.path.join(BASE, "ui"))
    url = srv.start()
    print("ui:", url.split("?")[0], flush=True)
    window = webview.create_window(appmod.APP_TITLE, url, width=1000, height=700)
    api.window = window
    threading.Thread(target=worker, args=(window, core), daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
