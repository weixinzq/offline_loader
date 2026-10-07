# 接口与依赖

> 本页记录当前源码中跨模块使用的契约。依据：`csharp/AolaLoader/BackendBridge.cs`、`src/ipc/server.py`、`src/accounts/*`、`src/network/*`、`src/messaging/*`、`src/scripting/*`；2026-09-27 静态核对，未做在线行为验证。

## WPF ↔ Python：本机命名管道

`BackendBridge.StartAsync` 生成管道名并启动 Python；Python `NamedPipeTransport` 建立服务端。双方按 UTF-8 JSON 对象逐行传输。C# 用请求 `id` 匹配待完成任务，Python 发 `response` 或不带请求 `id` 的 `event`。依据：`csharp/AolaLoader/BackendBridge.cs::StartAsync`、`SendAsync`、`ReadLoopAsync`，`src/ipc/named_pipe.py::NamedPipeTransport.read_json`、`write_json`，`src/ipc/server.py::DesktopBridge._respond`、`_emit`。

```json
{"id":"<请求标识>","action":"run_combination","payload":{"labels":["账号标签"],"steps":[],"repetitions":1,"interval":0.5}}
{"type":"response","id":"<请求标识>","ok":true,"data":{"started":true}}
{"type":"event","event":"task","kind":"combination","status":"completed","error":""}
```

上例只展示结构；`steps` 不能为空。`response.data.started` 表示后台任务已经创建，不表示执行完成；任务状态由 `task` 事件回传（`src/ipc/server.py::DesktopBridge._dispatch`、`_start_task`、`_watch_task`）。

| 接口族 | 输入/输出要点 | 双方位置 |
|---|---|---|
| 读状态 | `get_state` 返回 `accounts/scripts/messages`；`get_accounts` 返回 `accounts/connection_busy` | `src/ipc/server.py::DesktopBridge._state_payload`、`_dispatch`；`csharp/AolaLoader/MainWindow.xaml.cs::PollAccountStateAsync` |
| 改账号 | `add_account/edit_account/delete_account/get_account`；表单字段由 `AccountForm` 提交，后端写配置 | `csharp/AolaLoader/Models.cs::AccountForm`、`csharp/AolaLoader/MainWindow.xaml.cs::AddAccount_Click`、`src/ipc/server.py::DesktopBridge._mutate_account` |
| 连接 | `connect/reconnect/disconnect` 带 `labels`；先回 `started`，后发 `accounts`、`task`、`log` 事件 | `csharp/AolaLoader/MainWindow.xaml.cs::ConnectionActionAsync`、`src/ipc/server.py::DesktopBridge._run_connection_action` |
| 通用消息 | `parse_message` 传单条文本；`parse_message_file` 传路径；`send_messages` 传账号标签 | `csharp/AolaLoader/MainWindow.xaml.cs::ChooseMessageFile_Click`、`SendMessages_Click`，`src/ipc/server.py::DesktopBridge._dispatch` |
| 专用脚本 | `run_script` 传 `module` 和 `labels`；模块名在后端已发现脚本中查找 | `csharp/AolaLoader/MainWindow.xaml.cs::RunScript_Click`、`src/ipc/server.py::DesktopBridge._script` |
| 组合任务 | `run_combination` 传有序 `steps`、`repetitions`、`interval`；消息步骤为 `{kind:"messages",messages:[{id,cmd,param}]}`，脚本步骤为 `{kind:"script",module}` | `csharp/AolaLoader/MainWindow.xaml.cs::RunCombination_Click`、`src/ipc/server.py::DesktopBridge._combination_steps` |

WPF 的 `Models.cs` 与 Python 的 `_state_payload`、`_messages_payload`、`_combination_steps` 共同维护 JSON 字段名，当前没有独立共享的接口定义文件。修改字段时要同时核对双方及 `scripts/verify.py::test_bridge_account_config_persistence` 等相关验证；这是直接依赖，不是自动生成的契约。

## Python 内部的主要接口

| 契约 | 调用方 → 实现方 | 关键语义 |
|---|---|---|
| `AccountSpec` / `AccountConnection` | `DesktopBridge` → `src/accounts/manager.py` | 一个标签及合并后的配置对应一个独立 `AppContext`；`connect` 可重试，`disconnect` 清理上下文（`load_account_specs`、`AccountConnection.connect`、`disconnect`） |
| `AppContext` | dispatcher、composer、scripts → `src/network/context.py` | 提供 `socket`、`messages`、`battle`、`session_clock`、房间进入和自动化阻断状态（`AppContext`、`enter_room`、`assert_automation_allowed`） |
| `MessageSubscription.wait_for(predicate, timeout)` | 初始化、战斗入口、脚本 → `MessageRouter` | 先订阅再发送；在订阅者私有队列上等待匹配消息；用后 `close`。接收循环只由 `MessageRouter._receive_loop` 拥有（`src/network/context.py::MessageSubscription`、`MessageRouter`） |
| `SendMessage(id, cmd, param)` | UI/文件解析 → dispatcher、composer | 单条已校验的 EXT 消息；`parse_send_message_file` 先解析完整文件，再过滤会话自管 `55_2`（`src/messaging/parser.py::SendMessage`、`parse_send_message_file`） |
| `execute_messages(label, context, messages, delay)` | 桥接层/组合任务 → dispatcher | 预检整个批次，简单消息顺序发；含 `54_22` 的批次按战斗计划执行。返回 `SendResult` 列表（`src/messaging/dispatcher.py::_build_dispatch_plan`、`execute_messages`） |
| `InteractionScript.run(context)` | 桥接层/composer → `scripts/*.py` | 模块须提供异步 `run(context)`；由 `discover_scripts` 发现，脚本可订阅消息并直接使用该账号套接字（`src/scripting/loader.py::_load_script`、`discover_scripts`） |
| `execute_combination_for_accounts` | 桥接层 → composer | 多账号并发；每账号按步骤和轮次串行，失败时停止该账号的组合（`src/scripting/composer.py::execute_combination`、`execute_combination_for_accounts`） |

`GameSocket.send_xt_message(ext_id, cmd, params)` 负责序号与封包，并以单账号发送锁防止并发调用交错；`recv_message` 由路由器消费。其底层格式由 `src/protocol/message_codec.py`、`amf3.py`、`encrypt.py`、`msgseq.py` 提供（`src/network/socket_client.py::GameSocket.send_xt_message`、`recv_message`）。

仓库的 `Script/` 存放可由文件选择器导入的消息文本，`scripts/` 才是 `discover_scripts` 扫描的 Python 专用脚本目录；两者走不同入口（`csharp/AolaLoader/MainWindow.xaml.cs::ChooseMessageFile_Click`、`src/messaging/parser.py::parse_send_message_file`、`src/scripting/loader.py::discover_scripts`）。

## 依赖方向与例外

当前调用方向由 import 和调用点核对：WPF → 管道桥接 → accounts/messaging/scripting → network → protocol。脚本和 dispatcher 直接依赖 `AppContext`，`src/accounts/session.py::open_context` 负责组装 `GameSocket`、`MessageRouter`、`AppContext`。`src/messaging/audit_log.py` 只消费 `SendResult`，桥接层在任务结束后调用它。依据：`src/ipc/server.py`、`src/accounts/session.py`、`src/messaging/dispatcher.py`、`src/scripting/composer.py` 的导入与调用。

目前可见的例外与耦合点：

- `src/protocol/operations.py` 导入并调用 `GameSocket`，其中既有协议辅助函数也有发送操作，因此修改网络发送接口会影响它。
- `scripts/mt250816_1.py` 直接导入 `src/messaging/dispatcher.py::_end_previous_battle`；这是脚本对调度器私有函数的依赖。四象脚本也直接调用套接字发送，例如 `scripts/mt250816_1.py::run`、`scripts/mt250816_2.py::run`。
- WPF 与 Python 的管道 JSON、`#send` 文件格式及游戏 EXT 格式是三个不同边界；修改其中一个格式不会自动更新另外两个（`csharp/AolaLoader/BackendBridge.cs::SendAsync`、`src/messaging/parser.py::parse_send_message`、`src/network/socket_client.py::GameSocket.send_xt_message`）。

## 配置、敏感数据与可观察结果

`src/config.py::PROJECT_ROOT` 在源码模式指向仓库根目录，在 PyInstaller 模式指向可执行文件目录；`CONFIG_PATH` 是该目录的 `config.json`。后端通过 `DesktopBridge._save_connections` 写入账号信息；`config.example.json` 是模板，真实 `config.json` 被 `.gitignore` 排除。文档不记录任何账号、密码或会话值。

`src/messaging/audit_log.py::append_send_results` 将通用消息的 `SendResult` 写入 `logs/send-*.log`；`src/ui/runtime_log.py::configure_runtime_logging` 负责运行日志；`src/network/context.py::configure_battle_receive_logging` 记录符合过滤条件的战斗接收消息。这些日志的覆盖范围不同：脚本内直接发送的命令不进入 `SendResult` 汇总（`src/ipc/server.py::DesktopBridge._run_combination`）。`SendResult.success` 一般表示本地发送调用完成；需要服务器回包才能判断对应业务是否接受。静态源码没有证明服务器的最终处理结果。
