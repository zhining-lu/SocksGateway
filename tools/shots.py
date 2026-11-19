"""开发辅助：自动切换各页面并截图（python tools/shots.py）"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import webview  # noqa: E402
from PIL import ImageGrab  # noqa: E402

import app as appmod  # noqa: E402
from core import config as cfgmod  # noqa: E402
from core import webui  # noqa: E402

PAGES = ["overview", "server", "switch", "rulelist", "system"]
OUT = os.path.join(BASE, "shots")
os.makedirs(OUT, exist_ok=True)
TITLE = appmod.APP_TITLE


def grab(name: str):
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    u = ctypes.windll.user32
    hwnd = u.FindWindowW(None, TITLE)
    if hwnd:
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        box = (r.left, r.top, r.right, r.bottom)
    else:
        box = None
    ImageGrab.grab(bbox=box, all_screens=True).save(os.path.join(OUT, name + ".png"))
    print("shot", name, flush=True)


def worker(window, core):
    hwnd = 0
    for _ in range(80):
        hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
        if hwnd:
            break
        time.sleep(0.5)
    print("hwnd", hwnd, flush=True)
    time.sleep(5.0)
    grab("overview_raw")
    print("raw grab done", flush=True)
    for p in PAGES:
        try:
            window.evaluate_js(
                f"document.querySelector('.nav-item[data-page=\"{p}\"]').click()")
        except Exception as e:  # noqa: BLE001
            print("eval err", p, e, flush=True)
        time.sleep(1.2)
        grab(p)
        if p == "overview":
            window.evaluate_js("document.querySelector('.scroll').scrollTop = 430")
            time.sleep(0.6)
            grab("overview_log")
            window.evaluate_js("document.querySelector('.scroll').scrollTop = 0")
    core.shutdown()
    time.sleep(0.5)
    window.destroy()


def main():
    cfg = cfgmod.load()
    cfg["auto_start_gateway"] = False
    core = appmod.Core(cfg)
    api = appmod.Api(core)
    api.start()
    print("gateway up", api.cfg["listen"]["port"], flush=True)

    srv = webui.UiServer(api, os.path.join(BASE, "ui"))
    url = srv.start()
    window = webview.create_window(
        TITLE, url,
        width=1180, height=780, min_size=(980, 640), background_color="#f5f6f8")
    api.window = window
    threading.Thread(target=worker, args=(window, core), daemon=True).start()
    webview.start(debug=False, private_mode=False)


if __name__ == "__main__":
    main()
