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
_REGEX_CACHE_LIMIT = 1024


def _wildcard_regex(pattern: str) -> re.Pattern:
    """通配符正则缓存。带上限，换规则源后旧 pattern 不会一直常驻。"""
    rx = _regex_cache.get(pattern)
    if rx is None:
        if len(_regex_cache) >= _REGEX_CACHE_LIMIT:
            _regex_cache.clear()
        rx = _wildcard_to_regex(pattern)
        _regex_cache[pattern] = rx
    return rx


# ---------------- 域名索引（反向 trie） ----------------
_END = "\x00"   # 终止标记，合法域名标签里不会出现


class _DomainTrie:
    """反向域名字典树：按 com -> example -> www 的层级落节点。

    命中判定与线性实现的 `h == v or h.endswith("." + v)` 完全等价，
    区别只是把 O(n) 全表扫描换成 O(域名层数) 的逐层下探。
    路径上每个终止节点都要检查：规则写 `example.com` 时，
    `a.example.com` 在走到 example 这一层就该命中。
    """

    __slots__ = ("root",)

    def __init__(self) -> None:
        self.root: dict = {}

    def add(self, domain: str, index: int) -> None:
        node = self.root
        for label in reversed(domain.split(".")):
            nxt = node.get(label)
            if nxt is None:
                nxt = node[label] = {}
            node = nxt
        prev = node.get(_END)
        if prev is None or index < prev:   # 同一域名重复出现，保留最靠前的一条
            node[_END] = index

    def first_index(self, host: str) -> int | None:
        """返回所有命中的域名规则里最靠前的原始下标，未命中返回 None。"""
        node = self.root
        best: int | None = None
        for label in reversed(host.split(".")):
            node = node.get(label)
            if node is None:
                break
            hit = node.get(_END)
            if hit is not None and (best is None or hit < best):
                best = hit
        return best


def _in_network(host: str, net) -> bool:
    try:
        return ipaddress.ip_address(host) in net
    except ValueError:
        return False


class _RuleIndex:
    """规则列表的匹配索引：域名走 trie，通配 / 正则 / CIDR 走紧凑线性表。

    linear 按原始下标升序排列，用来和 trie 结果比先后，保证第一条命中生效。
    """

    __slots__ = ("trie", "linear")

    def __init__(self) -> None:
        self.trie = _DomainTrie()
        self.linear: list[tuple[int, str, object, str, dict]] = []


def _build_index(rules: list[dict]) -> _RuleIndex:
    idx = _RuleIndex()
    for i, rule in enumerate(rules):
        if not rule.get("enabled", True):
            continue
        rtype = (rule.get("type") or "domain").lower()
        value = (rule.get("value") or "").strip()
        if not value:
            continue
        action = rule.get("action") or "proxy"
        low = value.lower()

        if rtype in ("domain", "域名"):
            idx.trie.add(low, i)
        elif rtype in ("wildcard", "通配符", "域名通配符"):
            if "*" in low or "?" in low:
                idx.linear.append((i, "wild", _wildcard_regex(low), action, rule))
            else:
                # 不含通配符时与原逻辑一致：等价于域名后缀匹配
                idx.trie.add(low, i)
        elif rtype in ("regex", "正则"):
            if "/" in value:
                try:
                    net = ipaddress.ip_network(value, strict=False)
                except ValueError:
                    continue
                idx.linear.append((i, "cidr", net, action, rule))
            else:
                try:
                    rx = re.compile(value, re.I)
                except re.error:
                    continue
                idx.linear.append((i, "regex", rx, action, rule))

    idx.linear.sort(key=lambda e: e[0])
    return idx


_INDEX_CACHE_LIMIT = 8
_index_cache: dict[tuple[int, int], tuple[list, _RuleIndex]] = {}


def get_index(rules: list[dict]) -> _RuleIndex:
    """按 list 对象缓存索引，规则列表整体替换时自动重建。

    list 不支持 weakref，这里持一份强引用，防止对象被回收后 id 被复用导致
    命中错误缓存。原地改某个 rule 的 value 而长度不变时感知不到——现有代码
    保存配置都是整体替换列表，与此约定一致。
    """
    key = (id(rules), len(rules))
    hit = _index_cache.get(key)
    if hit is not None and hit[0] is rules:
        return hit[1]
    index = _build_index(rules)
    if len(_index_cache) >= _INDEX_CACHE_LIMIT:
        _index_cache.clear()
    _index_cache[key] = (rules, index)
    return index


def match_rule(host: str, rules: list[dict]) -> tuple[str | None, dict | None]:
    """按顺序匹配规则，返回 (action, rule)。

    域名类规则查反向 trie（O(域名层数)），通配 / 正则 / CIDR 顺序扫描；
    两边按原始下标取最靠前的一条，与逐条遍历的 first-match-wins 语义一致。
    """
    if not rules:
        return None, None
    h = norm_host(host)
    index = get_index(rules)
    best = index.trie.first_index(h)

    for pos, kind, payload, action, rule in index.linear:
        if best is not None and pos > best:
            break                      # 后面的下标只会更大，trie 已是最优
        if kind == "wild":
            if payload.match(h):       # type: ignore[union-attr]
                return action, rule
        elif kind == "regex":
            if payload.search(h):      # type: ignore[union-attr]
                return action, rule
        elif _in_network(h, payload):
            return action, rule

    if best is not None:
        rule = rules[best]
        return rule.get("action") or "proxy", rule
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
