"""最小可用的 asyncio SOCKS5 客户端（支持 socks5h 远程 DNS 与用户名密码认证）"""
from __future__ import annotations

import asyncio
import ipaddress
import struct

REPLY_TEXT = {
    0x01: "代理服务器内部错误",
    0x02: "规则不允许该连接",
    0x03: "网络不可达",
    0x04: "主机不可达（域名无法解析）",
    0x05: "目标拒绝连接",
    0x06: "TTL 过期",
    0x07: "不支持的命令",
    0x08: "不支持的地址类型",
}


class SocksError(Exception):
    pass


def _encode_addr(host: str, remote_dns: bool) -> tuple[int, bytes]:
    """返回 (ATYP, 地址字节)。remote_dns=True 时域名原样交给代理（socks5h）解析。"""
    host = host.strip().strip("[]")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.version == 4:
            return 0x01, ip.packed
        return 0x04, ip.packed
    raw = host.encode("idna") if any(ord(c) > 127 for c in host) else host.encode("utf-8")
    if len(raw) > 255:
        raise SocksError("域名过长")
    return 0x03, bytes([len(raw)]) + raw


async def connect(host: str, port: int,
                  proxy_host: str, proxy_port: int,
                  username: str = "", password: str = "",
                  remote_dns: bool = True,
                  timeout: float = 15.0) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """经 SOCKS5 代理连接到 host:port。"""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy_host, proxy_port), timeout)
    except asyncio.TimeoutError:
        raise SocksError(f"连接代理 {proxy_host}:{proxy_port} 超时") from None
    except OSError as e:
        raise SocksError(f"无法连接代理 {proxy_host}:{proxy_port}（{e.strerror or e}）") from e

    try:
        use_auth = bool(username)
        # 同时声明 no-auth 与 username/password，让代理选择
        methods = b"\x00\x02" if use_auth else b"\x00"
        writer.write(b"\x05" + bytes([len(methods)]) + methods)
        await writer.drain()

        ver, method = await asyncio.wait_for(reader.readexactly(2), timeout)
        if ver != 0x05:
            raise SocksError("代理返回的协议版本不是 SOCKS5")
        if method == 0xFF:
            raise SocksError("代理不接受任何认证方式")
        if method == 0x02:
            if not use_auth:
                raise SocksError("代理要求用户名/密码认证")
            u = username.encode("utf-8")
            p = password.encode("utf-8")
            writer.write(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            await writer.drain()
            _ver, status = await asyncio.wait_for(reader.readexactly(2), timeout)
            if status != 0x00:
                raise SocksError("用户名或密码错误")

        atyp, addr = _encode_addr(host, remote_dns)
        writer.write(b"\x05\x01\x00" + bytes([atyp]) + addr + struct.pack(">H", port))
        await writer.drain()

        head = await asyncio.wait_for(reader.readexactly(4), timeout)
        if head[0] != 0x05:
            raise SocksError("代理返回数据异常")
        if head[1] != 0x00:
            raise SocksError(REPLY_TEXT.get(head[1], f"SOCKS5 错误码 {head[1]}"))
        bnd = head[3]
        if bnd == 0x01:
            await reader.readexactly(4)
        elif bnd == 0x04:
            await reader.readexactly(16)
        elif bnd == 0x03:
            n = (await reader.readexactly(1))[0]
            await reader.readexactly(n)
        await reader.readexactly(2)   # BND.PORT：必须读掉，否则会污染隧道首包
        return reader, writer
    except asyncio.TimeoutError:
        writer.close()
        raise SocksError("代理响应超时") from None
    except asyncio.IncompleteReadError:
        writer.close()
        raise SocksError("代理提前断开连接") from None
    except Exception:
        writer.close()
        raise
