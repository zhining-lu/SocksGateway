"""开发辅助：只起界面服务（不起窗口），供无头浏览器截图/调试

用法： python tools/serve.py [--no-start]
打印带 token 的完整 URL。
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import app as appmod  # noqa: E402
from core import config as cfgmod  # noqa: E402
from core import webui  # noqa: E402

cfg = cfgmod.load()
cfg["auto_start_gateway"] = False
cfg["system_proxy"]["apply_on_start"] = False
core = appmod.Core(cfg)
api = appmod.Api(core)
srv = webui.UiServer(api, os.path.join(BASE, "ui"))
url = srv.start()
if "--no-start" not in sys.argv:
    api.start()
print("URL " + url, flush=True)
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    core.shutdown()
