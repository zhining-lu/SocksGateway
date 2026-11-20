"""停止网关不能卡住。

回归用例：Python 3.12+ 的 Server.wait_closed() 会等待所有已建立连接结束。
如果先 await wait_closed()、后关 writer，有 keep-alive 空闲连接时就会一直挂住，
前端表现为「停止网关」超时（后端 Core.call 的 8 秒兜底也会抛 TimeoutError）。
"""
import asyncio
import time

from core.gateway import Gateway


def _cfg(port: int) -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": port},
        "upstream": {"scheme": "socks5h", "host": "127.0.0.1", "port": 18080,
                     "username": "", "password": ""},
        "dns_via_proxy": True,
        "bypass": [],
        "rules": [],
        "rule_list": {"enabled": False, "rules": []},
        "default_action": "proxy",
    }


def _stop_cost(idle_connections: int) -> float:
    async def scenario():
        gw = Gateway(_cfg(0))          # 0 = 让系统分配端口，避免占用固定端口
        await gw.start()
        port = gw.server.sockets[0].getsockname()[1]

        writers = []
        for _ in range(idle_connections):
            _r, w = await asyncio.open_connection("127.0.0.1", port)
            writers.append(w)
        await asyncio.sleep(0.1)       # 让 handler 起来并进入等待读状态

        t0 = time.perf_counter()
        await asyncio.wait_for(gw.stop(), 3)
        cost = time.perf_counter() - t0

        for w in writers:
            w.close()
        return cost

    return asyncio.run(scenario())


def test_stop_without_connections_is_fast():
    assert _stop_cost(0) < 1.0


def test_stop_with_idle_keepalive_connections_is_fast():
    """有空闲连接时必须立刻返回；改回旧顺序这里会卡到 3 秒超时。"""
    assert _stop_cost(3) < 1.0


def test_stop_with_many_connections_is_fast():
    assert _stop_cost(60) < 1.5


def test_gateway_can_restart_after_stop():
    async def scenario():
        gw = Gateway(_cfg(0))
        await gw.start()
        port = gw.server.sockets[0].getsockname()[1]
        _r, w = await asyncio.open_connection("127.0.0.1", port)
        w.close()
        await gw.stop()
        assert gw.server is None
        await gw.start()
        assert gw.running is True
        await gw.stop()

    asyncio.run(scenario())
