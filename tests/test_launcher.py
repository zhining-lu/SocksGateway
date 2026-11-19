"""代理服务启动脚本：路径校验、启动分支、输出捕获。"""
import socket
import time

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


# ---------------- 启动入口 ----------------
def test_start_upstream_always_runs_script(monkeypatch):
    """不再探测端口：即使上游已在跑，点按钮也照样执行脚本。"""
    monkeypatch.setattr(appmod, "log_event", lambda *a, **k: None)
    called = []
    monkeypatch.setattr(launchermod, "run_script",
                        lambda p: called.append(p) or (True, "启动脚本已执行"))
    res = make_api(free_port(), "C:\\tools\\up.bat").start_upstream()
    assert res["ok"] and called == ["C:\\tools\\up.bat"]


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


# ---------------- 输出捕获 ----------------
def test_output_log_keeps_only_recent_lines():
    log = launchermod.OutputLog(max_lines=3)
    for i in range(5):
        log.add(f"line{i}")
    assert log.snapshot() == ["line2", "line3", "line4"]


def test_output_log_truncates_overlong_line():
    log = launchermod.OutputLog()
    log.add("x" * 5000)
    assert len(log.snapshot()[0]) <= launchermod.MAX_LINE_LEN + 4


def test_run_script_captures_stdout(tmp_path):
    bat = tmp_path / "out.bat"
    bat.write_text("@echo off\r\necho hello-from-bat\r\n")
    launchermod.clear_output()
    ok, _msg = launchermod.run_script(str(bat))
    assert ok
    deadline = time.time() + 5
    while time.time() < deadline:
        if any("hello-from-bat" in line for line in launchermod.read_output()):
            break
        time.sleep(0.05)
    lines = launchermod.read_output()
    assert lines[0].startswith("> ")                       # 第一条记录执行的脚本
    assert any("hello-from-bat" in line for line in lines), lines


def test_run_script_clears_previous_output(tmp_path):
    bat = tmp_path / "out.bat"
    bat.write_text("@echo off\r\necho second-run\r\n")
    launchermod.clear_output()
    launchermod._LOG.add("old-stale-line")
    launchermod.run_script(str(bat))
    lines = launchermod.read_output()
    assert not any("old-stale-line" in line for line in lines)


def test_api_script_output_returns_lines(monkeypatch):
    monkeypatch.setattr(launchermod, "read_output", lambda: ["a", "b"])
    res = make_api(free_port()).script_output()
    assert res["ok"] is True and res["lines"] == ["a", "b"]
