"""规则列表自动更新：判断是否到期。

只做时间判定，不碰网络和 UI，方便单测。
"""
from __future__ import annotations

from datetime import datetime, timedelta

DEFAULT_INTERVAL_HOURS = 24
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def parse_last_update(text: str | None) -> datetime | None:
    """把配置里的 last_update 字符串解析成时间，失败返回 None。"""
    try:
        return datetime.strptime((text or "").strip(), TIME_FORMAT)
    except (ValueError, TypeError):
        return None


def normalize_interval(value, default: float = DEFAULT_INTERVAL_HOURS) -> float:
    """校验更新间隔，非法或过小的值回退到默认值。

    下限 1 小时：避免配置写成 0 之后每轮检查都触发下载。
    """
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return float(default)
    if hours < 1:
        return float(default)
    return hours


def is_due(last_update: str | None, interval_hours=DEFAULT_INTERVAL_HOURS,
           now: datetime | None = None) -> bool:
    """距上次更新是否已经超过间隔。从未更新过一律视为到期。"""
    last = parse_last_update(last_update)
    if last is None:
        return True
    current = now or datetime.now()
    return (current - last) >= timedelta(hours=normalize_interval(interval_hours))


def format_now(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime(TIME_FORMAT)
