"""接管前备份的保护逻辑：不能把网关地址误当成用户的原配置。

对应场景：程序崩溃 / 被强杀后，系统代理仍指向网关，重启自动接管时
read_conn() 读到的是网关地址本身，直接存成备份会导致「关闭代理关不掉」。
"""
import json

import pytest

import app as appmod
from app import Api

TARGET = "http=127.0.0.1:18081;https=127.0.0.1:18081"


class FakeGateway:
    running = True


class FakeCore:
    def __init__(self, cfg):
        self.cfg = cfg
        self.gateway = FakeGateway()


class FakeSysproxy:
    """有状态的假实现，避免测试真的改系统代理。"""

    def __init__(self, initial):
        self.state = dict(initial)

    def apply_http_proxy(self, host, port, bypass):
        prev = dict(self.state)
        self.state = {
            "flags": 2,
            "proxy": f"http={host}:{port};https={host}:{port}",
            "bypass": "localhost",
            "pac": "",
        }
        return prev

    def disable(self, backup):
        self.state = dict(appmod.sysproxy.normalize_backup(backup))


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(Api, "BACKUP_FILE", str(tmp_path / "proxy-backup.json"))
    cfg = {"listen": {"port": 18081}, "system_proxy": {}, "backup": None}
    return Api(FakeCore(cfg))


def write_backup(data):
    with open(Api.BACKUP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def test_current_value_is_used_when_it_is_not_the_gateway(api):
    cur = {"flags": 2, "proxy": "socks=127.0.0.1:18080", "bypass": "localhost", "pac": ""}
    assert api._guard_backup(cur) == cur


def test_existing_backup_is_reused_when_proxy_already_points_to_gateway(api):
    write_backup({"flags": 1, "proxy": "socks=127.0.0.1:18080",
                  "bypass": "localhost", "pac": ""})
    cur = {"flags": 2, "proxy": TARGET, "bypass": "localhost", "pac": ""}
    out = api._guard_backup(cur)
    assert out["proxy"] == "socks=127.0.0.1:18080"
    assert out["flags"] == 1


def test_legacy_backup_format_is_reused(api):
    write_backup({"ProxyEnable": 1, "ProxyServer": "socks=127.0.0.1:18080",
                  "ProxyOverride": "localhost", "AutoConfigURL": None})
    cur = {"flags": 2, "proxy": TARGET, "bypass": "localhost", "pac": ""}
    out = api._guard_backup(cur)
    assert out["proxy"] == "socks=127.0.0.1:18080"
    assert out["flags"] == appmod.sysproxy.PROXY_TYPE_PROXY


def test_falls_back_to_direct_when_no_backup_exists(api):
    cur = {"flags": 2, "proxy": TARGET, "bypass": "localhost", "pac": ""}
    out = api._guard_backup(cur)
    assert out["flags"] == appmod.sysproxy.PROXY_TYPE_DIRECT
    assert out["proxy"] == ""
    assert out["bypass"] == "localhost"


def test_falls_back_to_direct_when_backup_itself_is_the_gateway(api):
    write_backup({"flags": 2, "proxy": TARGET, "bypass": "", "pac": ""})
    cur = {"flags": 2, "proxy": TARGET, "bypass": "localhost", "pac": ""}
    out = api._guard_backup(cur)
    assert out["flags"] == appmod.sysproxy.PROXY_TYPE_DIRECT
    assert out["proxy"] == ""


def test_points_to_matches_any_protocol_prefix():
    sp = appmod.sysproxy
    assert sp.points_to(TARGET, "127.0.0.1", 18081)
    assert sp.points_to("127.0.0.1:18081", "127.0.0.1", 18081)
    assert sp.points_to("http=127.0.0.1:18081", "127.0.0.1", 18081)
    assert not sp.points_to("socks=127.0.0.1:18080", "127.0.0.1", 18081)
    assert not sp.points_to("", "127.0.0.1", 18081)
    assert not sp.points_to("http=127.0.0.1:180811", "127.0.0.1", 18081)


def test_crash_then_relaunch_restores_original_not_gateway(api, monkeypatch):
    """崩溃后重启自动接管：备份不被网关地址覆盖，关闭时能回到用户原配置。"""
    monkeypatch.setattr(appmod, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(appmod.cfgmod, "save", lambda cfg: None)
    fake = FakeSysproxy({"flags": 2, "proxy": "socks=127.0.0.1:18080",
                         "bypass": "localhost", "pac": ""})
    monkeypatch.setattr(appmod.sysproxy, "apply_http_proxy", fake.apply_http_proxy)
    monkeypatch.setattr(appmod.sysproxy, "disable", fake.disable)

    api.apply_system_proxy(True)
    assert api._load_backup()["proxy"] == "socks=127.0.0.1:18080"

    # 模拟崩溃：没有还原就再次接管（重启后的自动接管）
    api.apply_system_proxy(True)
    assert api._load_backup()["proxy"] == "socks=127.0.0.1:18080"

    api.apply_system_proxy(False)
    assert fake.state["proxy"] == "socks=127.0.0.1:18080"
    assert fake.state["flags"] == 2


def test_crash_without_backup_restores_to_direct(api, monkeypatch):
    """没有任何备份可用时，关闭代理应回到直连，而不是继续指向网关。"""
    monkeypatch.setattr(appmod, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(appmod.cfgmod, "save", lambda cfg: None)
    fake = FakeSysproxy({"flags": 2, "proxy": TARGET, "bypass": "", "pac": ""})
    monkeypatch.setattr(appmod.sysproxy, "apply_http_proxy", fake.apply_http_proxy)
    monkeypatch.setattr(appmod.sysproxy, "disable", fake.disable)

    api.apply_system_proxy(True)
    assert api._load_backup()["flags"] == appmod.sysproxy.PROXY_TYPE_DIRECT

    api.apply_system_proxy(False)
    assert fake.state["proxy"] == ""
    assert fake.state["flags"] == appmod.sysproxy.PROXY_TYPE_DIRECT
