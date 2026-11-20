"""配置读写（%APPDATA%\\SocksGateway\\config.json）"""
from __future__ import annotations

import copy
import json
import os
import threading

APP_NAME = "SocksGateway"

DEFAULT_CONFIG = {
    # 本地监听（系统代理 / 浏览器指向这里）
    "listen": {"host": "127.0.0.1", "port": 18081},
    # 上游 SOCKS5 代理
    "upstream": {
        "scheme": "socks5h",      # socks5h = 域名交给代理解析（远程 DNS，防泄漏）
        "host": "127.0.0.1",
        "port": 18080,
        "username": "",
        "password": "",
    },
    "dns_via_proxy": True,
    # 不代理的地址列表（每行一个，支持 * 通配、CIDR 与 <local> 表示无点主机名）
    "bypass": [
        "127.0.0.1", "::1", "localhost", "<local>",
        "10.*", "172.16.*", "172.17.*", "172.18.*", "172.19.*", "172.20.*",
        "172.21.*", "172.22.*", "172.23.*", "172.24.*", "172.25.*", "172.26.*",
        "172.27.*", "172.28.*", "172.29.*", "172.30.*", "172.31.*",
        "192.168.*", "169.254.*", "*.local",
    ],
    # 自动切换规则（自顶向下，第一条命中生效）
    "rules": [
        {"type": "domain", "value": "internal.example.com", "action": "direct"},
        {"type": "wildcard", "value": "*.example.com", "action": "proxy"},
    ],
    "default_action": "proxy",   # proxy | direct

    "rule_list": {
        "url": "https://raw.githubusercontent.com/gfwlist/gfwlist/master/gfwlist.txt",
        "format": "gfwlist",     # gfwlist | autoproxy(同 gfwlist) | 纯文本域名
        "enabled": False,
        "update_interval_hours": 24,   # 自动更新间隔（小时），下限 1
        "last_update": None,
        "count": 0,
        "rules": [],
    },

    "system_proxy": {
        "apply_on_start": True,
        "restore_on_exit": True,
        "bypass_extra": True,    # 把不代理列表同步写入 Windows 例外
        # 记住「命令行工具 → 写入用户环境变量」的开关：网关启动时写入，
        # 停止 / 退出时清除（避免变量指向已关闭的端口）
        "write_user_env": False,
    },
    # 上游代理服务由外部程序提供，这里只记「用哪个脚本启动它」
    "upstream_launcher": {
        "bat_path": "",
    },
    "autostart": False,
    "start_minimized": False,
    "auto_start_gateway": False,   # 打开程序即启动网关（配合开机自启）
    # 启动时保存的系统代理原始值，退出/关闭时用于还原
    "backup": None,
}

_lock = threading.Lock()


def config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    with _lock:
        try:
            with open(config_path(), encoding="utf-8") as f:
                data = json.load(f)
            return _deep_merge(DEFAULT_CONFIG, data)
        except (OSError, ValueError):
            return copy.deepcopy(DEFAULT_CONFIG)


def save(cfg: dict) -> None:
    with _lock:
        tmp = config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, config_path())
