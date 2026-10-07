# 项目维护入门教程

> 目标：沿一次代码调用顺序认识各部分，而不是按目录逐个猜用途。依据 2026-09-27 的当前工作区源码；这是静态阅读路线，没有做在线行为验证。系统边界和术语先看[架构总览](ARCHITECTURE.md)。

## 一张调用路线图

```text
WPF 按钮
  → MainWindow.CommandAsync(action, payload)
  → BackendBridge.SendAsync(JSON 行)
  → NamedPipeTransport / DesktopBridge._dispatch
  → 账号连接 AccountConnection / 消息 dispatcher / 组合 composer
  → AppContext.socket.send_xt_message
  → GameSocket 封包 + protocol → 游戏服务器

游戏服务器 → GameSocket.recv_message → MessageRouter.publish
  → AppContext 状态观察 / 等待回包的订阅者
  → DesktopBridge 事件 → BackendBridge.ReadLoopAsync → WPF
```

这条路线的入口与出口分别见 `csharp/AolaLoader/MainWindow.xaml.cs::CommandAsync`、`csharp/AolaLoader/BackendBridge.cs::SendAsync`、`src/ipc/server.py::DesktopBridge._dispatch`、`src/network/socket_client.py::GameSocket.send_xt_message`、`src/network/context.py::MessageRouter.publish`。JSON 字段和 Python 接口的准确定义在[接口与依赖](docs/architecture/interfaces.md)。

## 第 1 站：先认清运行的是哪一版

**读**：`csharp/AolaLoader/BackendBridge.cs::StartBackend`、`backend_main.py`、`src/ipc/server.py::main`、`build_wpf.ps1`、`aola_backend.spec`。

**弄明白**：WPF 优先启动同目录 `AolaBackend.exe`；找不到才以源码模块启动 Python。源码模式与打包模式的配置根目录不同（`src/config.py::PROJECT_ROOT`）。CLI 从 `cli_main.py` 进入 `src.app.main`，不经过 WPF 命名管道。

**读完回答**：改了 `src/scripting/composer.py` 以后，为什么正在运行的已打包桌面程序可能仍表现为旧逻辑？

**安全练习**：只查看两个入口和构建脚本，画出所用可执行文件到 `DesktopBridge` 的路径。下一站看进程之间传什么。

## 第 2 站：跟踪一个界面命令

**读**：`csharp/AolaLoader/MainWindow.xaml.cs::RunCombination_Click` → `csharp/AolaLoader/BackendBridge.cs::SendAsync` → `src/ipc/named_pipe.py::NamedPipeTransport.read_json` → `src/ipc/server.py::DesktopBridge._dispatch`、`_combination_steps`、`_start_task`。

**接口关系**：WPF 传 `action="run_combination"` 和 `payload={labels,steps,repetitions,interval}`；后端把消息步骤变成 `MessageBatchStep`、脚本步骤变成 `ScriptStep`。`response` 只确认任务启动，后续 `task`/`log`/`accounts` 等事件由后端推送（`src/ipc/server.py::DesktopBridge._watch_task`、`_emit`，`csharp/AolaLoader/BackendBridge.cs::ReadLoopAsync`）。

**读完回答**：如果新加一个组合步骤字段，要改 WPF 哪个构造点、后端哪个解析点？为什么不能仅改 `Models.cs`？

**安全练习**：只读搜索 `"run_combination"`，列出构造请求、接收请求、实际执行和显示完成状态的四个位置。下一站看执行所依赖的账号上下文。

## 第 3 站：理解一个账号的生命周期

**读**：`src/accounts/manager.py::AccountConnection.connect` → `src/accounts/session.py::open_context` → `authenticate` / `login_and_connect` → `src/network/context.py::AppContext`、`MessageRouter`；结束时看 `AccountConnection.disconnect` 和 `AppContext.close`。

**接口关系**：`AccountSpec` 保存标签与配置；`AccountConnection` 保存连接状态与 `context`；`AppContext` 持有 `socket`、`messages`、战斗状态和会话时钟。HTTP 登录返回用户与区服信息后，游戏连接先试 WSS 再试 TCP；玩家初始化是单独任务（`src/accounts/login.py::http_login`、`src/network/socket_client.py::login_and_connect`、`src/accounts/session.py::start_player_initialization`）。

**读完回答**：界面显示“在线”时，玩家资料初始化是否必然完成？战斗发送在哪里等待它完成？

**安全练习**：只读跟踪 `player_initialized` 从写入到使用的路径。下一站看消息如何发送和接收。

## 第 4 站：普通消息、战斗消息和收包

**读**：`src/messaging/parser.py::parse_send_message_file` → `src/messaging/dispatcher.py::_build_dispatch_plan`、`execute_messages`、`_BattleExecutor.run` → `src/network/socket_client.py::GameSocket.send_xt_message`。反向看 `GameSocket.recv_message` → `src/network/context.py::MessageRouter._receive_loop`、`publish`、`MessageSubscription.wait_for`。

**接口关系**：`SendMessage` 是已解析的 `{id,cmd,param}`；dispatcher 决定发送顺序与战斗入口门槛；socket 负责序号、编码和传输；router 将服务器消息分发给 `AppContext` 观察器及各订阅者。只有 `MessageRouter` 在稳定连接后持续读取套接字（`src/network/context.py::MessageRouter`）。

注意大小写不同的两个目录：`Script/` 是消息文本样例，按路径导入；`scripts/` 是 Python 专用脚本，按模块发现（`src/messaging/parser.py::parse_send_message_file`、`src/scripting/loader.py::discover_scripts`）。

**读完回答**：为什么战斗的 `54_22` 后面需要入口订阅，而普通消息的 `SendResult.success` 不能直接解释为服务器业务成功？

**安全练习**：只读搜索 `2303`，标出生命周期观察、入口确认和错误翻译三个位置。下一站看脚本和组合如何复用这些接口。

## 第 5 站：专用脚本与组合任务

**读**：`src/scripting/loader.py::discover_scripts`、`InteractionScript` → `src/scripting/composer.py::execute_combination`、`execute_combination_for_accounts` → `scripts/mt250816_1.py::run`。

**接口关系**：组合中 `MessageBatchStep` 使用 dispatcher，`ScriptStep` 直接调用脚本的 `async run(context)`。各账号并发，每个账号内部按步骤顺序执行；`message_delay` 控制同一批次及本轮步骤间隔，`repeat_interval` 控制轮与轮的间隔（`src/scripting/composer.py::execute_combination`、`src/messaging/dispatcher.py::DEFAULT_MESSAGE_DELAY`）。脚本等待回包应先订阅、再发送、最后关闭订阅（`scripts/mt250816_1.py::run`）。

**读完回答**：为什么组合任务右侧的通用消息汇总可能没有脚本内部发送的逐条记录？脚本 `run` 返回又能证明到什么程度？

**安全练习**：只读跟踪 `MT250816_panel` 从脚本发送到订阅匹配，再看 `MT250816_t2c` 如何产生。需要增加脚本时，记得核对 `aola_backend.spec::Analysis` 的打包清单。下一站学习如何维护和验证。

## 第 6 站：改动前后的检查

从[维护指南](docs/architecture/maintenance.md#改动从哪里开始)按改动类型找所有消费方，再选相应的静态或测试检查。优先使用 `scripts/verify.py` 里与改动直接对应的契约验证；相关例子有 `test_send_message_parser`、`test_battle_entry_then_ordered_sends`、`test_combined_message_and_script_execution`。构建与测试命令见[维护指南](docs/architecture/maintenance.md#建议的验证方式)。

记住三个不同结论：**代码路径存在**、**本地发送完成**、**服务器业务接受**。前两者可由源码和本地验证支持，第三者需要带方向和时间的实际服务器回包/状态证据（`src/messaging/dispatcher.py::SendResult`、`_BattleExecutor._next_battle_message`）。
