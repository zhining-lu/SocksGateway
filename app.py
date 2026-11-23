"""Socks Gateway — Windows 本地 SOCKS5h 代理网关（桌面端）

把系统代理（WinINET）指向本机 HTTP 网关，网关再把流量按规则转交给
本地 SOCKS5h 代理（默认 127.0.0.1:18080），从而让浏览器之外的
Windows 应用也能走 SOCKS5 出网，且 DNS 由代理解析、不在本地泄漏。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core import __version__ as APP_VERSION  # noqa: E402
from core import config as cfgmod  # noqa: E402
from core import gateway as gwmod  # noqa: E402
from core import launcher as launchermod  # noqa: E402
from core import pac as pacmod  # noqa: E402
from core import rules as rulemod  # noqa: E402
from core import (  # noqa: E402
    rulesync,
    socks5,
    sysproxy,
    webui,
)

APP_TITLE = "Socks Gateway"

# 打包成 exe 后，ui/ 资源在临时解包目录里
RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR) if getattr(sys, "frozen", False) else BASE_DIR
UI_INDEX = os.path.join(RES_DIR, "ui", "index.html")

_LOG_PATH = os.path.join(cfgmod.config_dir(), "app.log")


def log_event(msg: str) -> None:
    """轻量日志：窗口模式没有控制台，出问题时可查 %APPDATA%\\SocksGateway\\app.log"""
    try:
        if os.path.exists(_LOG_PATH) and os.path.getsize(_LOG_PATH) > 512 * 1024:
            os.replace(_LOG_PATH, _LOG_PATH + ".1")
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


class Core:
    """在后台线程里跑 asyncio 事件循环，承载代理网关。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.loop = asyncio.new_event_loop()
        self.gateway = gwmod.Gateway(cfg)
        self._thread = threading.Thread(target=self._run, name="gateway-loop", daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro, timeout=20):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self):
        self.call(self.gateway.start())

    def stop(self):
        self.call(self.gateway.stop(), timeout=8)

    def shutdown(self):
        try:
            if self.gateway.running:
                self.stop()
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)


class Api:
    def __init__(self, core: Core):
        self.core = core
        self.cfg = core.cfg
        self.gateway = core.gateway
        self.window = None

    # ---------------- 状态 ----------------
    def get_state(self):
        try:
            snap = self.gateway.snapshot()
            return {
                "ok": True,
                "config": self.cfg,
                "status": {
                    "running": snap["running"],
                    "listen": snap["listen"],
                    "uptime": snap["uptime"],
                    "stats": snap["stats"],
                },
                "system_proxy": {
                    "applied": sysproxy.is_enabled(),
                    "current": sysproxy.read_current(),
                    "admin": sysproxy.is_admin(),
                },
                "env": {
                    "appdata": cfgmod.config_dir(),
                    "version": APP_VERSION,
                    "python": sys.version.split()[0],
                    "frozen": bool(getattr(sys, "frozen", False)),
                },
            }
        except Exception:
            import traceback
            log_event("get_state 失败:\n" + traceback.format_exc())
            raise

    def log_js(self, msg: str):
        """接收前端异常的落盘记录。"""
        log_event(f"[js] {msg}")
        return {"ok": True}

    def poll(self):
        snap = self.gateway.snapshot()
        return {
            "running": snap["running"],
            "listen": snap["listen"],
            "uptime": snap["uptime"],
            "stats": snap["stats"],
            "log": snap["log"],
            "system_proxy_applied": sysproxy.is_enabled(),
        }

    # ---------------- 开关 ----------------
    def _apply_user_env(self, enable: bool) -> tuple[bool, str]:
        """写入 / 清除命令行代理变量（主机与端口取自当前监听配置）。"""
        return sysproxy.set_user_env("127.0.0.1", int(self.cfg["listen"]["port"]), enable)

    def start(self):
        try:
            self._check_port()
            self.core.start()
            log_event(f"网关启动 {self.gateway.listen}")
            msg = f"网关已启动：{self.gateway.listen}"
            if self.cfg["system_proxy"].get("apply_on_start"):
                self.apply_system_proxy(True)
                msg += "，系统代理已接管"
            # 开关记住的选择：上次开着的话，这次启动自动写入
            if self.cfg["system_proxy"].get("write_user_env"):
                env_ok, env_msg = self._apply_user_env(True)
                if env_ok:
                    msg += "，命令行代理变量已写入"
                else:
                    log_event(env_msg)
            return {"ok": True, "message": msg}
        except OSError as e:
            return {"ok": False, "message": f"启动失败：端口被占用或不可用（{e}）"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"启动失败：{e}"}

    def stop(self):
        # 即使停止网关超时也要往下走：系统代理不还原、变量不清理 = 用户直接断网
        try:
            self.core.stop()
        except Exception as e:  # noqa: BLE001
            log_event(f"停止网关超时或失败：{e}")
        msg = "网关已停止"
        # 网关停了还留着系统代理 = 直接断网，这里必须一并还原
        try:
            if self._restore_proxy():
                msg += "，系统代理已还原"
        except Exception as e:  # noqa: BLE001
            log_event(f"还原系统代理失败：{e}")
        # 同理：环境变量还指向已关闭的端口，留着会让 git / npm 一直连不上
        if self.cfg["system_proxy"].get("write_user_env"):
            try:
                env_ok, _env_msg = self._apply_user_env(False)
                if env_ok:
                    msg += "，命令行代理变量已清除"
            except Exception as e:  # noqa: BLE001
                log_event(f"清除环境变量失败：{e}")
        log_event(msg)
        return {"ok": True, "message": msg}

    def _check_port(self):
        import socket
        port = int(self.cfg["listen"]["port"])
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        finally:
            s.close()

    # ---------------- 系统代理 ----------------
    BACKUP_FILE = os.path.join(cfgmod.config_dir(), "proxy-backup.json")

    def _save_backup(self, backup: dict) -> None:
        """备份写两份：配置里一份、独立文件一份（配置被删/损坏时还能还原）。"""
        self.cfg["backup"] = backup
        cfgmod.save(self.cfg)
        try:
            with open(self.BACKUP_FILE, "w", encoding="utf-8") as f:
                json.dump(backup, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _load_backup(self) -> dict | None:
        if self.cfg.get("backup"):
            return self.cfg["backup"]
        try:
            with open(self.BACKUP_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _restore_proxy(self) -> bool:
        """还原系统代理（按备份）。返回是否真的做了还原。"""
        backup = self._load_backup()
        if not backup:
            return False
        sysproxy.disable(backup)
        self.cfg["backup"] = None
        cfgmod.save(self.cfg)
        try:
            os.remove(self.BACKUP_FILE)
        except OSError:
            pass
        log_event("系统代理已还原")
        return True

    def _guard_backup(self, backup: dict) -> dict:
        """校正这次读到的"原始配置"，避免把网关地址当成用户原设置。

        如果当前代理已经指向本机网关，说明上一次接管没能正常还原（崩溃 / 强杀 /
        上次运行时被手动关掉），此时读到的值不是用户原本的配置：
          - 已经有备份  → 沿用旧备份，不要用网关地址覆盖它；
          - 没有备份    → 退化为「直连」，否则关闭代理后会还原成网关地址、再也关不掉。
        """
        host, port = "127.0.0.1", int(self.cfg["listen"]["port"])
        if not sysproxy.points_to(backup.get("proxy", ""), host, port):
            return backup

        existing = self._load_backup()
        if existing:
            old = sysproxy.normalize_backup(existing)
            if not sysproxy.points_to(old["proxy"], host, port):
                log_event("当前代理已指向网关且存在历史备份，沿用历史备份")
                return old
        log_event("当前代理已指向网关但无可用备份，按「直连」记录原状态")
        return {
            "flags": sysproxy.PROXY_TYPE_DIRECT,
            "proxy": "",
            "bypass": backup.get("bypass", ""),
            "pac": "",
        }

    def apply_system_proxy(self, enable: bool):
        try:
            if enable:
                if not self.gateway.running:
                    return {"ok": False, "message": "请先启动网关"}
                bypass = self.cfg.get("bypass") if self.cfg["system_proxy"].get("bypass_extra", True) else None
                backup = sysproxy.apply_http_proxy(
                    "127.0.0.1", int(self.cfg["listen"]["port"]), bypass)
                if backup:
                    self._save_backup(self._guard_backup(backup))
                return {"ok": True, "message": "系统代理已指向本机网关"}
            self._restore_proxy()
            return {"ok": True, "message": "已关闭系统代理"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"设置系统代理失败：{e}"}

    # ---------------- 上游代理服务 ----------------
    def pick_bat_file(self):
        """弹出文件对话框选脚本；取消时返回空路径，不算错误。"""
        path = launchermod.pick_script()
        return {"ok": True, "path": path, "message": ""}

    def script_output(self):
        """最近一次启动脚本的输出，供界面查看。"""
        return {"ok": True, "lines": launchermod.read_output()}

    def start_upstream(self):
        """执行启动脚本，不做端口探测——脚本是否幂等由用户自己决定。"""
        conf = self.cfg.get("upstream_launcher") or {}
        ok, msg = launchermod.run_script(conf.get("bat_path") or "")
        log_event(f"启动代理服务：{msg}")
        return {"ok": ok, "message": msg}

    def set_winhttp(self, enable: bool):
        if enable:
            ok, msg = sysproxy.set_winhttp("127.0.0.1", int(self.cfg["listen"]["port"]))
        else:
            ok, msg = sysproxy.clear_winhttp()
        return {"ok": ok, "message": msg}

    def set_user_env(self, enable: bool):
        ok, msg = self._apply_user_env(enable)
        if ok:
            # 记住选择，下次启动由 start() 按它自动写入
            sp = self.cfg.setdefault("system_proxy", {})
            if sp.get("write_user_env") != enable:
                sp["write_user_env"] = enable
                cfgmod.save(self.cfg)
        return {"ok": ok, "message": msg}

    # ---------------- 配置 ----------------
    def save_config(self, cfg: dict):
        try:
            port = int(cfg["listen"]["port"])
            up_port = int(cfg["upstream"]["port"])
            if not (1 <= port <= 65535) or not (1 <= up_port <= 65535):
                return {"ok": False, "message": "端口需在 1-65535 之间"}
            if port == up_port:
                return {"ok": False, "message": "网关端口不能与上游 SOCKS5 端口相同"}
        except (KeyError, ValueError, TypeError):
            return {"ok": False, "message": "配置格式不正确"}

        was_running = self.gateway.running
        old_listen = (self.cfg["listen"].get("host"), int(self.cfg["listen"]["port"]))
        self.cfg.clear()
        self.cfg.update(cfg)
        cfgmod.save(self.cfg)
        new_listen = (self.cfg["listen"].get("host"), int(self.cfg["listen"]["port"]))
        if was_running and old_listen != new_listen:  # 只有监听地址变化才需要重启
            self.core.stop()
            try:
                self.core.start()
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "message": f"配置已保存，但重启网关失败：{e}"}
        return {"ok": True, "message": "配置已保存"}

    # ---------------- 测试 ----------------
    def test_upstream(self):
        up = self.cfg["upstream"]
        t0 = time.time()

        async def probe():
            r, w = await socks5.connect("www.gstatic.com", 443, up["host"], int(up["port"]),
                                        up.get("username", ""), up.get("password", ""),
                                        self.cfg.get("dns_via_proxy", True), timeout=10)
            w.close()
            return (r, w)

        try:
            self.core.call(probe(), timeout=15)
            return {"ok": True, "message": f"上游 SOCKS5 可用，握手耗时 {int((time.time() - t0) * 1000)} ms"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"上游不可用：{e}"}

    def test_gateway(self, url: str = "http://www.gstatic.com/generate_204"):
        if not self.gateway.running:
            return {"ok": False, "message": "请先启动网关"}
        addr = f"127.0.0.1:{self.cfg['listen']['port']}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": f"http://{addr}", "https": f"http://{addr}"}))
        # 带上自检标记：网关侧强制走上游，不受规则列表 / 点击直连影响，
        # 这样按钮验的是「本机 → 网关 → 上游 → 目标」这条完整链路
        req = urllib.request.Request(url, headers={gwmod.FORCE_PROXY_HEADER: "1"})
        t0 = time.time()
        try:
            with opener.open(req, timeout=10) as resp:
                resp.read(2048)
            return {"ok": True, "message": f"经网关走上游访问成功（{resp.status}），{int((time.time() - t0) * 1000)} ms"}
        except urllib.error.HTTPError as e:
            return {"ok": True, "message": f"经网关访问成功（HTTP {e.code}）"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"经网关访问失败：{e}"}

    # ---------------- 规则列表 ----------------
    def update_rule_list(self):
        rl = self.cfg.get("rule_list") or {}
        url = (rl.get("url") or "").strip()
        if not url:
            return {"ok": False, "message": "规则列表网址为空"}
        text, err = self._fetch(url)
        if text is None:
            return {"ok": False, "message": f"下载失败：{err}"}
        parsed = rulemod.parse_rule_list(text, rl.get("format", "gfwlist"))
        if not parsed:
            return {"ok": False, "message": "规则列表为空或格式无法识别"}
        # direct（@@ 例外）排在前面：match_rule 是 first-match-wins，
        # 白名单落在屏蔽规则后面就永远轮不到
        merged = rulemod.merge_parsed(parsed)
        # 整体替换而不是就地改字段：后台自动更新与 UI 线程并发读时不会看到半成品
        updated = dict(rl)
        updated["rules"] = merged
        updated["count"] = len(merged)
        updated["last_update"] = rulesync.format_now()
        self.cfg["rule_list"] = updated
        cfgmod.save(self.cfg)
        return {"ok": True, "message": f"已更新 {len(merged)} 条规则（{updated['last_update']}）"}

    def auto_update_rule_list(self):
        """后台定时调用：没填网址、未到期都直接跳过，不产生请求。"""
        rl = self.cfg.get("rule_list") or {}
        if not (rl.get("url") or "").strip():
            return None
        interval = rulesync.normalize_interval(rl.get("update_interval_hours"))
        if not rulesync.is_due(rl.get("last_update"), interval):
            return None
        res = self.update_rule_list()
        log_event(f"规则列表自动更新：{res.get('message')}")
        return res

    def _fetch(self, url: str):
        req = urllib.request.Request(url, headers={"User-Agent": "SocksGateway/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", "ignore"), None
        except Exception as first_err:  # noqa: BLE001
            if self.gateway.running:  # 直连失败 -> 经自己的网关再试一次
                addr = f"127.0.0.1:{self.cfg['listen']['port']}"
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": f"http://{addr}", "https": f"http://{addr}"}))
                try:
                    with opener.open(req, timeout=20) as resp:
                        return resp.read().decode("utf-8", "ignore"), None
                except Exception as e:  # noqa: BLE001
                    return None, str(e)
            return None, str(first_err)

    # ---------------- 导出 / 打开 ----------------
    def export_pac(self):
        addr = f"127.0.0.1:{self.cfg['listen']['port']}"
        content = pacmod.build_pac(self.cfg, addr)
        path = os.path.join(cfgmod.config_dir(), "socks-gateway.pac")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "message": f"PAC 已导出：{path}", "path": path}

    def export_env_script(self):
        path = os.path.join(cfgmod.config_dir(), "enable-proxy.bat")
        with open(path, "w", encoding="mbcs", errors="ignore") as f:
            f.write(sysproxy.env_script("127.0.0.1", int(self.cfg["listen"]["port"])))
        return {"ok": True, "message": f"已导出：{path}", "path": path}

    def open_folder(self, path: str = ""):
        target = path or cfgmod.config_dir()
        try:
            if os.path.isfile(target):
                subprocess.Popen(["explorer", "/select,", target])
            else:
                os.startfile(target)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    def set_autostart(self, enable: bool):
        startup = sysproxy.startup_dir()
        link = os.path.join(startup, "SocksGateway.cmd")
        try:
            if enable:
                if getattr(sys, "frozen", False):
                    cmd = f'start "" "{sys.executable}"'
                else:
                    cmd = f'start "" "{sys.executable}" "{os.path.join(BASE_DIR, "app.py")}"'
                with open(link, "w", encoding="mbcs", errors="ignore") as f:
                    f.write("@echo off\r\n" + cmd + "\r\n")
                self.cfg["autostart"] = True
            else:
                if os.path.exists(link):
                    os.remove(link)
                self.cfg["autostart"] = False
            cfgmod.save(self.cfg)
            return {"ok": True, "message": "开机自启已" + ("开启" if enable else "关闭")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"设置开机自启失败：{e}"}

    def quit_app(self):
        if self.window is not None:
            self.window.destroy()
        return {"ok": True}


class RuleUpdater(threading.Thread):
    """规则列表定时更新。

    每 check_seconds 醒一次（默认 30 分钟），只有距上次更新超过配置间隔才真正
    发起下载，所以「每天一次」的精度是 ±30 分钟，不会产生多余的请求。
    下载走独立线程，不占网关的 asyncio 事件循环。
    """

    def __init__(self, api, check_seconds: int = 1800):
        super().__init__(name="rule-updater", daemon=True)
        self._api = api
        self._check = max(60, int(check_seconds))
        self._stop_evt = threading.Event()

    def run(self):
        while not self._stop_evt.is_set():
            try:
                self._api.auto_update_rule_list()
            except Exception as e:  # noqa: BLE001
                log_event(f"规则列表自动更新异常：{e}")
            self._stop_evt.wait(self._check)

    def stop(self):
        self._stop_evt.set()


def _launch_upstream_on_boot(api: Api) -> None:
    """每次启动都执行一次启动脚本；没配脚本就静默跳过。

    不做端口探测，也不管脚本是否幂等——那是脚本自己的事。
    """
    conf = api.cfg.get("upstream_launcher") or {}
    if not (conf.get("bat_path") or "").strip():
        return
    res = api.start_upstream()
    log_event(f"启动时拉起代理服务：{res.get('message')}")


def _headless(core: Core, api: Api):
    print("=" * 56)
    print(f" {APP_TITLE} {APP_VERSION} — headless")
    print("=" * 56)
    _launch_upstream_on_boot(api)
    res = api.start()
    print(res["message"])
    print(f" 上游: {core.cfg['upstream']['scheme']}://{core.cfg['upstream']['host']}:{core.cfg['upstream']['port']}")
    print(f" 网关: http://127.0.0.1:{core.cfg['listen']['port']}")
    print(" Ctrl+C 停止")
    updater = RuleUpdater(api)
    updater.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        updater.stop()
        if api.cfg.get("backup"):
            api.apply_system_proxy(False)
        core.shutdown()
        print("\n已退出")


def _window_size() -> tuple[int, int]:
    """按屏幕（逻辑）尺寸自适应，避免高 DPI 下窗口超出屏幕。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        return max(940, min(1180, int(sw) - 90)), max(600, min(780, int(sh) - 130))
    except Exception:
        return 1180, 780


def _single_instance() -> bool:
    """同一时间只允许一个实例，避免多个 WebView2 抢同一份用户数据目录。"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Local\\SocksGateway.SingleInstance")
        if not handle:
            return True
        if kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            return False
        _single_instance.handle = handle        # 保持引用，进程存活期间不释放
        return True
    except Exception:
        return True


def _run_webview(window, api, cfg, core, fallback_url: str):
    import webview
    last_err = None
    for attempt in (1, 2, 3):
        try:
            webview.start(debug=False, private_mode=False)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            log_event(f"webview.start 失败（第 {attempt} 次）：{e}")
            time.sleep(1.5)
    # 兜底：用默认浏览器打开同一套界面（API 走本地 HTTP，功能完全一样）
    log_event("内嵌窗口启动失败，改用默认浏览器")
    try:
        import webbrowser
        webbrowser.open(fallback_url)
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, f"内嵌窗口启动失败（{last_err}）。\n\n已用默认浏览器打开界面，功能一致。\n"
               "关闭程序请点界面右上角窗口关闭，或在任务管理器结束 SocksGateway。",
            APP_TITLE, 0x40)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def main():
    cfg = cfgmod.load()
    core = Core(cfg)
    api = Api(core)
    log_event(f"=== 启动 frozen={bool(getattr(sys, 'frozen', False))} python={sys.version.split()[0]}")

    if "--headless" in sys.argv:
        _headless(core, api)
        return

    if getattr(sys, "frozen", False) and not _single_instance():
        log_event("已有实例在运行，本次退出")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "Socks Gateway 已在运行中。\n请在任务栏找它的窗口。",
                                             APP_TITLE, 0x40)
        except Exception:
            pass
        core.shutdown()
        return

    try:
        import webview
    except ImportError:
        log_event("未安装 pywebview")
        print("未安装 pywebview，无法打开界面。请运行: pip install pywebview")
        print("或用 --headless 以无界面方式启动。")
        return

    # 规则列表每天自动更新：独立线程，不影响窗口和网关
    updater = RuleUpdater(api)
    updater.start()

    # 每次启动都把上游代理服务拉起来（脚本自己负责幂等）
    _launch_upstream_on_boot(api)

    # 开机自启场景：窗口还没出来就把网关拉起来，避免前几秒无法上网
    if cfg.get("auto_start_gateway"):
        try:
            api.start()
        except Exception as e:  # noqa: BLE001
            log_event(f"自动启动网关失败：{e}")

    # 界面走本地 HTTP（不依赖 pywebview 的 js_api 桥，稳定性更好）
    ui_server = webui.UiServer(api, os.path.join(RES_DIR, "ui"))
    try:
        url = ui_server.start()
    except OSError as e:
        log_event(f"界面服务端口启动失败：{e}")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, f"界面服务启动失败：{e}", APP_TITLE, 0x10)
        except Exception:
            pass
        return
    log_event(f"界面服务 {url.split('?')[0]}")

    w, h = _window_size()
    window = webview.create_window(
        APP_TITLE, url,
        width=w, height=h, min_size=(940, 600),
        background_color="#f5f6f8",
    )
    api.window = window

    def on_closing():
        try:
            # 只还原「本程序真正接管过」的系统代理，避免误关用户原有设置
            if cfg.get("system_proxy", {}).get("restore_on_exit", True):
                api._restore_proxy()
            # 退出后网关不再监听，变量留着会让 CLI 一直连不上；
            # 配置里的选择不动，下次启动仍会按它自动打开
            if cfg.get("system_proxy", {}).get("write_user_env"):
                try:
                    sysproxy.set_user_env("127.0.0.1", int(cfg["listen"]["port"]), False)
                except Exception as e:  # noqa: BLE001
                    log_event(f"退出时清除环境变量失败：{e}")
        finally:
            try:
                core.shutdown()
            finally:
                ui_server.stop()
        return True

    window.events.closing += on_closing
    try:
        _run_webview(window, api, cfg, core, url)
    finally:
        updater.stop()
        try:
            core.shutdown()
        except Exception:
            pass
        ui_server.stop()


if __name__ == "__main__":
    main()
