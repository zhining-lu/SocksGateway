# Socks Gateway

把本机 SOCKS5h 代理（默认 `127.0.0.1:18080`）接成 **Windows 系统级代理** 的桌面小工具。

Windows 的「系统代理」只认 HTTP 代理，不认 SOCKS；而 `socks=host:port` 这种写法大多数应用并不买账。
本程序在本机起一个 HTTP 代理网关（默认 `127.0.0.1:18081`），把流量按规则转交给上游 SOCKS5，
并把系统代理指向这个网关——于是 Edge / Chrome / Office / Electron 应用都能走 SOCKS5 出网，
且域名交给上游解析（socks5h），本地不做 DNS 查询，不会泄漏。

```
应用 ──HTTP代理──> 127.0.0.1:18081 (本程序) ──SOCKS5h──> 127.0.0.1:18080 (你的代理) ──> 互联网
```

界面本身跑在 `127.0.0.1` 的随机端口上（每次启动不同，带会话令牌），窗口用 WebView2 内嵌；
接口是普通 HTTP + JSON，`curl` 就能调试，不依赖浏览器扩展或任何在线服务。

## 快速开始

**方式一（推荐）**：双击 `dist\SocksGateway.exe`
**方式二**：双击 `启动 SocksGateway.bat`（需要 Python 3.10+ 与 `pip install pywebview`）

1. 确认上游 SOCKS5 已在 `127.0.0.1:18080` 监听；
2. 点右上角 **启动网关**；
3. 拨动 **接管系统代理**（或直接点「启动网关」时已按设置自动接管）；
4. 打开浏览器访问任意网站验证；在「概览 → 最近请求」能看到实时连接。

不需要管理员权限：只写 `HKCU` 注册表。

## 用 IDE 打开（IntelliJ IDEA / PyCharm）

工程已经按 IDE 习惯整理好，直接 `File → Open` 选目录 `C:\Users\Lu\Desktop\SocksGateway` 即可：

| 已准备好 | 说明 |
| --- | --- |
| `.venv/` | 项目内虚拟环境，已装好 pywebview / pytest / ruff / pyinstaller，IDEA 会自动识别 |
| `.idea/` | 模块（`SocksGateway.iml`）、源码根、测试根、编码 UTF-8 |
| `.run/` | 5 个运行配置，右键即可 Run / Debug |
| `pyproject.toml` | 依赖声明、pytest 与 ruff 配置（IDEA 会直接用） |
| `conftest.py` | 把工程根加入 `sys.path`，测试与调试时 `from core import ...` 不会报红 |
| `tests/test_rules.py` | 纯单元测试（规则匹配 / gfwlist 解析 / PAC），毫秒级 |
| `tests/test_smoke.py` | 端到端冒烟：子进程跑 selftest，自带假 SOCKS5 与源站 |

运行配置：

- **Socks Gateway（桌面界面）** —— `app.py`，可直接断点调试；界面在 WebView2 里，逻辑都在 Python 侧
- **无界面模式（--headless）** —— 只跑网关 + 接管系统代理，适合脚本/计划任务
- **协议自检 selftest** —— 9 项协议级检查
- **真实链路 live_check** —— 走真实上游 `18080` 验证出网
- **界面服务（浏览器调试）** —— 只起 HTTP 服务，改完前端刷新浏览器即可，不必重启窗口

常用命令：

```bash
.venv/Scripts/python.exe -m pytest      # 全部测试（约 15 秒）
.venv/Scripts/ruff.exe check .          # 静态检查
```

> 单实例保护只对打包后的 exe 生效；IDE 里可以反复启动调试，不会互相顶掉。
>
> 若 IDEA 提示解释器无效，选 `Project Structure → SDK → Add → Existing → .venv\Scripts\python.exe` 即可；
> `.run` 里的配置已直接指向该路径，选好解释器就能直接跑。

## 界面说明

| 页面 | 作用 |
| --- | --- |
| 概览 | 运行状态、实时上下行速率、并发连接、最近请求日志、连通性自检 |
| 代理服务器 | 上游协议/地址/端口、认证、本地监听端口、不代理的地址列表 |
| 切换规则 | 按域名（域名 / 通配符 / 正则）决定「通过代理」还是「直接连接」 |
| 规则列表 | 订阅 AutoProxy / gfwlist 列表（`||domain` 走代理，`@@||domain` 直连） |
| 系统代理 | 查看当前注册表代理项、接管/还原、WinHTTP、命令行环境变量、PAC 导出 |

默认已在「不代理的地址列表」里放好回环与内网网段（`127.*`、`10.*`、`172.16-31.*`、`192.168.*`、`*.local`），
内网 ERP / 数据库 / 打印机不会被绕进代理。

## 它到底改了什么

接管时写入 `HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings`：

```
ProxyEnable   = 1
ProxyServer   = http=127.0.0.1:18081;https=127.0.0.1:18081
ProxyOverride = <不代理列表> + 你原有的例外项（合并保留）
```

然后调用 `InternetSetOption` 并广播 `WM_SETTINGCHANGE`，让已打开的应用立即重读。

- 接管前的原始值会备份到配置文件里，点「还原 / 关闭」、点「停止网关」或在窗口关闭时（默认开启）都会精确还原；
- 如果程序被强杀，系统代理会停留在网关地址上——重新打开程序并关闭系统代理即可，或手动把 `ProxyEnable` 置 0。

## 覆盖范围（重要）

| 程序类型 | 是否生效 |
| --- | --- |
| Edge / Chrome / Firefox（用系统代理时）、Office、Electron 应用、.NET `WebRequest` | ✅ 走网关 |
| 自己读 `HTTP_PROXY` 的 CLI（git/npm/pip…） | 打开「系统代理 → 写入用户环境变量」 |
| 走 WinHTTP 的系统服务 | 「设置 WinHTTP（管理员）」 |
| 直接建 TCP 连接的程序（部分游戏、P2P、QQ/微信主流量） | ❌ 系统代理管不到，需要 TUN 模式 |

程序只能接管「愿意读系统代理」的流量，这点和 Zero Omega 只能管浏览器是同一类边界，别指望它等于全局 VPN。

## 自检

```bash
# 1. 上游 SOCKS5 是否可用（socks5h）
python tests/live_check.py 18080 28091

# 2. 网关链路（HTTP / CONNECT / 规则 / 不代理列表 / 502 / 环路保护）
python tests/selftest.py

# 3. 手动验证
curl -x http://127.0.0.1:18081 https://www.gstatic.com/generate_204 -o NUL -w "%{http_code}\n"
```

界面里也有「检测上游 SOCKS5」「检测网关出网」两个按钮。

## 文件与路径

- 配置：`%APPDATA%\SocksGateway\config.json`
- 代理备份：`%APPDATA%\SocksGateway\proxy-backup.json`（接管前的原始注册表值，配置被删也能还原）
- 日志：`%APPDATA%\SocksGateway\app.log`（窗口模式无控制台，排障看这里）
- 导出：`socks-gateway.pac`、`enable-proxy.bat`

```
app.py                 入口：创建窗口 + 启动本地界面服务 + 后台事件循环
conftest.py            pytest 引导（把工程根加入 sys.path）
core/__init__.py       包元信息（版本号唯一来源）
core/socks5.py         asyncio SOCKS5 客户端（socks5h / 用户名密码）
core/gateway.py        HTTP 代理前端：CONNECT 隧道、HTTP 转发、路由判定、统计
core/rules.py          不代理列表、通配符/正则规则、gfwlist 解析
core/sysproxy.py       WinINET 注册表接管与还原、WinHTTP、用户环境变量
core/pac.py            PAC 生成
core/webui.py          本地界面服务（静态文件 + 带令牌的 JSON API）
core/config.py         配置读写（%APPDATA%\SocksGateway\config.json）
ui/                    界面（HTML/CSS/JS，无外部依赖，支持 #overview 这类深链）
tests/                 单元测试 + 协议自检 + 真实链路验证
tools/                 开发辅助：图标生成、无窗口起服务、批量截图、诊断
```

开发时可以直接用浏览器调界面（不弹窗口）：

```bash
python tools/serve.py     # 打印带令牌的 http://127.0.0.1:xxxxx/?t=...
```

## 重新打包

```bash
.venv/Scripts/python.exe -m PyInstaller --noconfirm --onefile --windowed --name SocksGateway ^
  --icon assets/icon.ico --collect-all webview --add-data "ui;ui" app.py
```

`--collect-all webview` 建议保留：pywebview 在部分 WebView2 版本上缺少运行时文件会导致内嵌窗口起不来。

无界面运行（做计划任务/脚本时有用）：

```bash
SocksGateway.exe --headless
```

## 版本管理

仓库已初始化（`main` 分支，首个提交打 `v1.0.0` 标签），换行统一为仓库内 LF、`.bat` 检出 CRLF。

```bash
git status                  # 看改动
git log --oneline --graph   # 看历史
git diff                    # 看具体差异
git tag -a v1.1.0 -m "说明" # 打版本
```

已忽略：`.venv/`、`dist/`、`build/`、缓存、`.idea/workspace.xml`、`.workbuddy/`。
`dist/SocksGateway.exe` 是构建产物默认不入库；若确实想把 exe 放进历史，用 `git add -f dist/SocksGateway.exe`。

接远端：

```bash
git remote add origin <仓库地址>
git push -u origin main --tags
```
