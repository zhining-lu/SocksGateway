"""Windows 系统代理（WinINET / WinHTTP / 环境变量）设置与还原

系统代理走 InternetSetOption(INTERNET_OPTION_PER_CONNECTION_OPTION) 写入，
由 Windows 统一同步三处状态：
  1. HKCU\\...\\Internet Settings 下的 ProxyEnable / ProxyServer / ProxyOverride
  2. Connections\\DefaultConnectionSettings 二进制块（Internet 选项对话框的显示来源）
  3. 向所有顶层窗口广播 WM_SETTINGCHANGE，让已运行的 WinINET 进程重读配置

直接写注册表只能改到第 1 处，会导致对话框显示旧值（用户点确定就把设置回滚）、
已在跑的浏览器继续走旧代理。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import winreg
from ctypes import wintypes

INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

INTERNET_OPTION_REFRESH = 37
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_PER_CONNECTION_OPTION = 75

# INTERNET_PER_CONN_OPTION.dwOption
PER_CONN_FLAGS = 1
PER_CONN_PROXY_SERVER = 2
PER_CONN_PROXY_BYPASS = 3
PER_CONN_AUTOCONFIG_URL = 4

# INTERNET_PER_CONN_FLAGS 的位掩码
PROXY_TYPE_DIRECT = 0x00000001
PROXY_TYPE_PROXY = 0x00000002
PROXY_TYPE_AUTO_PROXY_URL = 0x00000004
PROXY_TYPE_AUTO_DETECT = 0x00000008

VALUE_NAMES = ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL")

_UNSET = object()
_wininet = ctypes.WinDLL("wininet", use_last_error=True)


# ---------------- WinINET 结构体 ----------------
class _OptionValue(ctypes.Union):
    _fields_ = [
        ("dwValue", wintypes.DWORD),
        ("pszValue", ctypes.c_void_p),
        ("ftValue", wintypes.FILETIME),
    ]


class INTERNET_PER_CONN_OPTIONW(ctypes.Structure):
    _fields_ = [("dwOption", wintypes.DWORD), ("Value", _OptionValue)]


class INTERNET_PER_CONN_OPTION_LISTW(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("pszConnection", wintypes.LPWSTR),
        ("dwOptionCount", wintypes.DWORD),
        ("dwOptionError", wintypes.DWORD),
        ("pOptions", ctypes.POINTER(INTERNET_PER_CONN_OPTIONW)),
    ]


_wininet.InternetSetOptionW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
_wininet.InternetSetOptionW.restype = wintypes.BOOL

_wininet.InternetQueryOptionW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
    ctypes.POINTER(wintypes.DWORD)]
_wininet.InternetQueryOptionW.restype = wintypes.BOOL


# ---------------- 注册表读取（展示 / 兜底） ----------------
def _open(access=winreg.KEY_READ):
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS, 0, access)


def read_current() -> dict:
    """读取注册表里存的原始值，供界面展示。字段与旧版保持一致。"""
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


# ---------------- 官方 API：查询 / 写入 ----------------
def read_conn() -> dict:
    """查询当前默认连接（LAN）实际生效的代理配置。

    返回 {flags, proxy, bypass, pac}，可直接 JSON 序列化后作为备份。
    """
    opts = (INTERNET_PER_CONN_OPTIONW * 4)()
    for i, opt in enumerate((PER_CONN_FLAGS, PER_CONN_PROXY_SERVER,
                             PER_CONN_PROXY_BYPASS, PER_CONN_AUTOCONFIG_URL)):
        opts[i].dwOption = opt

    lst = INTERNET_PER_CONN_OPTION_LISTW()
    lst.dwSize = ctypes.sizeof(lst)
    lst.pszConnection = None          # NULL = 当前默认（LAN）连接
    lst.dwOptionCount = 4
    lst.dwOptionError = 0
    lst.pOptions = opts

    size = wintypes.DWORD(ctypes.sizeof(lst))
    ok = _wininet.InternetQueryOptionW(
        None, INTERNET_OPTION_PER_CONNECTION_OPTION,
        ctypes.byref(lst), ctypes.byref(size))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())

    # 字符串由 WinINet 分配，先抄下指针，读完统一 GlobalFree
    ptrs = [opts[i].Value.pszValue for i in (1, 2, 3)]
    try:
        out = {
            "flags": int(opts[0].Value.dwValue),
            "proxy": ctypes.wstring_at(ptrs[0]) if ptrs[0] else "",
            "bypass": ctypes.wstring_at(ptrs[1]) if ptrs[1] else "",
            "pac": ctypes.wstring_at(ptrs[2]) if ptrs[2] else "",
        }
    finally:
        for p in ptrs:
            if p:
                ctypes.windll.kernel32.GlobalFree(ctypes.c_void_p(p))
    return out


def _set_options(flags=_UNSET, proxy=_UNSET, bypass=_UNSET, pac=_UNSET) -> None:
    """一次性写入代理选项。传 _UNSET 表示该项不动，传 None 表示清空。"""
    items: list = []
    if flags is not _UNSET:
        items.append((PER_CONN_FLAGS, int(flags)))
    for opt, val in ((PER_CONN_PROXY_SERVER, proxy),
                     (PER_CONN_PROXY_BYPASS, bypass),
                     (PER_CONN_AUTOCONFIG_URL, pac)):
        if val is not _UNSET:
            items.append((opt, val))
    if not items:
        return

    arr = (INTERNET_PER_CONN_OPTIONW * len(items))()
    keep = []  # 保持字符串缓冲区存活到调用返回，否则指针会悬空
    for i, (opt, val) in enumerate(items):
        arr[i].dwOption = opt
        if isinstance(val, int):
            arr[i].Value.dwValue = val
        else:
            buf = ctypes.create_unicode_buffer(val or "")
            keep.append(buf)
            arr[i].Value.pszValue = ctypes.cast(buf, ctypes.c_void_p).value

    lst = INTERNET_PER_CONN_OPTION_LISTW()
    lst.dwSize = ctypes.sizeof(lst)
    lst.pszConnection = None
    lst.dwOptionCount = len(items)
    lst.dwOptionError = 0
    lst.pOptions = arr

    ok = _wininet.InternetSetOptionW(
        None, INTERNET_OPTION_PER_CONNECTION_OPTION,
        ctypes.byref(lst), ctypes.sizeof(lst))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    if lst.dwOptionError:
        raise OSError(f"代理选项写入失败，第 {lst.dwOptionError} 项被拒绝")
    _ = keep  # 显式引用，避免被优化掉


def refresh() -> None:
    """通知系统与各应用重读代理设置。官方 API 已广播一次，这里补发确保兼容。"""
    try:
        _wininet.InternetSetOptionW(None, INTERNET_OPTION_SETTINGS_CHANGED, None, 0)
        _wininet.InternetSetOptionW(None, INTERNET_OPTION_REFRESH, None, 0)
    except Exception:
        pass
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_wchar_p,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        user32.SendMessageTimeoutW.restype = ctypes.c_size_t
        res = ctypes.c_size_t()
        user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, INTERNET_SETTINGS, 0x0002, 800, ctypes.byref(res))
    except Exception:
        pass


# ---------------- 应用 / 还原 ----------------
def _normalize_item(item: str) -> str:
    """WinINET 只接受方括号形式的 IPv6 字面量。

    裸 `::1` 通过注册表能写进去，但 InternetSetOption 会直接返回
    ERROR_INVALID_PARAMETER(87)，所以统一转成 `[::1]`。
    `host:port` 写法保持原样。
    """
    if ":" not in item or item.startswith("["):
        return item
    head, _sep, tail = item.partition(":")
    if head and tail.isdigit():      # 形如 127.0.0.1:8080
        return item
    return f"[{item}]"


def build_override(bypass_lines: list[str], existing: str = "") -> str:
    """把不代理列表转成 WinINET 例外串，并保留用户原有的例外项。"""
    parts: list[str] = []
    for raw in bypass_lines or []:
        p = (raw or "").strip()
        if not p or p.startswith("#"):
            continue
        parts.append(_normalize_item(p))
    for raw in (existing or "").split(";"):
        p = _normalize_item(raw.strip())
        if p and p not in parts:
            parts.append(p)
    return ";".join(dict.fromkeys(parts))


def proxy_string(host: str, port: int) -> str:
    """本机网关对应的 WinINET 代理串。"""
    return f"http={host}:{port};https={host}:{port}"


def points_to(proxy: str, host: str, port: int) -> bool:
    """判断代理串是否已经指向 host:port（忽略协议前缀差异）。

    用于识别「上次接管没还原干净」的场景。
    """
    target = f"{host}:{port}"
    for part in (proxy or "").split(";"):
        part = part.strip()
        if not part:
            continue
        addr = part.split("=", 1)[1] if "=" in part else part
        if addr.strip() == target:
            return True
    return False


def apply_http_proxy(host: str, port: int, bypass_lines: list[str] | None = None) -> dict:
    """把系统代理指向本地网关。返回被覆盖前的配置，供还原使用。

    返回值 {flags, proxy, bypass, pac} 可直接存入 JSON 备份。
    """
    try:
        backup = read_conn()
    except OSError:
        # WinINET 查询失败（极少见）时退回注册表值，保证仍能接管
        reg = read_current()
        flags = PROXY_TYPE_PROXY if int(reg.get("ProxyEnable") or 0) else PROXY_TYPE_DIRECT
        if reg.get("AutoConfigURL"):
            flags |= PROXY_TYPE_AUTO_PROXY_URL
        backup = {"flags": flags,
                  "proxy": reg.get("ProxyServer") or "",
                  "bypass": reg.get("ProxyOverride") or "",
                  "pac": reg.get("AutoConfigURL") or ""}

    # 保留“自动检测设置”位；手动代理优先于 PAC，所以 PAC 必须清掉
    new_flags = PROXY_TYPE_PROXY | (backup.get("flags", 0) & PROXY_TYPE_AUTO_DETECT)

    kwargs = {"flags": new_flags, "proxy": proxy_string(host, port), "pac": ""}
    override = None
    if bypass_lines is not None:
        override = build_override(bypass_lines, backup.get("bypass") or "")
        kwargs["bypass"] = override
    try:
        _set_options(**kwargs)
    except OSError:
        if not override or ":" not in override:
            raise
        # 个别例外项写法 WinINET 不认（含冒号），剔除后重试，保证代理本身接管成功
        kwargs["bypass"] = ";".join(x for x in override.split(";") if ":" not in x)
        _set_options(**kwargs)
        backup["_warn"] = "部分例外项含冒号被 WinINET 拒绝，已剔除"
    refresh()
    return backup


def normalize_backup(restore: dict) -> dict:
    """兼容旧版注册表格式的备份，统一成 {flags, proxy, bypass, pac}。"""
    if "flags" in restore:
        pac = restore.get("pac") or ""
        flags = int(restore.get("flags") or 0)
        if not pac:
            flags &= ~PROXY_TYPE_AUTO_PROXY_URL
        return {"flags": flags,
                "proxy": restore.get("proxy") or "",
                "bypass": restore.get("bypass") or "",
                "pac": pac}

    flags = 0
    if int(restore.get("ProxyEnable") or 0):
        flags |= PROXY_TYPE_PROXY
    pac = restore.get("AutoConfigURL") or ""
    if pac:
        flags |= PROXY_TYPE_AUTO_PROXY_URL
    if not flags:
        flags = PROXY_TYPE_DIRECT
    return {"flags": flags,
            "proxy": restore.get("ProxyServer") or "",
            "bypass": restore.get("ProxyOverride") or "",
            "pac": pac}


def disable(restore: dict | None = None) -> None:
    """关闭系统代理；有备份则按备份完整还原（代理串 / 例外 / PAC 一起写回）。"""
    if not restore:
        _set_options(flags=PROXY_TYPE_DIRECT, pac="")
        refresh()
        return
    st = normalize_backup(restore)
    _set_options(flags=st["flags"], proxy=st["proxy"],
                 bypass=st["bypass"], pac=st["pac"])
    refresh()


def is_enabled() -> bool:
    try:
        conn = read_conn()
    except OSError:
        cur = read_current()
        if cur.get("AutoConfigURL"):
            return True
        return bool(int(cur.get("ProxyEnable") or 0)) and bool(cur.get("ProxyServer"))
    flags = conn.get("flags", 0)
    if flags & PROXY_TYPE_AUTO_PROXY_URL:
        return True
    return bool(flags & PROXY_TYPE_PROXY) and bool(conn.get("proxy"))


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
