"""「写入用户环境变量」开关要记住用户的选择。

覆盖三条路径：手动拨动开关 → 存进配置；网关启动 → 自动写入；
网关停止 → 自动清除（变量不能指向已关闭的端口）。
"""
import pytest

import app as appmod
from app import Api


class FakeGateway:
    running = True
    listen = "127.0.0.1:18081"

    def snapshot(self):
        return {"running": True, "listen": self.listen, "uptime": 1,
                "stats": {}, "log": []}


class FakeCore:
    def __init__(self, cfg):
        self.cfg = cfg
        self.gateway = FakeGateway()

    def start(self):
        pass

    def stop(self):
        pass


@pytest.fixture
def env_api(tmp_path, monkeypatch):
    calls = []
    saved = []

    monkeypatch.setattr(appmod, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(Api, "BACKUP_FILE", str(tmp_path / "proxy-backup.json"))
    monkeypatch.setattr(appmod.cfgmod, "save", lambda cfg: saved.append(True))
    monkeypatch.setattr(Api, "_check_port", lambda self: None)

    def fake_set_user_env(host, port, enable):
        calls.append((host, port, enable))
        return True, ("已写入" if enable else "已清除")

    monkeypatch.setattr(appmod.sysproxy, "set_user_env", fake_set_user_env)

    cfg = {
        "listen": {"host": "127.0.0.1", "port": 18081},
        "system_proxy": {"apply_on_start": False, "write_user_env": False},
        "backup": None,
    }
    api = Api(FakeCore(cfg))
    api.window = None
    return api, cfg, calls, saved


def test_toggle_on_is_persisted(env_api):
    api, cfg, calls, saved = env_api
    r = api.set_user_env(True)
    assert r["ok"] is True
    assert calls == [("127.0.0.1", 18081, True)]
    assert cfg["system_proxy"]["write_user_env"] is True
    assert saved, "选择没有落盘"


def test_toggle_off_is_persisted(env_api):
    api, cfg, calls, saved = env_api
    cfg["system_proxy"]["write_user_env"] = True
    r = api.set_user_env(False)
    assert r["ok"] is True
    assert calls == [("127.0.0.1", 18081, False)]
    assert cfg["system_proxy"]["write_user_env"] is False


def test_failed_write_does_not_change_saved_choice(env_api, monkeypatch):
    api, cfg, calls, saved = env_api
    monkeypatch.setattr(appmod.sysproxy, "set_user_env",
                        lambda host, port, enable: (False, "拒绝访问"))
    r = api.set_user_env(True)
    assert r["ok"] is False
    assert cfg["system_proxy"]["write_user_env"] is False
    assert not saved, "失败时不该写配置"


def test_start_restores_remembered_choice(env_api):
    api, cfg, calls, saved = env_api
    cfg["system_proxy"]["write_user_env"] = True
    r = api.start()
    assert r["ok"] is True
    assert ("127.0.0.1", 18081, True) in calls
    assert "命令行代理变量已写入" in r["message"]


def test_start_skips_when_choice_is_off(env_api):
    api, cfg, calls, saved = env_api
    r = api.start()
    assert r["ok"] is True
    assert calls == [], "关着的开关不该写入变量"
    assert "命令行代理变量" not in r["message"]


def test_stop_clears_when_choice_is_on(env_api):
    api, cfg, calls, saved = env_api
    cfg["system_proxy"]["write_user_env"] = True
    r = api.stop()
    assert r["ok"] is True
    assert ("127.0.0.1", 18081, False) in calls
    assert "命令行代理变量已清除" in r["message"]
    # 清除只是运行时行为，不能把用户的选择改掉
    assert cfg["system_proxy"]["write_user_env"] is True
