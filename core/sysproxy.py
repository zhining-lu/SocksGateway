"""Windows 系统代理（WinINET / WinHTTP / 环境变量）设置与还原"""
from __future__ import annotations

import ctypes
import os
import subprocess
import winreg

INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
INTERNET_OPTION_REFRESH = 37
INTERNET_OPTION_SETTINGS_CHANGED = 39

VALUE_NAMES = ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL")


# ---------------- 注册表读写 ----------------
def _open(access=winreg.KEY_READ):
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS, 0, access)


def read_current() -> dict:
    out: dict = {}
    try:
        with _open() as k:
            for name in VALUE_NAMES:
                try:
                    val, _t = winreg.QueryValueEx(k, name)
                    out[name] = val
                except FileNotFoundError:
                    out[name] = None
    except OSError:
        pass
    out.setdefault("ProxyEnable", 0)
    out.setdefault("ProxyServer", "")
    out.setdefault("ProxyOverride", "")
    out.setdefault("AutoConfigURL", None)
    return out


def _write(name: str, value, kind=winreg.REG_SZ) -> None:
    with _open(winreg.KEY_READ | winreg.KEY_WRITE) as k:
        if value is None:
            try:
                winreg.DeleteValue(k, name)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(k, name, 0, kind, value)


def refresh() -> None:
    """通知系统与各应用重读代理设置。"""
    try:
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)
    except Exception:
        pass
    try:
        res = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, INTERNET_SETTINGS, 0x0002, 800, ctypes.byref(res))
    except Exception:
        pass


# ---------------- 应用 / 还原 ----------------
def build_override(bypass_lines: list[str], existing: str = "") -> str:
    """把不代理列表转成 WinINET 例外串，并保留用户原有的例外项。"""
    parts: list[str] = []
    for raw in bypass_lines or []:
        p = (raw or "").strip()
        if not p or p.startswith("#"):
            continue
        parts.append(p)
    for raw in (existing or "").split(";"):
        p = raw.strip()
        if p and p not in parts:
            parts.append(p)
    return ";".join(dict.fromkeys(parts))


def apply_http_proxy(host: str, port: int, bypass_lines: list[str] | None = None) -> dict:
    """把系统代理指向本地网关。返回被覆盖前的原始设置，供还原使用。"""
    backup = read_current()
    addr = f"{host}:{port}"
    _write("ProxyEnable", 1, winreg.REG_DWORD)
    _write("ProxyServer", f"http={addr};https={addr}")
    if bypass_lines is not None:
        _write("ProxyOverride",
               build_override(bypass_lines, backup.get("ProxyOverride") or ""))
    _write("AutoConfigURL", None)
    refresh()
    return backup


def disable(restore: dict | None = None) -> None:
    """关闭系统代理；有备份则按备份还原。

    注意：备份里缺失的键不去写空值，避免把用户原有的 ProxyServer / ProxyOverride 抹掉。
    """
    if restore:
        _write("ProxyEnable", int(restore.get("ProxyEnable") or 0), winreg.REG_DWORD)
        if restore.get("ProxyServer") is not None:
            _write("ProxyServer", restore["ProxyServer"])
        if restore.get("ProxyOverride") is not None:
            _write("ProxyOverride", restore["ProxyOverride"])
        _write("AutoConfigURL", restore.get("AutoConfigURL"))
    else:
        _write("ProxyEnable", 0, winreg.REG_DWORD)
    refresh()


def is_enabled() -> bool:
    cur = read_current()
    if cur.get("AutoConfigURL"):
        return True
    return bool(int(cur.get("ProxyEnable") or 0)) and bool(cur.get("ProxyServer"))


# ---------------- 附加能力 ----------------
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def set_winhttp(host: str, port: int) -> tuple[bool, str]:
    """设置 WinHTTP 代理（服务/系统组件用），需要管理员权限。"""
    if not is_admin():
        return False, "需要以管理员身份运行才能修改 WinHTTP 代理"
    try:
        subprocess.run(["netsh", "winhttp", "set", "proxy", f"{host}:{port}"],
                       check=True, capture_output=True, creationflags=0x08000000)
        return True, f"WinHTTP 代理已设为 {host}:{port}"
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or b"").decode("gbk", "ignore").strip() or "netsh 执行失败"


def clear_winhttp() -> tuple[bool, str]:
    if not is_admin():
        return False, "需要以管理员身份运行"
    try:
        subprocess.run(["netsh", "winhttp", "reset", "proxy"],
                       check=True, capture_output=True, creationflags=0x08000000)
        return True, "WinHTTP 代理已重置"
    except subprocess.CalledProcessError:
        return False, "netsh 执行失败"


def set_user_env(host: str, port: int, enable: bool) -> None:
    """为命令行工具（git / npm / pip 等）写入用户环境变量。"""
    keys = {
        "HTTP_PROXY": f"http://{host}:{port}",
        "HTTPS_PROXY": f"http://{host}:{port}",
        "ALL_PROXY": f"socks5h://{host}:{port}",
        "NO_PROXY": "localhost,127.0.0.1,::1",
    }
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as k:
            for name, value in keys.items():
                if enable:
                    winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
                else:
                    try:
                        winreg.DeleteValue(k, name)
                    except FileNotFoundError:
                        pass
        refresh()
    except OSError:
        pass


def env_script(host: str, port: int) -> str:
    return (
        "@echo off\r\n"
        "rem 在当前 CMD 窗口启用代理（仅本次会话有效）\r\n"
        f'set HTTP_PROXY=http://{host}:{port}\r\n'
        f'set HTTPS_PROXY=http://{host}:{port}\r\n'
        f'set ALL_PROXY=socks5h://{host}:{port}\r\n'
        "set NO_PROXY=localhost,127.0.0.1,::1\r\n"
        "echo 代理已启用。\r\n"
    )


def startup_dir() -> str:
    return os.path.join(os.environ.get("APPDATA", ""),
                        r"Microsoft\Windows\Start Menu\Programs\Startup")
