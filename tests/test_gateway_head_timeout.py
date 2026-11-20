"""请求头读取超时：半开连接不能长期占用 task / writer（slowloris 防护）。

没有超时的时候，客户端连上网关只发半截请求头就停住，_read_head 会永久
挂在 read()，对应 task 与 writer 一直留在集合里（UI 的 active 还是 0，
完全看不出来）。本用例把超时调到 0.1s，验证超时后资源被回收，且正常
请求头（即使分片到达）不受影响。
"""
import asyncio

from core.gateway import Gateway


def _cfg(port: int, head_timeout: float) -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": port},
        "upstream": {"scheme": "socks5h", "host": "127.0.0.1", "port": 18080,
                     "username": "", "password": ""},
        "dns_via_proxy": True,
        "bypass": [],
        "rules": [],
        "rule_list": {"enabled": False, "rules": []},
        "default_action": "proxy",
        "head_timeout": head_timeout,
    }


async def _settle(predicate, timeout=2.0):
    """等条件成立（task 的清理在 done_callback 里，需要让出事件循环）。"""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def _half_open_scenario():
    gw = Gateway(_cfg(0, head_timeout=0.1))
    await gw.start()
    port = gw.server.sockets[0].getsockname()[1]
    r, w = await asyncio.open_connection("127.0.0.1", port)
    try:
        w.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: exa")  # 无 \r\n\r\n
        await w.drain()
        data = await asyncio.wait_for(r.read(), 3)
        return gw, data
    finally:
        w.close()


def test_half_open_header_is_rejected_and_cleaned():
    async def scenario():
        gw, data = await _half_open_scenario()
        try:
            assert b"502" in data, data[:120]
            assert b"\xe8\xaf\xb7\xe6\xb1\x82\xe5\xa4\xb4\xe6\x8e\xa5\xe6\x94\xb6\xe8\xb6\x85\xe6\x97\xb6" in data, data[:160]
            assert await _settle(lambda: not gw._writers and not gw._tasks)
            assert gw.stats["failed"] >= 1
        finally:
            await gw.stop()
    asyncio.run(scenario())


def test_split_header_not_killed_by_timeout():
    """请求头分两次到、总耗时低于超时：应正常解析，不误判为半开连接。"""
    async def scenario():
        cfg = _cfg(0, head_timeout=1.0)
        # direct 规则：让 127.0.0.1:1 立即被拒绝，不必等上游 15s 超时
        cfg["rules"] = [{"type": "domain", "value": "127.0.0.1",
                         "action": "direct", "enabled": True}]
        gw = Gateway(cfg)
        await gw.start()
        port = gw.server.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        try:
            w.write(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\nHo")
            await w.drain()
            await asyncio.sleep(0.15)
            w.write(b"st: 127.0.0.1:1\r\n\r\n")
            await w.drain()
            data = await asyncio.wait_for(r.read(), 3)
            # 目标端口 1 不可达 -> 502 但不是「请求头接收超时」
            assert b"502" in data, data[:120]
            assert b"\xe8\xaf\xb7\xe6\xb1\x82\xe5\xa4\xb4\xe6\x8e\xa5\xe6\x94\xb6\xe8\xb6\x85\xe6\x97\xb6" not in data, data[:160]
        finally:
            w.close()
            await gw.stop()
    asyncio.run(scenario())


def test_head_timeout_defaults_to_15s():
    gw = Gateway(_cfg(0, head_timeout=0.1))
    gw_default = Gateway({
        "listen": {"host": "127.0.0.1", "port": 0},
        "upstream": {"scheme": "socks5h", "host": "127.0.0.1", "port": 18080,
                     "username": "", "password": ""},
        "bypass": [], "rules": [],
    })
    assert gw.head_timeout == 0.1
    assert gw_default.head_timeout == 15.0
