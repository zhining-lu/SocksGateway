"""生成 PAC 脚本（给浏览器/其他客户端按规则分流用）"""
from __future__ import annotations

import time


def _js_str(s: str) -> str:
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_pac(cfg: dict, gateway_addr: str) -> str:
    bypass = [x.strip() for x in (cfg.get("bypass") or []) if x and x.strip()]
    rules = [r for r in (cfg.get("rules") or []) if r.get("value") and r.get("enabled", True)]
    rl = cfg.get("rule_list") or {}
    if rl.get("enabled"):
        rules += [r for r in (rl.get("rules") or []) if r.get("value")]
    default = cfg.get("default_action", "proxy")
    proxy = f"PROXY {gateway_addr}"

    lines = [
        "// Socks Gateway PAC — 由 Socks Gateway 自动生成",
        f"// 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f'var PROXY = "{proxy}";',
        "var DIRECT = \"DIRECT\";",
        "var BYPASS = [",
        ",\n".join(f"  {_js_str(b)}" for b in bypass),
        "];",
        "",
        "function _isPlainHost(h) { return h.indexOf('.') === -1; }",
        "",
        "function _match(host, pattern) {",
        "  pattern = pattern.toLowerCase();",
        "  if (pattern === '<local>') return _isPlainHost(host);",
        "  if (pattern.indexOf('/') > -1 && pattern.indexOf(':') > -1) {",
        "    return isInNetEx(host, pattern);",
        "  }",
        "  if (pattern.charAt(0) === '*') {",
        "    var suffix = pattern.substring(pattern.indexOf('.') + 1);",
        "    return dnsDomainIs(host, '.' + suffix) || host === suffix;",
        "  }",
        "  return host === pattern || dnsDomainIs(host, '.' + pattern);",
        "}",
        "",
        "function FindProxyForURL(url, host) {",
        "  host = host.toLowerCase();",
        "  for (var i = 0; i < BYPASS.length; i++) {",
        "    if (_match(host, BYPASS[i])) return DIRECT;",
        "  }",
    ]

    for r in rules:
        rtype = (r.get("type") or "domain").lower()
        value = r["value"]
        action = "DIRECT" if r.get("action") == "direct" else "PROXY"
        expr = {"domain": f'host === {_js_str(value.lower())} || dnsDomainIs(host, {_js_str("." + value.lower())})',
                "wildcard": f'shimMatches(host, {_js_str(value.lower())})',
                "regex": f'new RegExp({_js_str(value)}, "i").test(host)'}.get(rtype)
        if not expr:
            continue
        then = "PROXY" if action == "PROXY" else "DIRECT"
        lines.append(f"  if ({expr}) return {then};")

    lines += [
        f"  return {'PROXY' if default == 'proxy' else 'DIRECT'};",
        "}",
        "",
        "// 简易通配匹配（PAC 里没有 shExpMatch 实现的环境用）",
        "function shimMatches(str, pattern) {",
        "  var re = '^' + pattern.replace(/[.+^${}()|[\\]\\\\]/g, '\\\\$&')",
        "    .replace(/\\*/g, '.*').replace(/\\?/g, '.') + '$';",
        "  return new RegExp(re, 'i').test(str);",
        "}",
        "",
    ]
    return "\r\n".join(lines)
