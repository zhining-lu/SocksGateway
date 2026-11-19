"""端到端自检：本地起一个 SOCKS5 服务 + 一个 HTTP 源站，验证网关链路。

运行： python tests/selftest.py
"""
from __future__ import annotations

import asyncio
import http.server
import os
import socket
import socketserver
import struct
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as cfgmod  # noqa: E402
from core import gateway as gwmod  # noqa: E402

SOCKS_PORT = 28080
GW_PORT = 28081
ORIGIN_PORT = 28099

seen: list[tuple[str, str, int]] = []


# ---------------- 假 SOCKS5 服务（记录被请求的目标） ----------------
async def _relay(r1, w1, r2, w2):
    async def pipe(r, w):
        try:
            while True:
                d = await r.read(65536)
                if not d:
                    break
                w.write(d)
                await w.drain()
        except Exception:
            pass
        finally:
            try:
                w.close()
            except Exception:
                pass
    await asyncio.gather(pipe(r1, w2), pipe(r2, w1), return_exceptions=True)


async def socks_client(reader, writer):
    try:
        ver, n = await reader.readexactly(2)
        await reader.readexactly(n)
        writer.write(b"\x05\x00")
        await writer.drain()
        _ver, cmd, _rsv, atyp = await reader.readexactly(4)
        if atyp == 1:
            host = socket.inet_ntoa(await reader.readexactly(4))
            tag = "ipv4"
        elif atyp == 3:
            ln = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(ln)).decode()
            tag = "domain"
        else:
            host = str(asyncio.base_events.socket.inet_ntop(
                socket.AF_INET6, await reader.readexactly(16)))
            tag = "ipv6"
        port = struct.unpack(">H", await reader.readexactly(2))[0]
        seen.append((tag, host, port))
        try:
            r2, w2 = await asyncio.open_connection(host, port)
        except OSError:
            writer.write(b"\x05\x04\x00\x01" + b"\x00" * 6)
            await writer.drain()
            writer.close()
            return
        writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
        await writer.drain()
        await _relay(reader, writer, r2, w2)
    except Exception:
        try:
            writer.close()
        except Exception:
            pass


# ---------------- 源站 ----------------
class Origin(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"hello-from-origin " + self.path.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def http_via_gateway(path: str, host: str = "127.0.0.1") -> bytes:
    """走网关的普通 HTTP 代理请求（绝对 URI 形式）"""
    url = f"http://{host}:{ORIGIN_PORT}{path}"
    s = socket.create_connection(("127.0.0.1", GW_PORT), 6)
    s.sendall(f"GET {url} HTTP/1.1\r\nHost: {host}:{ORIGIN_PORT}\r\n"
              f"Connection: close\r\n\r\n".encode())
    data = b""
    while True:
        chunk = s.recv(8192)
        if not chunk:
            break
        data += chunk
    s.close()
    return data


def connect_via_gateway(host: str, port: int, path: str = "/") -> bytes:
    """走网关的 CONNECT 隧道"""
    s = socket.create_connection(("127.0.0.1", GW_PORT), 6)
    s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(1024)
        if not chunk:
            return buf
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    if b" 200 " not in head:
        s.close()
        return head
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    data = rest
    while True:
        chunk = s.recv(8192)
        if not chunk:
            break
        data += chunk
    s.close()
    return data


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    return ok


def main() -> int:
    origin = socketserver.ThreadingTCPServer(("127.0.0.1", ORIGIN_PORT), Origin)
    origin.daemon_threads = True
    threading.Thread(target=origin.serve_forever, daemon=True).start()

    loop = asyncio.new_event_loop()
    socks_srv = loop.run_until_complete(
        asyncio.start_server(socks_client, "127.0.0.1", SOCKS_PORT))
    threading.Thread(target=loop.run_forever, daemon=True).start()

    cfg = cfgmod.DEFAULT_CONFIG.copy()
    cfg = cfgmod._deep_merge(cfgmod.DEFAULT_CONFIG, {
        "listen": {"port": GW_PORT},
        "upstream": {"host": "127.0.0.1", "port": SOCKS_PORT, "scheme": "socks5h"},
        "bypass": [],
        "rules": [],
        "default_action": "proxy",
    })
    gw = gwmod.Gateway(cfg)
    asyncio.run_coroutine_threadsafe(gw.start(), loop).result(5)
    print(f"网关已启动 {gw.listen}，上游 socks5h 127.0.0.1:{SOCKS_PORT}\n")

    ok = True

    # 1) 普通 HTTP 代理（IP 目标）
    seen.clear()
    body = http_via_gateway("/alpha")
    ok &= check("HTTP 代理转发（IP 目标）", b"hello-from-origin /alpha" in body, body[:60].decode("latin-1"))
    ok &= check("上游实际收到连接", seen == [("ipv4", "127.0.0.1", ORIGIN_PORT)], str(seen))

    # 2) socks5h：域名原样交给代理解析
    seen.clear()
    body = http_via_gateway("/beta", host="localhost")
    ok &= check("socks5h 远程 DNS（域名交给代理）", ("domain", "localhost", ORIGIN_PORT) in seen, str(seen))

    # 3) CONNECT 隧道（HTTPS 走的就是这条路径）
    seen.clear()
    body = connect_via_gateway("localhost", ORIGIN_PORT, "/gamma")
    ok &= check("CONNECT 隧道建立并收到响应", b"hello-from-origin /gamma" in body, body[:60].decode("latin-1"))

    # 4) 规则命中「直接连接」时不应经过代理
    cfg["rules"] = [{"type": "domain", "value": "localhost", "action": "direct", "enabled": True}]
    seen.clear()
    body = connect_via_gateway("localhost", ORIGIN_PORT, "/direct")
    ok &= check("规则 direct：绕过上游直连", b"hello-from-origin /direct" in body and not seen,
                f"seen={seen}")
    # 规则通配符
    cfg["rules"] = [{"type": "wildcard", "value": "*.localhost", "action": "direct", "enabled": True}]
    seen.clear()
    body = connect_via_gateway("localhost", ORIGIN_PORT, "/wild")
    ok &= check("通配符规则不误伤本域", b"hello-from-origin /wild" in body and bool(seen), f"seen={seen}")

    # 5) 不代理列表
    cfg["rules"] = []
    cfg["bypass"] = ["localhost"]
    seen.clear()
    body = connect_via_gateway("localhost", ORIGIN_PORT, "/bypass")
    ok &= check("不代理列表生效", b"hello-from-origin /bypass" in body and not seen, f"seen={seen}")

    # 6) 失败路径返回 502
    cfg["bypass"] = []
    seen.clear()
    body = http_via_gateway("/x", host="127.0.0.1")
    cfg["rules"] = [{"type": "domain", "value": "127.0.0.1", "action": "direct", "enabled": True}]
    s = socket.create_connection(("127.0.0.1", GW_PORT), 6)
    s.sendall(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\nHost: 127.0.0.1:1\r\n\r\n")
    resp = s.recv(4096)
    s.close()
    ok &= check("目标不可达时返回 502", b"502" in resp, resp.split(b"\r\n")[0].decode("latin-1"))

    # 7) 环路保护
    s = socket.create_connection(("127.0.0.1", GW_PORT), 6)
    s.sendall(f"CONNECT 127.0.0.1:{GW_PORT} HTTP/1.1\r\n\r\n".encode())
    resp = s.recv(4096)
    s.close()
    ok &= check("指向代理自身时被拦截", b"502" in resp or "环路".encode() in resp,
                resp.split(b"\r\n")[0].decode("latin-1"))

    # 8) 统计与日志
    snap = gw.snapshot()
    ok &= check("流量统计与请求日志可用",
                snap["stats"]["total"] > 0 and snap["stats"]["down"] > 0 and len(snap["log"]) > 0,
                f"total={snap['stats']['total']} up={snap['stats']['up']} down={snap['stats']['down']}")

    asyncio.run_coroutine_threadsafe(gw.stop(), loop).result(5)
    socks_srv.close()
    origin.shutdown()
    print("\n" + ("全部通过 ✓" if ok else "存在失败项 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
