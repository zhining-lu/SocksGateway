"""规则匹配：不代理列表 + 自动切换规则（对应 Zero Omega 的「切换规则」）"""
from __future__ import annotations

import base64
import fnmatch
import ipaddress
import re


def norm_host(host: str) -> str:
    return (host or "").strip().strip("[]").rstrip(".").lower()


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _cidr_match(host: str, pattern: str) -> bool:
    try:
        return ipaddress.ip_address(host) in ipaddress.ip_network(pattern, strict=False)
    except ValueError:
        return False


def match_bypass(host: str, bypass_lines: list[str]) -> bool:
    """host 是否命中「不代理的地址列表」。"""
    h = norm_host(host)
    if not h:
        return True
    is_ip = _is_ip(h)
    for raw in bypass_lines:
        p = (raw or "").strip()
        if not p or p.startswith("#"):
            continue
        pl = p.lower()
        if pl == "<local>":
            if not is_ip and "." not in h:
                return True
            continue
        if "/" in pl and _cidr_match(h, pl):
            return True
        if pl.startswith("*."):
            if h == pl[2:] or h.endswith("." + pl[2:]):
                return True
            continue
        if "*" in pl or "?" in pl:
            if fnmatch.fnmatchcase(h, pl):
                return True
            continue
        if h == pl or h.endswith("." + pl):
            return True
    return False


def _wildcard_to_regex(pattern: str) -> re.Pattern:
    out = ["^"]
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    out.append("$")
    return re.compile("".join(out), re.I)


_regex_cache: dict[str, re.Pattern] = {}


def match_rule(host: str, rules: list[dict]) -> tuple[str | None, dict | None]:
    """按顺序匹配规则，返回 (action, rule)。"""
    h = norm_host(host)
    for rule in rules or []:
        rtype = (rule.get("type") or "domain").lower()
        value = (rule.get("value") or "").strip()
        if not value or not rule.get("enabled", True):
            continue
        action = rule.get("action") or "proxy"
        try:
            if rtype in ("domain", "域名"):
                if h == value.lower() or h.endswith("." + value.lower()):
                    return action, rule
            elif rtype in ("wildcard", "通配符", "域名通配符"):
                pat = value.lower()
                if "*" in pat or "?" in pat:
                    rx = _regex_cache.get(pat) or _wildcard_to_regex(pat)
                    _regex_cache[pat] = rx
                    if rx.match(h):
                        return action, rule
                elif h == pat or h.endswith("." + pat):
                    return action, rule
            elif rtype in ("regex", "正则"):
                if _cidr_match(h, value) if "/" in value else re.search(value, h, re.I):
                    return action, rule
        except re.error:
            continue
    return None, None


def decode_gfwlist(text: str) -> str:
    """gfwlist.txt 是 base64 编码的 AutoProxy 列表，自动解码。"""
    stripped = "".join(text.split())
    if stripped.startswith("[AutoProxy"):
        return text
    try:
        raw = base64.b64decode(stripped + "=" * (-len(stripped) % 4))
        decoded = raw.decode("utf-8", "ignore")
        if "[AutoProxy" in decoded:
            return decoded
    except Exception:
        pass
    return text


def parse_rule_list(text: str, fmt: str = "gfwlist") -> list[dict]:
    """把规则列表文本转成规则数组。"""
    body = decode_gfwlist(text) if fmt != "plain" else text
    rules: list[dict] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("!") or line.startswith("["):
            continue
        action = "proxy"
        if line.startswith("@@"):
            action = "direct"
            line = line[2:]
        if line.startswith("||"):
            domain = line[2:].split("^")[0].split("/")[0].strip("*.")
            if domain:
                rules.append({"type": "domain", "value": domain, "action": action})
        elif line.startswith("|") or line.startswith("."):
            domain = line.lstrip("|.").split("^")[0].split("/")[0]
            if domain:
                rules.append({"type": "domain", "value": domain, "action": action})
        elif line.startswith("/") and line.endswith("/") and len(line) > 2:
            rules.append({"type": "regex", "value": line[1:-1], "action": action})
        elif re.fullmatch(r"[\w\-.*]+(\.[\w\-]+)+", line):
            domain = line.strip("*.")
            if domain:
                rules.append({"type": "domain", "value": domain, "action": action})
    return rules
