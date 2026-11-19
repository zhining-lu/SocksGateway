"""真实链路验证：用本机正在运行的 SOCKS5（默认 127.0.0.1:18080）跑一次出网检查。

运行： python tests/live_check.py [上游端口] [网关端口]
"""
from __future__ import annotations

import asyncio
import os
import socket
import ssl
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as cfgmod  # noqa: E402
from core import gateway as gwmod  # noqa: E402

UP_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
GW_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 28091


def https_via_gateway(host: str, path: str = "/") -> tuple[int, str]:
    """通过网关 CONNECT + TLS 抓取一个 HTTPS 页面。"""
    s = socket.create_connection(("127.0.0.1", GW_PORT), 8)
    s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(1024)
        if not chunk:
            raise RuntimeError("网关未返回 CONNECT 响应")
        buf += chunk
    head = buf.split(b"\r\n\r\n")[0]
    if b" 200 " not in head:
        raise RuntimeError(head.decode("latin-1", "ignore"))
    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(s, server_hostname=host)
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: SocksGateway/livecheck\r\n"
           "Accept: */*\r\nConnection: close\r\n\r\n")
    tls.sendall(req.encode())
    data = b""
    while True:
        chunk = tls.recv(8192)
        if not chunk:
            break
        data += chunk
    tls.close()
    status = int(data.split(b" ")[1])
    return status, data.split(b"\r\n", 1)[0].decode("latin-1", "ignore")


def main() -> int:
    cfg = cfgmod._deep_merge(cfgmod.DEFAULT_CONFIG, {
        "listen": {"port": GW_PORT},
        "upstream": {"host": "127.0.0.1", "port": UP_PORT, "scheme": "socks5h"},
        "bypass": ["127.0.0.1", "::1", "localhost"],
        "rules": [],
        "default_action": "proxy",
    })
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    gw = gwmod.Gateway(cfg)
    asyncio.run_coroutine_threadsafe(gw.start(), loop).result(5)
    print(f"网关 127.0.0.1:{GW_PORT} → socks5h 127.0.0.1:{UP_PORT}\n")

    ok = True
    for host, path in [("www.gstatic.com", "/generate_204"),
                       ("www.cloudflare.com", "/cdn-cgi/trace")]:
        t0 = time.time()
        try:
            status, line = https_via_gateway(host, path)
            dt = int((time.time() - t0) * 1000)
            flag = "PASS" if status in (200, 204) else "FAIL"
            ok &= status in (200, 204)
            print(f"[{flag}] https://{host}{path} → {line}  ({dt} ms)")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[FAIL] https://{host}{path} → {e}")

    # curl 交叉验证（如果系统里有 curl）
    curl = None
    for cand in ("curl", r"C:\Windows\System32\curl.exe"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True)
            curl = cand
            break
        except Exception:
            continue
    if curl:
        r = subprocess.run([curl, "-s", "-o", os.devnull, "-w", "%{http_code}",
                            "-x", f"http://127.0.0.1:{GW_PORT}",
                            "--max-time", "15", "https://www.wikipedia.org/"],
                           capture_output=True, text=True)
        code = (r.stdout or "").strip()
        flag = "PASS" if code.startswith("2") or code.startswith("3") else "FAIL"
        ok &= flag == "PASS"
        print(f"[{flag}] curl -x http://127.0.0.1:{GW_PORT} https://www.wikipedia.org/ → {code or r.stderr.strip()[:80]}")

    snap = gw.snapshot()
    print(f"\n统计：请求 {snap['stats']['total']}，上行 {snap['stats']['up']} B，下行 {snap['stats']['down']} B")
    for e in snap["log"][:5]:
        print(f"  {e['time']} {e['method']:<7} {e['host']}:{e['port']:<5} {e['action']:<6} {e['status']} {e['ms']}ms")

    asyncio.run_coroutine_threadsafe(gw.stop(), loop).result(5)
    print("\n" + ("真实链路可用 ✓" if ok else "真实链路存在问题 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
