# 长期维护指南

> 当前状态与建议分开写。依据当前工作区源码和构建配置，2026-09-27 核对；本页的代码关系不代表在线行为已验证。

## 改动从哪里开始

| 修改需求 | 首先查看 | 同时检查的边界 |
|---|---|---|
| 改 WPF 控件、状态展示 | `csharp/AolaLoader/MainWindow.xaml`、`csharp/AolaLoader/MainWindow.xaml.cs` | `csharp/AolaLoader/Models.cs`、`csharp/AolaLoader/BackendBridge.cs::ReadLoopAsync`、后端事件字段（`src/ipc/server.py::DesktopBridge._emit`） |
| 新增/修改桌面命令 | `csharp/AolaLoader/MainWindow.xaml.cs::CommandAsync`、`src/ipc/server.py::DesktopBridge._dispatch` | 请求/响应 JSON 字段、任务完成事件、账号互斥（`src/ipc/server.py::DesktopBridge._ensure_labels_idle`） |
| 调整账号连接/登录 | `src/accounts/session.py::open_context`、`src/accounts/manager.py::AccountConnection.connect` | HTTP 返回解析、`GameSocket.login`、初始化任务、断开清理、WPF 状态轮询 |
| 改 `#send` 格式 | `src/messaging/parser.py` | 桥接层 `parse_message*`、WPF 载入、`SendMessage` 调用方、`scripts/verify.py::test_send_message_parser` 与文件解析验证 |
| 改战斗发送 | `src/messaging/dispatcher.py::_build_dispatch_plan`、`_BattleExecutor` | `AppContext.battle`、消息订阅、房间状态、组合任务、`scripts/verify.py::test_battle_entry_then_ordered_sends` |
| 改组合任务间隔或顺序 | `src/scripting/composer.py::execute_combination` | `src/messaging/dispatcher.py::DEFAULT_MESSAGE_DELAY`、WPF `RunCombination_Click` 的 `interval`、组合验证 |
| 新增专用脚本 | `scripts/` 中现有 `async run(context)`、`src/scripting/loader.py::discover_scripts` | `aola_backend.spec::Analysis.datas/hiddenimports`、脚本结果/超时、`scripts/verify.py` 的脚本清单与行为验证 |
| 改游戏封包 | `src/network/socket_client.py::GameSocket`、`src/protocol/message_codec.py` | AMF3、加密、内外序号、回包解码、初始化与战斗消息的调用方 |
| 改账号配置字段 | `src/config.py`、`src/accounts/manager.py::load_account_specs`、`src/ipc/server.py::_save_connections` | WPF `AccountForm`、`config.example.json`、源码/打包目录各自的 `config.json` |

以上是影响检查清单，不表示每一项都必须改动。每次先沿调用链找所有消费者，再选择相应验证；接口详情见[接口与依赖](interfaces.md)，流程见[主要运行流程](flows.md)。

## 当前运行与发布边界

- **配置位置**：源码运行时 `PROJECT_ROOT` 是仓库根目录；打包运行时是后端可执行文件目录。即使字段相同，两处 `config.json` 也是不同文件（`src/config.py::PROJECT_ROOT`、`CONFIG_PATH`）。真实配置包含账号信息，由 `.gitignore` 排除。
- **构建产物**：`build_wpf.ps1` 将 Python 后端打包为 `AolaBackend.exe`，再发布 WPF 到 `dist/AolaLoader`；`BackendBridge.StartBackend` 优先启动同目录的已打包后端。改了 Python 源码但没有重新打包，不会改变该运行路径（`build_wpf.ps1`、`aola_backend.spec`、`csharp/AolaLoader/BackendBridge.cs::StartBackend`）。
- **独立工具**：`batch_send_template.py::main` 直接读取当前源码配置并批量连接/发送；`create_roles_batch.py::main` 是单独的角色创建工具，其默认配置路径指向 `dist/AolaLoader/config.json`，与源码配置路径不同。它们不经过 WPF 管道任务互斥，操作同一配置或账号时须单独核对边界（`batch_send_template.py::main`、`create_roles_batch.py::DEFAULT_CONFIG_PATH`、`src/ipc/server.py::DesktopBridge._ensure_labels_idle`）。
- **并发所有权**：每账号只有一个 `MessageRouter` 收包循环；套接字发送由 `_send_lock` 保护。桥接层通过 `task_labels` 阻止同账号同时运行其管理的不同任务；其他直接调用 `GameSocket` 的路径仍需遵守单账号序号/状态约束（`src/network/context.py::MessageRouter`、`src/network/socket_client.py::GameSocket.send_xt_message`、`src/ipc/server.py::DesktopBridge._ensure_labels_idle`）。
- **排查顺序**：先看界面 `log`/`task` 事件，再看 `logs/runtime-*.log` 与 `logs/send-*.log`；战斗回包的 JSONL 仅记录满足过滤条件的接收消息。逐条脚本直接发送与通用消息审计日志的覆盖范围不同（`src/ipc/server.py::_log`、`src/ui/runtime_log.py::configure_runtime_logging`、`src/messaging/audit_log.py::append_send_results`、`src/network/context.py::_is_battle_trace_message`）。

## 有依据的维护风险

| 优先级 | 当前事实与影响 | 最小处理方向 / 触发条件 |
|---|---|---|
| 高 | `GameSocket.connect_ws` 创建 TLS 上下文后关闭主机名检查与证书验证。WSS 连接不验证对端身份（`src/network/socket_client.py::GameSocket.connect_ws`）。 | 若运行环境必须信任服务器身份，先确定服务器证书要求，再恢复验证并做真实连接测试；不要仅凭静态修改宣称兼容。 |
| 中 | 管道 JSON 字段由 C# 匿名对象/模型和 Python 字典分别定义，字段变更可让另一侧在运行时失败（`csharp/AolaLoader/BackendBridge.cs::SendAsync`、`csharp/AolaLoader/Models.cs`，`src/ipc/server.py::DesktopBridge._dispatch`、`_state_payload`）。 | 改跨进程字段时，成对检查发送方、接收方和代表性契约测试。若接口频繁演进，再考虑共享 schema。 |
| 中 | 专用脚本可以直接发送游戏消息，`_run_combination` 的 `SendResult` 只收集 `MessageBatchStep`；因此一个“脚本运行完毕”不证明服务器已接受脚本的最终命令（`src/scripting/composer.py::execute_combination`、`src/ipc/server.py::_run_combination`、`scripts/mt250816_1.py::run`）。 | 遇到脚本业务状态争议时，核对带方向与时间的发送/回包记录；若确有需要，再为具体脚本加入对应回包验证。 |
| 中 | 新脚本在源码模式由目录自动发现，但打包清单显式列举现有四个脚本；漏改清单会导致源码与发布物不一致（`src/scripting/loader.py::discover_scripts`、`aola_backend.spec::Analysis`）。 | 增加或更名脚本时同步维护打包清单和验证，再检查发布物内容。 |

这里的优先级是基于静态结构与可能影响的维护排序，不是生产事故统计。服务器接受策略、脚本最终生效时间和 WSS 证书部署情况属于**未知**，需要运行环境证据才能确定。

## 建议的验证方式

按改动范围选取验证，避免把静态/单元结果说成在线成功：

1. 文档和路径：检查引用路径、符号、交叉链接与 `git diff --check`；本页所述事实应以源码重新核对。
2. Python 逻辑：`python -m scripts.verify` 覆盖解析、账号、路由/协议及发送/组合的部分契约；`python -m compileall -q cli_main.py backend_main.py src scripts` 检查语法（`scripts/verify.py`、`README.md`）。
3. WPF 或 IPC 字段：`dotnet build .\csharp\AolaLoader\AolaLoader.csproj -c Release` 检查编译；还需在界面和后端实际交互时确认事件顺序（`csharp/AolaLoader/BackendBridge.cs::ReadLoopAsync`）。
4. 发布内容：修改打包脚本后按 `build_wpf.ps1` 构建，并确认正在使用的是同一个输出目录。构建成功只证明发布产物生成，不证明游戏连接成功。
5. 游戏协议行为：在授权的测试账号上比较发送和接收时间、命令与服务器业务状态；`SendResult.success` 本身只覆盖客户端发送或战斗入口的局部确认（`src/messaging/dispatcher.py::SendResult`、`_BattleExecutor._ensure_entry_ready`）。

## 文档更新规则

新增命令、共享字段或状态时更新[接口与依赖](interfaces.md)；改变从触发到结果的顺序时更新[主要运行流程](flows.md)；改变入口、部署或模块所有权时更新[总览](../../ARCHITECTURE.md)；改变推荐阅读顺序时更新[教程](../../tutorial.md)。不要把推断写成已验证的运行结果。
