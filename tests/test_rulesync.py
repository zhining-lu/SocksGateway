"""规则列表自动更新：时间判定 + 「这次要不要真的下载」。"""
from datetime import datetime, timedelta

import pytest

import app as appmod
from app import Api
from core import rulesync

NOW = datetime(2026, 11, 19, 13, 0, 0)


class FakeCore:
    def __init__(self, cfg):
        self.cfg = cfg
        self.gateway = None


def hours_ago(h):
    return rulesync.format_now(NOW - timedelta(hours=h))


# ---------------- 时间判定 ----------------
@pytest.mark.parametrize("last,expected", [
    (None, True),                       # 从没更新过
    ("", True),
    ("不是时间", True),                  # 脏数据按到期处理，便于自愈
    (hours_ago(25), True),              # 超过一天
    (hours_ago(24), True),              # 刚好一天
    (hours_ago(23), False),             # 差一小时
    (hours_ago(1), False),
])
def test_is_due(last, expected):
    assert rulesync.is_due(last, 24, now=NOW) is expected


def test_is_due_respects_custom_interval():
    last = hours_ago(5)
    assert rulesync.is_due(last, 4, now=NOW) is True
    assert rulesync.is_due(last, 6, now=NOW) is False


def test_is_due_ignores_future_timestamp():
    """系统时钟回拨导致 last_update 在未来时，不应该反复触发下载。"""
    future = rulesync.format_now(NOW + timedelta(hours=2))
    assert rulesync.is_due(future, 24, now=NOW) is False


@pytest.mark.parametrize("value,expected", [
    (24, 24.0),
    ("12", 12.0),
    (0, 24.0),        # 下限保护，避免每轮检查都下载
    (-5, 24.0),
    ("abc", 24.0),
    (None, 24.0),
])
def test_normalize_interval(value, expected):
    assert rulesync.normalize_interval(value) == expected


# ---------------- 是否真的下载 ----------------
@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(appmod, "log_event", lambda *a, **k: None)
    cfg = {"rule_list": {"url": "https://example.com/list.txt",
                         "update_interval_hours": 24, "last_update": None,
                         "rules": [], "count": 0}}
    return Api(FakeCore(cfg))


@pytest.fixture
def downloaded(monkeypatch):
    calls = []

    def fake_update(self):
        calls.append(1)
        return {"ok": True, "message": "已更新 10 条规则"}

    monkeypatch.setattr(Api, "update_rule_list", fake_update)
    return calls


def test_downloads_when_never_updated(api, downloaded):
    api.auto_update_rule_list()
    assert len(downloaded) == 1


def test_skips_when_url_empty(api, downloaded):
    api.cfg["rule_list"]["url"] = "   "
    assert api.auto_update_rule_list() is None
    assert downloaded == []


def test_skips_when_not_due(api, downloaded):
    api.cfg["rule_list"]["last_update"] = rulesync.format_now()
    assert api.auto_update_rule_list() is None
    assert downloaded == []


def test_downloads_when_stale(api, downloaded):
    api.cfg["rule_list"]["last_update"] = rulesync.format_now(
        datetime.now() - timedelta(hours=25))
    api.auto_update_rule_list()
    assert len(downloaded) == 1


def test_rule_updater_stops_promptly(monkeypatch):
    """定时线程要能被 stop() 立刻唤醒退出，不能等满一个检查周期。"""
    import threading
    import time

    monkeypatch.setattr(appmod, "log_event", lambda *a, **k: None)
    hits = []

    class DummyApi:
        def auto_update_rule_list(self):
            hits.append(1)

    up = appmod.RuleUpdater(DummyApi(), check_seconds=3600)
    up.start()
    time.sleep(0.2)
    assert hits, "启动后应立刻检查一次"
    started = time.time()
    up.stop()
    up.join(timeout=2)
    assert not up.is_alive()
    assert time.time() - started < 1.5
    assert threading.active_count() >= 1
