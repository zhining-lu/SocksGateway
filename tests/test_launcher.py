"""代理服务启动脚本：路径校验、端口探测、启动分支。"""
import socket

import pytest

import app as appmod
from app import Api
from core import launcher as launchermod


class FakeCore:
    def __init__(self, cfg):
        self.cfg = cfg
        self.gateway = None


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def listener():
    """占一个真实监听端口，模拟「代理服务已经在跑」。"""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        yield s.getsockname()[1]
    finally:
        s.close()


def make_api(port, bat_path=""):
    cfg = {"upstream": {"host": "127.0.0.1", "port": port},
           "upstream_launcher": {"bat_path": bat_path}}
    return Api(FakeCore(cfg))


# ---------------- 脚本校验 ----------------
@pytest.mark.parametrize("path,expected", [
    ("C:\\tools\\up.bat", True),
    ("C:\\tools\\up.CMD", True),
    ("C:\\tools\\up.exe", False),
    ("C:\\tools\\up.bat.txt", False),
    ("", False),
])
def test_valid_script(path, expected):
    assert launchermod.valid_script(path) is expected


def test_run_script_rejects_empty_path():
    ok, msg = launchermod.run_script("")
    assert not ok and "没有选择" in msg


def test_run_script_rejects_non_bat(tmp_path):
    exe = tmp_path / "up.exe"
    exe.write_text("")
    ok, msg = launchermod.run_script(str(exe))
    assert not ok and ".bat" in msg


def test_run_script_rejects_missing_file(tmp_path):
    ok, msg = launchermod.run_script(str(tmp_path / "nope.bat"))
    assert not ok and "找不到" in msg


def test_run_script_invokes_cmd_in_script_dir(tmp_path, monkeypatch):
    bat = tmp_path / "up.bat"
    bat.write_text("@echo off\n")
    calls = []

    class FakeProc:
        pid = 1

    monkeypatch.setattr(launchermod.subprocess, "Popen",
                        lambda argv, **kw: calls.append((argv, kw)) or FakeProc())

    ok, msg = launchermod.run_script(str(bat))
    assert ok and "已执行" in msg
    argv, kw = calls[0]
    assert argv[0] == "cmd" and argv[1] == "/c" and argv[2] == str(bat)
    assert kw["cwd"] == str(tmp_path)                 # 脚本里的相对路径要能生效
    assert kw["creationflags"] == launchermod.CREATE_NO_WINDOW


def test_run_script_reports_spawn_failure(tmp_path, monkeypatch):
    bat = tmp_path / "up.bat"
    bat.write_text("@echo off\n")

    def boom(*a, **k):
        raise OSError("拒绝访问")

    monkeypatch.setattr(launchermod.subprocess, "Popen", boom)
    ok, msg = launchermod.run_script(str(bat))
    assert not ok and "拒绝访问" in msg


# ---------------- 端口探测 ----------------
def test_port_open_true(listener):
    assert launchermod.port_open("127.0.0.1", listener) is True


def test_port_open_false():
    assert launchermod.port_open("127.0.0.1", free_port()) is False


def test_port_open_handles_zero_port():
    assert launchermod.port_open("127.0.0.1", 0) is False


# ---------------- 启动入口 ----------------
def test_start_upstream_skips_when_port_already_open(listener, monkeypatch):
    called = []
    monkeypatch.setattr(launchermod, "run_script",
                        lambda p: called.append(p) or (True, "x"))
    res = make_api(listener, "C:\\tools\\up.bat").start_upstream()
    assert res["ok"] and "已在运行" in res["message"]
    assert called == []          # 不能重复起第二个实例


def test_start_upstream_runs_selected_script(monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "log_event", lambda *a, **k: None)
    bat = tmp_path / "up.bat"
    seen = []
    monkeypatch.setattr(launchermod, "run_script",
                        lambda p: seen.append(p) or (True, "启动脚本已执行"))
    res = make_api(free_port(), str(bat)).start_upstream()
    assert res["ok"] and seen == [str(bat)]


def test_start_upstream_without_script(monkeypatch):
    monkeypatch.setattr(appmod, "log_event", lambda *a, **k: None)
    res = make_api(free_port()).start_upstream()
    assert not res["ok"] and "没有选择" in res["message"]


def test_pick_bat_file_cancel_is_not_an_error(monkeypatch):
    monkeypatch.setattr(launchermod, "pick_script", lambda: "")
    res = make_api(free_port()).pick_bat_file()
    assert res["ok"] is True and res["path"] == ""


# ---------------- 启动时自动拉起 ----------------
def test_boot_skips_when_no_script_configured(monkeypatch):
    called = []
    monkeypatch.setattr(Api, "start_upstream", lambda self: called.append(1))
    appmod._launch_upstream_on_boot(make_api(free_port()))
    assert called == []


def test_boot_skips_when_script_is_blank(monkeypatch):
    called = []
    monkeypatch.setattr(Api, "start_upstream", lambda self: called.append(1))
    appmod._launch_upstream_on_boot(make_api(free_port(), "   "))
    assert called == []


def test_boot_launches_once_when_script_configured(monkeypatch):
    called = []
    monkeypatch.setattr(appmod, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(
        Api, "start_upstream",
        lambda self: called.append(1) or {"ok": True, "message": "启动脚本已执行"})
    appmod._launch_upstream_on_boot(make_api(free_port(), "C:\\tools\\up.bat"))
    assert called == [1]


def test_boot_tolerates_start_failure(monkeypatch):
    """脚本配错（比如文件被删）时不能让启动流程崩掉。"""
    monkeypatch.setattr(appmod, "log_event", lambda *a, **k: None)
    res = appmod._launch_upstream_on_boot(make_api(free_port(), "C:\\nope\\up.bat"))
    assert res is None
