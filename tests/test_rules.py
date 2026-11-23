"""单元测试（无网络、无端口占用）：规则匹配、gfwlist 解析、PAC 生成。"""
import base64

from core import pac, rules


def test_bypass_matching():
    bypass = ["127.0.0.1", "::1", "localhost", "<local>",
              "10.*", "192.168.*", "*.corp.com", "172.20.0.0/16"]
    assert rules.match_bypass("localhost", bypass)
    assert rules.match_bypass("myhost", bypass)          # <local>
    assert rules.match_bypass("127.0.0.1", bypass)
    assert rules.match_bypass("10.1.2.3", bypass)
    assert rules.match_bypass("172.20.5.5", bypass)
    assert rules.match_bypass("oa.corp.com", bypass)
    assert not rules.match_bypass("www.example.com", bypass)
    assert not rules.match_bypass("11.1.2.3", bypass)


def test_rule_order_and_types():
    rs = [
        {"type": "domain", "value": "example.com", "action": "direct"},
        {"type": "wildcard", "value": "*.example.com", "action": "proxy"},
        {"type": "regex", "value": r"^ads?\d+\.", "action": "direct"},
    ]
    assert rules.match_rule("example.com", rs)[0] == "direct"
    assert rules.match_rule("a.example.com", rs)[0] == "direct"   # 第一条优先
    assert rules.match_rule("x.y.example.com", rs)[0] == "direct"
    assert rules.match_rule("ads12.foo.net", rs)[0] == "direct"
    assert rules.match_rule("unknown.org", rs)[0] is None


def test_wildcard_semantics():
    rs = [{"type": "wildcard", "value": "*.google.*", "action": "proxy"}]
    assert rules.match_rule("www.google.com", rs)[0] == "proxy"
    assert rules.match_rule("google.com", rs)[0] is None


def test_gfwlist_parse():
    body = "\n".join([
        "[AutoProxy 0.2.9]",
        "! comment",
        "||blocked.example",
        "@@||direct.example",
        "/^regex\\.example/",
        "plain.example",
    ])
    encoded = base64.b64encode(body.encode()).decode()
    parsed = rules.parse_rule_list(encoded, "gfwlist")
    values = {r["value"]: r["action"] for r in parsed}
    assert values.get("blocked.example") == "proxy"
    assert values.get("direct.example") == "direct"
    assert values.get("plain.example") == "proxy"
    assert any(r["type"] == "regex" for r in parsed)


def test_pac_generation():
    cfg = {
        "bypass": ["127.0.0.1", "localhost"],
        "rules": [{"type": "domain", "value": "intra.corp", "action": "direct"}],
        "rule_list": {"enabled": False, "rules": []},
        "default_action": "proxy",
    }
    text = pac.build_pac(cfg, "127.0.0.1:18081")
    assert "function FindProxyForURL" in text
    assert "intra.corp" in text
    assert "PROXY 127.0.0.1:18081" in text
    assert text.count("{") == text.count("}")


def test_domain_index_keeps_original_priority():
    """域名走 trie 之后，仍必须遵守原始顺序：下标最小的规则胜出。"""
    # 同一域名写了两条，取最靠前的一条
    rs = [
        {"type": "domain", "value": "dup.example", "action": "direct"},
        {"type": "domain", "value": "dup.example", "action": "proxy"},
    ]
    assert rules.match_rule("a.dup.example", rs)[0] == "direct"

    # 通配符在前、域名在后：通配符下标更小，应胜出
    rs = [
        {"type": "wildcard", "value": "*.example.com", "action": "direct"},
        {"type": "domain", "value": "example.com", "action": "proxy"},
    ]
    assert rules.match_rule("x.example.com", rs)[0] == "direct"

    # 域名在前、通配符在后：域名胜出
    rs = [
        {"type": "domain", "value": "example.com", "action": "proxy"},
        {"type": "wildcard", "value": "*.example.com", "action": "direct"},
    ]
    assert rules.match_rule("x.example.com", rs)[0] == "proxy"


def test_domain_suffix_and_deep_subdomain():
    rs = [{"type": "domain", "value": "example.com", "action": "proxy"}]
    assert rules.match_rule("example.com", rs)[0] == "proxy"
    assert rules.match_rule("a.b.c.example.com", rs)[0] == "proxy"
    assert rules.match_rule("www.example.com.", rs)[0] == "proxy"   # 尾点归一化
    assert rules.match_rule("notexample.com", rs)[0] is None        # 不能当子串误判
    assert rules.match_rule("example.com.cn", rs)[0] is None


def test_disabled_and_blank_rules_are_skipped():
    rs = [
        {"type": "domain", "value": "off.example", "action": "proxy", "enabled": False},
        {"type": "domain", "value": "   ", "action": "proxy"},
        {"type": "domain", "value": "on.example", "action": "direct"},
    ]
    assert rules.match_rule("www.off.example", rs)[0] is None
    assert rules.match_rule("www.on.example", rs)[0] == "direct"


def test_wildcard_without_glob_char_behaves_like_domain():
    rs = [{"type": "wildcard", "value": "corp.internal", "action": "direct"}]
    assert rules.match_rule("corp.internal", rs)[0] == "direct"
    assert rules.match_rule("a.corp.internal", rs)[0] == "direct"
    assert rules.match_rule("xcorp.internal", rs)[0] is None


def test_index_rebuilds_when_rule_list_is_replaced():
    """列表整体替换后必须重建索引，不能命中上一个列表的缓存。"""
    first = [{"type": "domain", "value": "a.example", "action": "proxy"}]
    assert rules.match_rule("a.example", first)[0] == "proxy"

    second = [{"type": "domain", "value": "a.example", "action": "direct"}]
    assert rules.match_rule("a.example", second)[0] == "direct"

    # 旧列表的结果不应被新列表污染
    assert rules.match_rule("a.example", first)[0] == "proxy"


def test_single_pipe_url_rules_keep_real_host():
    """`|http://host/` 这类 URL 规则要取出 host，不能塌成 'http:'。"""
    body = "\n".join([
        "[AutoProxy 0.2.9]",
        "|http://img.dlsite.jp/",
        "|https://secure.hustler.com",
        "|http://www.dmm.com/netgame",
        "|http://*.pimg.tw/",
    ])
    parsed = rules.parse_rule_list(body, "plain")
    assert [r["value"] for r in parsed] == [
        "img.dlsite.jp", "secure.hustler.com", "www.dmm.com", "pimg.tw"]
    assert not any(r["value"] in ("http:", "https:") for r in parsed)
    assert all(r["type"] == "domain" for r in parsed)
    assert rules.match_rule("img.dlsite.jp", parsed)[0] == "proxy"
    assert rules.match_rule("a.pimg.tw", parsed)[0] == "proxy"


def test_network_prefix_keeps_path_rules():
    """`||host/path` 仍按 host 收规则，路径部分不能污染域名。"""
    body = "\n".join([
        "[AutoProxy 0.2.9]",
        "||addons.mozilla.org/*-*/firefox/addon/ublock-origin/*",
        "||http://cdn.example.com/x",
    ])
    parsed = rules.parse_rule_list(body, "plain")
    assert [r["value"] for r in parsed] == ["addons.mozilla.org", "cdn.example.com"]


def test_slash_regex_not_treated_as_cidr():
    """regex 里含 `/` 不等于网段；解析失败要回退成正则，而不是把规则丢掉。"""
    rs = [
        {"type": "regex", "value": "10.0.0.0/8", "action": "proxy"},
        {"type": "regex", "value": r"^https?:\/\/[^\/]+blogspot\.(.*)", "action": "proxy"},
        {"type": "regex", "value": r"^ads?\d+\.", "action": "direct"},
        {"type": "regex", "value": "foo/bar", "action": "proxy"},
    ]
    kinds = [e[1] for e in rules.get_index(rs).linear]
    assert kinds == ["cidr", "regex", "regex", "regex"]
    assert rules.match_rule("10.1.2.3", rs)[0] == "proxy"
    assert rules.match_rule("ads12.foo.net", rs)[0] == "direct"
    # 带斜杠的正则被编译进索引，而不是静默消失
    blogspot = rules.get_index(rs).linear[1][2]
    assert blogspot.search("https://x.blogspot.com")


def test_merge_parsed_puts_direct_first():
    """`@@` 白名单必须排在被屏蔽域名之前，否则 first-match-wins 下永远失效。"""
    parsed = [
        {"type": "domain", "value": "gov.tw", "action": "proxy"},
        {"type": "domain", "value": "www.gov.tw", "action": "direct"},
        {"type": "domain", "value": "google.com", "action": "proxy"},
        {"type": "domain", "value": "adservice.google.com", "action": "direct"},
    ]
    merged = rules.merge_parsed(parsed)
    assert [r["action"] for r in merged] == ["direct", "direct", "proxy", "proxy"]
    assert rules.match_rule("www.gov.tw", merged)[0] == "direct"
    assert rules.match_rule("adservice.google.com", merged)[0] == "direct"
    assert rules.match_rule("www.google.com", merged)[0] == "proxy"
    assert rules.match_rule("gov.tw", merged)[0] == "proxy"    # 白名单不放大到父域
    assert rules.merge_parsed([]) == []


def test_gfwlist_end_to_end_whitelist_and_url_rules():
    """真实 gfwlist 的两种典型行：@@ 例外 + 单竖线完整 URL。"""
    body = "\n".join([
        "[AutoProxy 0.2.9]",
        "! comment",
        "||gov.tw",
        "@@||www.gov.tw",
        "|http://img.dlsite.jp/",
    ])
    encoded = base64.b64encode(body.encode()).decode()
    merged = rules.merge_parsed(rules.parse_rule_list(encoded, "gfwlist"))
    assert rules.match_rule("www.gov.tw", merged)[0] == "direct"
    assert rules.match_rule("img.dlsite.jp", merged)[0] == "proxy"
    assert rules.match_rule("nothing.example", merged)[0] is None
