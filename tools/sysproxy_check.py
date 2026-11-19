"""临时占用 30 秒：接管系统代理 → 保持 → 还原，用于人工/自动化验证。"""
import asyncio
import json
import sys
import threading
import time

sys.path.insert(0, ".")

from core import config as cfgmod
from core import gateway as gwmod
from core import sysproxy

before = sysproxy.read_current()
print("原始:", json.dumps(before, ensure_ascii=False), flush=True)

cfg = cfgmod._deep_merge(cfgmod.DEFAULT_CONFIG, {
    "listen": {"port": 18081},
    "upstream": {"host": "127.0.0.1", "port": 18080, "scheme": "socks5h"},
    "bypass": ["127.0.0.1", "::1", "localhost", "<local>"], "rules": [],
    "default_action": "proxy"})
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
gw = gwmod.Gateway(cfg)
asyncio.run_coroutine_threadsafe(gw.start(), loop).result(5)

backup = sysproxy.apply_http_proxy("127.0.0.1", 18081, cfg["bypass"])
print("接管后:", json.dumps(sysproxy.read_current(), ensure_ascii=False), flush=True)
print("is_enabled:", sysproxy.is_enabled(), "| 请在 30 秒内用默认代理访问网络", flush=True)

for i in range(30):
    time.sleep(1)
    if i == 14:
        print("中途统计:", json.dumps(gw.snapshot()["stats"], ensure_ascii=False), flush=True)

sysproxy.disable(backup)
restored = sysproxy.read_current()
keys = ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL")
print("还原后:", json.dumps(restored, ensure_ascii=False), flush=True)
print("还原一致:", {k: before.get(k) for k in keys} == {k: restored.get(k) for k in keys}, flush=True)
print("最终流量:", json.dumps(gw.snapshot()["stats"], ensure_ascii=False), flush=True)
asyncio.run_coroutine_threadsafe(gw.stop(), loop).result(5)
