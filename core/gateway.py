"""本地 HTTP 代理网关：接收系统代理流量 → 按规则直连或转交上游 SOCKS5（socks5h）

它负责三件事：
1. HTTP / HTTPS(CONNECT) 请求的接管与转发；
2. 「不代理的地址列表」与「切换规则」的路由判定（可直连、可走代理）；
3. 流量统计与最近请求日志（给界面展示）。
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from urllib.parse import urlsplit

from . import rules as rulemod
from . import socks5

MAX_HEAD = 64 * 1024
# 转发块大小。每块要走一次事件循环（读、写、drain），实测块越大往返次数越少、
# 吞吐越高；128 KB 之后收益基本饱和（再大只是多占内存），故定在这里。
CHUNK = 128 * 1024


def _now() -> float:
    return time.time()


class Gateway:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.server: asyncio.AbstractServer | None = None
        self.running = False
        self.started_at: float | None = None
        self.listen = f"127.0.0.1:{cfg['listen']['port']}"
        # 请求头读取超时：客户端连上后只发半截头就停住（slowloris / 异常中断），
        # 没有超时的话 _read_head 会永久挂在 read()，对应 task 与 writer 一直被持有。
        # 做成可配置仅为测试方便，生产默认 15s，覆盖正常客户端的建连+发头时间。
        self.head_timeout = float(cfg.get("head_timeout", 15.0))
        self.stats = {"up": 0, "down": 0, "active": 0, "total": 0, "failed": 0}
        self.log: deque = deque(maxlen=200)
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        if self.running:
            return
        host = self.cfg["listen"].get("host") or "127.0.0.1"
        port = int(self.cfg["listen"]["port"])
        self.server = await asyncio.start_server(self._handle, host, port, backlog=256)
        self.running = True
        self.started_at = _now()
        self.listen = f"{host}:{port}"

    async def stop(self) -> None:
        self.running = False
        server, self.server = self.server, None

        # 1) 先停止接受新连接
        if server is not None:
            server.close()

        # 2) 再断开已建立的连接。顺序不能反：Python 3.12+ 的 wait_closed()
        #    会等所有连接处理完才返回，而连接恰恰要靠这一步关闭才会结束，
        #    先 await 就会一直挂到调用方超时（有 keep-alive 连接时必现）。
        writers = list(self._writers)
        self._writers.clear()
        for w in writers:
            try:
                w.close()
            except Exception:
                pass

        # 3) 取消在途请求
        tasks = [t for t in self._tasks if not t.done()]
        self._tasks.clear()
        for t in tasks:
            t.cancel()

        # 4) 兜底等待：对端不响应 / 半关闭时不至于无限拖住
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), 3.0)
            except Exception:
                pass
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), 3.0)
            except Exception:
                pass

        self.stats["active"] = 0

    # ---------- 路由判定 ----------
    def _route(self, host: str) -> tuple[str, str]:
        cfg = self.cfg
        if rulemod.match_bypass(host, cfg.get("bypass") or []):
            return "direct", "不代理列表"
        action, rule = rulemod.match_rule(host, cfg.get("rules") or [])
        if action:
            return action, f"规则 {rule.get('value')}"
        # 规则列表（gfwlist 等）命中的域名走代理，其余按默认动作
        rl = cfg.get("rule_list") or {}
        if rl.get("enabled") and rl.get("rules"):
            action2, rule2 = rulemod.match_rule(host, rl.get("rules") or [])
            if action2:
                return action2, "规则列表"
        return cfg.get("default_action", "proxy"), "默认"

    # ---------- 连接建立 ----------
    async def _dial(self, host: str, port: int, action: str):
        up = self.cfg["upstream"]
        listen_port = int(self.cfg["listen"]["port"])
        loopback = host in ("127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0")
        if loopback and port == listen_port:
            raise socks5.SocksError("目标指向网关自身，已阻止环路")
        if action == "proxy":
            if loopback and port == int(up["port"]):
                raise socks5.SocksError("目标指向代理自身，已阻止环路")
            return await socks5.connect(
                host, port,
                up["host"], int(up["port"]),
                username=up.get("username", ""),
                password=up.get("password", ""),
                remote_dns=self.cfg.get("dns_via_proxy", True) or up.get("scheme") == "socks5h",
                timeout=15.0,
            )
        return await asyncio.wait_for(asyncio.open_connection(host, port), 15.0)

    # ---------- 主处理 ----------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        if task:
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        self._writers.add(writer)
        entry = None
        try:
            # 半开连接（只发部分请求头就停住）不能永久占用 task/writer；
            # 超时交给下面的 TimeoutError 分支统一记失败并回 502、关闭连接。
            try:
                head, rest = await asyncio.wait_for(
                    self._read_head(reader), self.head_timeout)
            except asyncio.TimeoutError:
                raise socks5.SocksError("请求头接收超时") from None
            if head is None:
                return
            first, _, header_blob = head.partition(b"\r\n")
            try:
                method, target, version = first.decode("latin-1").split(" ", 2)
            except ValueError:
                await self._fail(writer, 400, "请求格式错误")
                return

            if method.upper() == "CONNECT":
                entry = await self._handle_connect(target, reader, writer, rest)
            else:
                entry = await self._handle_plain(method, target, version, header_blob,
                                                 reader, writer, rest)
        except (socks5.SocksError, OSError, asyncio.TimeoutError) as e:
            msg = getattr(e, "strerror", None) or str(e) or e.__class__.__name__
            self.stats["failed"] += 1
            if entry is not None:
                entry["status"] = "失败"
                entry["error"] = msg
            try:
                await self._fail(writer, 502, msg)
            except Exception:
                pass
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self.stats["failed"] += 1
            if entry is not None:
                entry["status"] = "失败"
                entry["error"] = f"{e.__class__.__name__}: {e}"
        finally:
            self.stats["active"] = max(0, self.stats["active"] - 1)
            self._writers.discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_connect(self, target: str, reader, writer, rest: bytes):
        host, _, port_s = target.rpartition(":")
        if not host:
            host, port_s = target, "443"
        port = int(port_s) if port_s.isdigit() else 443
        action, why = self._route(host)
        entry = self._log(method="CONNECT", host=host, port=port, action=action, why=why)
        up_r, up_w = await self._dial(host, port, action)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n"
                     b"Proxy-Agent: SocksGateway\r\n\r\n")
        await writer.drain()
        entry["status"] = "已连接"
        entry["connected"] = True
        if rest:
            up_w.write(rest)
            await up_w.drain()
        await self._relay(reader, writer, up_r, up_w, entry)
        return entry

    async def _handle_plain(self, method, target, version, header_blob, reader, writer, rest):
        if not target.lower().startswith("http://"):
            await self._fail(writer, 400, "只支持代理 http:// 与 CONNECT 请求")
            return None
        parts = urlsplit(target)
        host = parts.hostname or ""
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        action, why = self._route(host)
        entry = self._log(method=method, host=host, port=port, action=action, why=why)
        up_r, up_w = await self._dial(host, port, action)

        headers = self._rewrite_headers(header_blob, host, parts.port or 80)
        req = f"{method} {path} {version}\r\n{headers}\r\n\r\n".encode("latin-1", "replace")
        up_w.write(req)
        if rest:
            up_w.write(rest)
        await up_w.drain()
        entry["status"] = "转发中"
        await self._relay(reader, writer, up_r, up_w, entry)
        return entry

    @staticmethod
    def _rewrite_headers(header_blob: bytes, host: str, port: int) -> str:
        drop = {"proxy-connection", "proxy-authorization", "connection", "keep-alive"}
        lines = []
        have_host = False
        for raw in header_blob.split(b"\r\n"):
            if not raw.strip():
                continue
            line = raw.decode("latin-1")
            name = line.split(":", 1)[0].strip().lower()
            if name in drop:
                continue
            if name == "host":
                have_host = True
            lines.append(line)
        if not have_host:
            lines.insert(0, f"Host: {host}" + ("" if port == 80 else f":{port}"))
        lines.append("Connection: close")
        return "\r\n".join(lines)

    # ---------- 双向转发 ----------
    async def _relay(self, cr, cw, ur, uw, entry: dict):
        def pipe(src, dst, key):
            async def run():
                try:
                    while True:
                        data = await src.read(CHUNK)
                        if not data:
                            break
                        dst.write(data)
                        await dst.drain()
                        self.stats[key] += len(data)
                        entry[key] += len(data)
                except Exception:
                    pass
                finally:
                    try:
                        dst.close()
                    except Exception:
                        pass
            return run()

        await asyncio.gather(
            pipe(cr, uw, "up"),
            pipe(ur, cw, "down"),
            return_exceptions=True,
        )
        entry["status"] = "完成"
        entry["ms"] = int((_now() - entry["t0"]) * 1000)
        for w in (uw, cw):
            try:
                w.close()
            except Exception:
                pass

    # ---------- 工具 ----------
    def _log(self, method, host, port, action, why) -> dict:
        entry = {
            "t0": _now(),
            "time": time.strftime("%H:%M:%S"),
            "method": method,
            "host": host,
            "port": port,
            "action": action,
            "why": why,
            "status": "连接中",
            "up": 0,
            "down": 0,
            "ms": 0,
            "error": "",
        }
        self.stats["total"] += 1
        self.stats["active"] += 1
        self.log.appendleft(entry)
        return entry

    @staticmethod
    async def _read_head(reader):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await reader.read(8192)
            if not chunk:
                return None, b""
            buf += chunk
            if len(buf) > MAX_HEAD:
                raise OSError("请求头过大")
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head, rest

    @staticmethod
    async def _fail(writer, code: int, msg: str):
        body = f"Socks Gateway: {msg}".encode()
        writer.write(
            f"HTTP/1.1 {code} Bad Gateway\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"Proxy-Agent: SocksGateway\r\n\r\n".encode("latin-1") + body
        )
        await writer.drain()
        writer.close()

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "listen": self.listen,
            "uptime": int(_now() - self.started_at) if self.started_at and self.running else 0,
            "stats": dict(self.stats),
            "log": list(self.log)[:60],
        }
