"""验证官方 API 接管：写入后三处状态是否同步，还原后是否完全一致。

会真实修改系统代理，脚本用 try/finally 保证退出前还原。
"""
import json
import struct
import sys
import winreg

sys.path.insert(0, ".")

from core import config as cfgmod
from core import sysproxy as sp

BLOB_KEY = sp.INTERNET_SETTINGS + r"\Connections"


def read_blob() -> dict:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, BLOB_KEY) as k:
        b, _t = winreg.QueryValueEx(k, "DefaultConnectionSettings")
    ver, cnt, flags = struct.unpack_from("<III", b, 0)
    off = 12
    fields = {}
    for lbl in ("proxy", "bypass", "pac"):
        n = struct.unpack_from("<I", b, off)[0]
        off += 4
        fields[lbl] = b[off:off + n].decode("utf-8", "replace")
        off += n
    fields.update(version=hex(ver), counter=cnt, flags=hex(flags))
    return fields


def snapshot(tag: str) -> dict:
    reg = sp.read_current()
    conn = sp.read_conn()
    blob = read_blob()
    print(f"\n=== {tag} ===")
    print(f"注册表  ProxyEnable={reg['ProxyEnable']}  ProxyServer={reg['ProxyServer']}")
    print(f"        AutoConfigURL={reg['AutoConfigURL']!r}")
    print(f"WinINET flags={conn['flags']:#04x}  proxy={conn['proxy']}")
    print(f"        pac={conn['pac']!r}")
    print(f"Blob    flags={blob['flags']}  counter={blob['counter']}  proxy={blob['proxy']}")
    print(f"is_enabled={sp.is_enabled()}")
    return {"reg": reg, "conn": conn, "blob": blob}


before = snapshot("接管前")
backup = sp.apply_http_proxy("127.0.0.1", 18081, cfgmod.DEFAULT_CONFIG["bypass"])
print("\n备份 =", json.dumps(backup, ensure_ascii=False)[:160] + " ...")

try:
    after = snapshot("接管后")
    checks = {
        "注册表 ProxyEnable=1": after["reg"]["ProxyEnable"] == 1,
        "注册表 ProxyServer 指向网关": after["reg"]["ProxyServer"] == "http=127.0.0.1:18081;https=127.0.0.1:18081",
        "blob flags 已置使用代理位": bool(int(after["blob"]["flags"], 16) & 0x02),
        "blob proxy 已同步": after["blob"]["proxy"] == "http=127.0.0.1:18081;https=127.0.0.1:18081",
        "blob 修改计数已递增": after["blob"]["counter"] > before["blob"]["counter"],
        "PAC 已清空": after["conn"]["pac"] == "",
        "is_enabled=True": sp.is_enabled() is True,
    }
    print("\n--- 接管断言 ---")
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
finally:
    sp.disable(backup)

restored = snapshot("还原后")
same = {
    "注册表 ProxyEnable": restored["reg"]["ProxyEnable"] == before["reg"]["ProxyEnable"],
    "注册表 ProxyServer": restored["reg"]["ProxyServer"] == before["reg"]["ProxyServer"],
    "注册表 ProxyOverride": restored["reg"]["ProxyOverride"] == before["reg"]["ProxyOverride"],
    "WinINET flags": restored["conn"]["flags"] == before["conn"]["flags"],
    "WinINET proxy": restored["conn"]["proxy"] == before["conn"]["proxy"],
    "blob proxy": restored["blob"]["proxy"] == before["blob"]["proxy"],
    "is_enabled": sp.is_enabled() == (before["conn"]["flags"] & 0x02 != 0),
}
print("\n--- 还原断言 ---")
for name, ok in same.items():
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

bad = [k for k, v in {**checks, **same}.items() if not v]
print("\n结果:", "全部通过" if not bad else f"失败项 {bad}")
