"""Socks Gateway 核心包。

- socks5:  asyncio SOCKS5 客户端（socks5h 远程 DNS、可选用户名密码）
- gateway: 本地 HTTP 代理网关（CONNECT 隧道 / HTTP 转发 / 路由判定 / 统计）
- rules:   不代理列表、域名规则、gfwlist 解析
- sysproxy: Windows 系统代理（WinINET 注册表）接管与还原
- pac:     PAC 脚本生成
- webui:   本地界面服务（静态文件 + 带令牌的 JSON API）
- config:  配置读写（%APPDATA%\\SocksGateway\\config.json）
"""

__version__ = "1.0.0"
__all__ = ["config", "gateway", "pac", "rules", "socks5", "sysproxy", "webui"]
