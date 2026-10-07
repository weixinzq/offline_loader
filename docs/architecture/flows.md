# 主要运行流程

> 以下是当前源码的调用路径，不是联网成功记录。2026-09-27 静态核对；消息实际到达和业务接受仍须运行时证据。

## 启动与账号连接

1. WPF 窗口加载时创建 `BackendBridge`，生成唯一管道名，启动同目录的 `AolaBackend.exe`；若该文件不存在则在项目根目录启动 `python -m src.ipc.server`。之后开启事件读取和账号状态轮询。依据：`csharp/AolaLoader/MainWindow.xaml.cs::Window_Loaded`、`PollAccountStateAsync`，`csharp/AolaLoader/BackendBridge.cs::StartAsync`、`StartBackend`。
2. Python `DesktopBridge.__init__` 读取配置、构建 `AccountManager`、发现脚本；`run` 发初始账号与脚本事件，并接收管道请求。配置解析失败时保留启动错误，账号配置写入被阻断。依据：`src/ipc/server.py::DesktopBridge.__init__`、`run`。
3. 点击连接后，桥接层先校验标签与任务冲突，建立异步 `connection` 任务并立即回 `started`。任务逐账号调用 `AccountConnection.connect`；该方法处理旧上下文清理和有限重试。依据：`src/ipc/server.py::DesktopBridge._dispatch`、`_start_task`、`_run_connection_action`，`src/accounts/manager.py::AccountConnection.connect`。
4. `open_context` 先用 `http_login` 查询角色并登录，选择区服；`login_and_connect` 先尝试 WSS，失败后尝试 TCP，再完成游戏登录。成功后创建 `MessageRouter` 与 `AppContext`，启动唯一收包循环及异步玩家初始化。依据：`src/accounts/session.py::authenticate`、`open_context`，`src/accounts/login.py::http_login`，`src/network/socket_client.py::login_and_connect`。
5. 初始化通过订阅等待启动信息与玩家资料响应，然后建立 `55_9 → 55_1 → 周期性 55_2` 会话时钟。`open_context` 返回时初始化任务可能还在进行；战斗发送会调用 `wait_for_player_initialization` 等它完成。依据：`src/accounts/session.py::initialize_player_context`、`initialize_session_clock`、`start_player_initialization`，`src/messaging/dispatcher.py::_BattleExecutor.run`。

**失败与状态**：HTTP/长连接失败由 `AccountConnection` 记录状态和错误，并按其重试策略处理；初始化失败记录在 `AppContext.player_initialization_error`，不等同于连接建立失败。WPF 轮询 `get_accounts` 并接收 `accounts`/`log`/`task` 事件。依据：`src/accounts/manager.py::AccountConnection.connect`、`src/accounts/session.py::_run_player_initialization`、`src/ipc/server.py::DesktopBridge._run_connection_action`、`csharp/AolaLoader/MainWindow.xaml.cs::Bridge_EventReceived`。

## 收包与订阅

`GameSocket.recv_message` 解帧、解码；每账号的 `MessageRouter._receive_loop` 是稳定阶段的唯一读取者，并调用 `publish`。`AppContext` 以观察器更新战斗生命周期、房间和会话阻断状态；初始化、战斗入口或脚本以独立订阅队列等待关心的回包。队列满时会丢弃该队列最旧的一条消息。依据：`src/network/socket_client.py::GameSocket.recv_message`、`src/network/context.py::MessageRouter._receive_loop`、`publish`、`AppContext._observe_message`。

维护时先 `subscribe` 再 `send_xt_message`，最后在 `finally` 中关闭订阅；已有范例见 `src/accounts/session.py::initialize_player_context`、`src/network/context.py::AppContext.enter_room`、`scripts/mt250816_1.py::run`。不要为同一账号再开一个并行读取 `recv_message` 的循环，否则可能抢走订阅所需的回包；这是从现有单接收器所有权推导的维护约束。

## 通用消息与战斗入口

1. WPF 将粘贴文本或文件路径传给后端；`parse_send_message` 严格校验一条 `#send=<JSON>|`，`parse_send_message_file` 先校验整份文件，并过滤文件中的会话自管 `55_2`。解析结果保存在 `DesktopBridge.messages`，通过 `messages` 事件回显。依据：`csharp/AolaLoader/MainWindow.xaml.cs::ParseMessage_Click`、`ChooseMessageFile_Click`，`src/ipc/server.py::DesktopBridge._dispatch`，`src/messaging/parser.py::parse_send_message_file`。
2. `send_messages` 创建任务；`dispatch_to_accounts` 并发处理不同账号，每账号 `execute_messages` 保持原顺序。发送前 `_build_dispatch_plan` 检查会话自管命令、扩展 ID、房间与战斗消息顺序。依据：`src/ipc/server.py::DesktopBridge._run_send`，`src/messaging/dispatcher.py::dispatch_to_accounts`、`_build_dispatch_plan`。
3. 普通计划在相邻消息之间使用 `DEFAULT_MESSAGE_DELAY`。战斗计划先等待玩家初始化；如果需要动态房间，先通过 `AppContext.enter_room` 等到 `joinOK`；发送 `54_22` 后，由 `_BattleExecutor._ensure_entry_ready` 等待 `2303/2401/2426/2402` 入口消息，随后按顺序及间隔发送后续战斗消息。依据：`src/messaging/dispatcher.py::_execute_simple_plan`、`_BattleExecutor.run`、`_ensure_entry_ready`。
4. 如果上一场战斗仍被本地状态标为活动，下一消息批次开始前 `_end_previous_battle` 会发 `1404` 并更新本地状态；它不等待最终 `2403` 才开始下一批。异常时战斗执行器停止且不自动重发已提交消息。依据：`src/messaging/dispatcher.py::_end_previous_battle`、`execute_messages`、`_BattleExecutor.run`。

**结果含义**：普通消息的 `SendResult.success` 来自 `send_xt_message` 返回；战斗入口还要求入口响应齐全。后续回合消息没有逐回合确认门槛。服务器拒绝 `2303` 会成为任务失败，日志中的“已提交”计数是发送调用次数，不是服务器接受次数。依据：`src/messaging/dispatcher.py::_execute_simple_plan`、`_BattleExecutor._send`、`_next_battle_message`、`run`。

## 组合任务与专用脚本

WPF 先把当前通用消息复制为组合步骤快照，或将脚本模块名加入步骤列表；提交 `run_combination` 时发送有序步骤、轮数和轮间隔。Python 将其解析为 `MessageBatchStep` / `ScriptStep`，对各账号并发执行；每账号内按步骤串行。依据：`csharp/AolaLoader/MainWindow.xaml.cs::AddMessagesStep_Click`、`AddScriptStep_Click`、`RunCombination_Click`，`src/ipc/server.py::DesktopBridge._combination_steps`、`_run_combination`，`src/scripting/composer.py::execute_combination_for_accounts`。

`MessageBatchStep` 调用 `execute_messages`；`ScriptStep` 调用脚本的 `run(context)`。步骤成功后，本轮还有下一步骤时使用 `message_delay` 等待；每轮结束后才使用界面的 `repeat_interval`。默认消息间隔取自 `src/messaging/dispatcher.py::DEFAULT_MESSAGE_DELAY`，与界面的轮间隔是两种不同参数。脚本内部的等待和发送顺序由脚本自行决定。依据：`src/scripting/composer.py::execute_combination`、`src/messaging/dispatcher.py::DEFAULT_MESSAGE_DELAY`。

四象脚本示例：`scripts/mt250816_1.py::run` 先订阅 `MT250816_panel`，请求面板、等待回包，比较历史值与本次值，发送 `MT250816_t2c`；第一关脚本还有活动战斗时的 `1404` 处理。`scripts/mt250816_2.py::run` 使用另一组索引。脚本 `run` 返回代表当前代码路径完成，不证明服务器已经应用选择。`DesktopBridge._run_combination` 的 `SendResult` 汇总只包含通用消息步骤；脚本直接发出的命令不会成为其中的逐条结果。

任一账号的某步失败时，该账号的组合提前结束；其他账号由 `asyncio.gather` 独立执行。取消任务会传播 `CancelledError`；桥接层经 `task` 事件告知状态。依据：`src/scripting/composer.py::execute_combination`、`execute_combination_for_accounts`，`src/ipc/server.py::DesktopBridge._watch_task`。

## 账号配置写入

WPF 账号表单经管道交给 `_mutate_account`。桥接层拒绝修改在线账号，构建新账号列表后调用 `_save_connections`：先写同目录临时 JSON，再替换 `CONFIG_PATH`，重新加载并重建 `AccountManager`，然后推送账号事件。账号密码来自本地 `config.json`，不要复制到文档、提交或诊断输出中。依据：`csharp/AolaLoader/MainWindow.xaml.cs::AddAccount_Click`、`EditAccount_Click`，`src/ipc/server.py::DesktopBridge._mutate_account`、`_save_connections`，`src/config.py::CONFIG_PATH`。
