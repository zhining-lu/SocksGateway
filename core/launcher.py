"""启动上游代理服务：执行用户在界面上指定的 .bat / .cmd 脚本。

只负责「把脚本跑起来」，不托管进程、不做守护——脚本里怎么起 sing-box /
v2ray / SSH 隧道由用户自己决定。
"""
from __future__ import annotations

import os
import subprocess
import threading
from collections import deque

CREATE_NO_WINDOW = 0x08000000
_ALLOWED_EXT = (".bat", ".cmd")
_DEVNULL = subprocess.DEVNULL
MAX_LINES = 400          # 输出只保留最近这么多行
MAX_LINE_LEN = 2000      # 单行过长时截断，防止极端输出撑爆内存


class OutputLog:
    """脚本输出的环形缓冲。"""

    def __init__(self, max_lines: int = MAX_LINES):
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()

    def add(self, text: str) -> None:
        line = (text or "").rstrip()
        if len(line) > MAX_LINE_LEN:
            line = line[:MAX_LINE_LEN] + " …"
        with self._lock:
            self._lines.append(line)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._lines)


_LOG = OutputLog()


def read_output() -> list[str]:
    """最近一次启动脚本的输出（含命令行和退出码）。"""
    return _LOG.snapshot()


def clear_output() -> None:
    _LOG.clear()


def valid_script(path: str) -> bool:
    """只接受 .bat / .cmd，避免误把 exe 之类的路径配进来。"""
    return bool(path) and path.lower().endswith(_ALLOWED_EXT)


def _pump(proc) -> None:
    """把脚本的 stdout/stderr 读进缓冲。

    注意：bat 里如果用 `start xxx.exe` 拉起真正的服务，cmd 会很快退出，
    这里只能拿到 cmd 自己那几行；服务自身的日志得由脚本重定向到文件。
    """
    stream = getattr(proc, "stdout", None)
    if stream is None:
        return
    try:
        for raw in iter(stream.readline, b""):
            _LOG.add(raw.decode("mbcs", "replace"))
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass
        try:
            # 流关闭时进程可能还没完全退出，poll() 会拿到 None，稍等一下更准
            code = proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            code = None
        if code is not None:
            _LOG.add(f"[脚本已退出，返回码 {code}]")


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
        proc = subprocess.Popen(
            ["cmd", "/c", bat],
            cwd=os.path.dirname(bat) or None,   # 脚本里常写相对路径
            stdin=_DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as e:
        return False, f"启动失败：{e}"
    clear_output()
    _LOG.add(f"> {bat}")
    threading.Thread(target=_pump, args=(proc,), daemon=True).start()
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
