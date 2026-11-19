"""启动上游代理服务：执行用户在界面上指定的 .bat / .cmd 脚本。

只负责「把脚本跑起来」，不托管进程、不做守护——脚本里怎么起 sing-box /
v2ray / SSH 隧道由用户自己决定。
"""
from __future__ import annotations

import os
import socket
import subprocess

CREATE_NO_WINDOW = 0x08000000
_ALLOWED_EXT = (".bat", ".cmd")
_DETACHED = (subprocess.DEVNULL, subprocess.DEVNULL, subprocess.DEVNULL)


def valid_script(path: str) -> bool:
    """只接受 .bat / .cmd，避免误把 exe 之类的路径配进来。"""
    return bool(path) and path.lower().endswith(_ALLOWED_EXT)


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    """上游端口是否已经可连，用来避免重复启动。"""
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def run_script(path: str) -> tuple[bool, str]:
    """执行启动脚本。返回 (是否已执行, 提示语)。"""
    bat = (path or "").strip()
    if not bat:
        return False, "还没有选择启动脚本"
    if not valid_script(bat):
        return False, "启动脚本必须是 .bat 或 .cmd 文件"
    if not os.path.isfile(bat):
        return False, f"找不到启动脚本：{bat}"
    try:
        subprocess.Popen(
            ["cmd", "/c", bat],
            cwd=os.path.dirname(bat) or None,   # 脚本里常写相对路径
            stdin=_DETACHED[0], stdout=_DETACHED[1], stderr=_DETACHED[2],
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as e:
        return False, f"启动失败：{e}"
    return True, "启动脚本已执行"


def pick_script() -> str:
    """弹系统「打开文件」对话框选 .bat，取消返回空串。

    界面是本地 HTTP 页面，拿不到 <input type=file> 的真实路径，
    所以走 comdlg32 的原生对话框。调用会阻塞当前请求线程，直到用户操作。
    """
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
        ]

    buf = ctypes.create_unicode_buffer(1024)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = ctypes.windll.user32.GetForegroundWindow()
    ofn.lpstrFilter = "启动脚本 (*.bat;*.cmd)\0*.bat;*.cmd\0所有文件 (*.*)\0*.*\0\0"
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = len(buf)
    ofn.lpstrTitle = "选择启动代理服务的脚本"
    # FILEMUSTEXIST | PATHMUSTEXIST | NOCHANGEDIR
    ofn.Flags = 0x00001000 | 0x00000800 | 0x00000008
    ofn.lpstrDefExt = "bat"
    try:
        ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    except (OSError, AttributeError):
        return ""
    return buf.value if ok else ""
