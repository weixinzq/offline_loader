# 奥拉星长连接客户端

这是一个用于登录奥拉星服务器、维持游戏长连接并按需执行网络交互脚本的项目。保留 C# WPF 桌面端构建和 Python CLI 启动；桌面端的登录、协议和脚本后端使用 Python。




## 验证

```powershell
python -m scripts.verify
python -m compileall -q cli_main.py backend_main.py src scripts
dotnet build .\csharp\AolaLoader\AolaLoader.csproj -c Release
```

## 启动 CLI

安装运行依赖并从项目根目录启动：

```powershell
python -m pip install -r requirements.txt
python .\cli_main.py
```

## Linux 配置驱动的组合任务

安装运行依赖后，以 `tasks.example.json` 为模板填写总任务配置。账号密码仍放在
`accounts_file` 引用的配置中；任务文件按账号序号区间或标签选择账号。所有相对路径
以任务文件所在目录为基准，序号从 1 开始、区间两端包含。相同登录账号不能重复选择，
即使 `char_id` 不同也会在启动前报错。

```bash
python cli_main.py validate --job tasks.json
python cli_main.py start --job tasks.json
python cli_main.py start --job tasks.json --detach
python cli_main.py status --run RUN_ID
python cli_main.py stop --run RUN_ID
```

`validate` 不启动进程或登录账号。`start --detach` 返回运行编号，退出 SSH 后任务继续执行；
`status` 查看账号进度、轮次和进程状态；`stop` 清理该次运行的账号和进程，最多等待 20 秒。
运行目录默认是 `logs/jobs/RUN_ID`，可用 `--runs-dir` 为 start/status/stop 指定同一个目录。
进程管理需要 Linux `/proc`；Windows 无参数交互菜单保持原用法。

每个 worker 可设置 `mode`：

- `bounded`：`concurrency` 限制登录、初始化、全部轮次执行及断开的完整生命周期。
- `all_online`：`concurrency` 限制登录并发，登录成功的账号随后并行执行，结束后断开。

默认并发数为 5、轮数为 1、轮间隔为 0.5 秒、消息间隔为 0.2 秒。任务配置只控制外层组合，
不修改专用脚本内部的消息或间隔。账号失败只影响自身；遇到限流或验证码时停止该 worker
的后续登录并清理连接，其他 worker 独立运行。不自动重试登录或重启进程。

组合步骤支持：

```json
[
  {"kind":"messages","messages":[{"id":-1,"cmd":"#time","param":{"seconds":1}}]},
  {"kind":"messages","file":"messages.txt"},
  {"kind":"script","module":"scripts.auto_battle"}
]
```

消息文件使用现有 `#send=...|`、`#wait|`、`#time=...|` 格式；字符串内的竖线保留。
script 只能使用发现列表中的模块。旧脚本可能不返回发送明细，结果仅报告可追踪的本地写入，
不把缺失明细解释成“实际发送 0 条”，也不将本地写入视为服务器执行成功。

`clash.binary` 指定 Mihomo 二进制，`clash.config` 指定用户提供的节点 YAML。
程序只导入出站节点、代理组和代理集合，生成本次运行专用配置；不沿用原配置的 TUN、
控制接口或监听端口。每个 worker 的 `proxy.port` 必须唯一且大于 1024，`proxy.node`
必须存在，路由不能包含 DIRECT。入站只监听 127.0.0.1，不使用 sudo 或修改系统路由。
文件代理集合必须位于节点 YAML 所在目录内；HTTP 集合缓存重定向到运行目录。

HTTP 登录、TCP 和 WSS 均绑定 worker 的 SOCKS5 端口，域名交由代理解析，失败不回退直连。
程序验证端口属于本次 Mihomo 进程后才启动账号工作进程。各 worker 的运行、接收、发包及
任务结果文件独立保存；内部任务快照含所选账号配置，目录权限为 700、文件权限为 600，
不要分享 `job.json`。分享日志前仍应核对业务消息中自定义的敏感内容。

完整模拟验证（不使用真实游戏账号）：

```bash
python -B -m unittest scripts.test_cli_jobs scripts.test_rank_info -v
python -B -m scripts.verify
```

节点资料未提供时不能启动真实代理任务。部署后的两个端口不自动证明出口 IP 不同；
应先验证实际出口和账号初始化，再执行用户指定的真实组合任务。

## 构建 WPF 桌面程序

安装构建依赖并执行：

```powershell
python -m pip install -r requirements-build.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_wpf.ps1
```

输出文件：

```text
dist/AolaLoader/AolaLoader.exe
dist/AolaLoader/AolaBackend.exe
dist/AolaLoader/config.example.json
```


调试工具保留在 `scripts/debug/`，以模块方式运行，例如：

```powershell
python -m scripts.debug.wss_test2
```
