# 多出口分组验收记录

日期：2026-10-15。操作方法见[多出口账号分组](egress-groups.md)。本次使用独立测试数据和模拟游戏，不操作当前在线账号，不修改实际 Clash 配置。

| 检查 | 结果 | 证据 |
|---|---|---|
| Windows Python 3.12.9 后端回归 | 82 项，通过；其中 3 项 POSIX 专用测试跳过 | `build/egress-full-tests.log` |
| Linux Python 3.12.15 后端回归 | 82 项全部通过 | `build/linux-egress-evidence/tests-final.log` |
| 保留的协议与脚本回归 | Windows / Linux 均运行 38 项，37 项通过，1 项旧 CLI 测试按既定迁移跳过；`scripts.verify` 通过 | `build/egress-legacy-tests.log`、`build/egress-legacy-verify.log`、Linux 对应 final 日志 |
| 前端组件回归 | 22 项通过 | `build/egress-frontend-tests.log` |
| Vue 类型检查及生产构建 | 通过 | `build/egress-frontend-build.log` |
| OpenAPI 生成类型一致性 | `npm run api:check` 通过 | `docs/openapi.json`、`frontend/src/api.generated.ts` |
| Edge 155 完整模拟操作 | 创建、规则展示、探测、确认、批量分配和任务页面通过，无页面异常 | `frontend/test-results/browser-smoke.json` |
| 桌面 / 手机布局 | 1440×1080、390×844 截图核对通过，无横向页面溢出 | `frontend/test-results/egress-desktop.png`、`egress-mobile.png` |
| 2,000 账号选择性能 | 渲染 50 行，单选中位数 30.0 ms，全选 30.5 ms；选择时 0 个 API 请求 | `frontend/test-results/account-performance-egress.json` |

## 运行行为

- 100 个模拟常驻会话分布在四个真实独立执行进程：30 / 30 / 30 / 10。第 101 个在登录前因全局容量不足被拒绝；单组超限同样在登录前拒绝。
- 组容量为 2 时，7 个临时账号按完整生命周期依次执行，在线与预留之和始终不超过 2，结束后连接、占用和预留均清空。
- 使用真实脚本注册、消息路由和订阅执行 `rank_info`，游戏响应来自内存替身。取消等待响应的脚本保留已发生的一次本地写入，借用的常驻会话保留，服务器业务确认保持未知。
- 终止一组执行进程，该组账号离线并释放占用，另一组继续执行脚本。同一真实登录账号在不同分组中仍发生占用冲突。
- 模拟人工验证中断受影响任务批次，独立组常驻会话保留。
- Windows Job 对象与 Linux 父进程死亡信号均通过真实父子进程强制终止测试，父进程退出后专用执行进程停止。
- 执行进程创建中同时停止分组，等待启动过程结束后退出子进程，不遗留脱管进程。取消控制请求超时不递归发起取消。
- 旧代码打印不会破坏 JSON 通信；凭据及运行时 SID 从任务日志脱敏；执行进程环境移除服务管理员令牌。
- 未确认组无法登录；修改策略名清除确认。批量分配遇到缺失或占用账号不会部分保存，在线组不可修改，有关联账号的组不可删除。
- 从初始数据库 schema 升级后账号与凭据保留，旧账号不自动分组；重启不启动执行进程、不自动重放登录任务。

## Linux 环境与日志

SSH 别名 `aliyun-test`，实际用户 `codex-test`。独立目录为 `/home/codex-test/workspace/offline-loader-egress-20261015-2055`。所有测试、虚拟环境、pip 缓存和临时文件均在此目录；未使用 sudo/root，未访问生产数据库或密钥，未安装系统依赖。

基础 Python 来自此前授权工作区的私有运行时。外网下载较慢，停止了本次目录中未完成的 pip 下载，将此前验收环境的依赖复制到新虚拟环境；原环境保留不改。最终项目在新环境中离线重新构建并安装，`pip check` 通过，所有锁定依赖版本逐项核对通过。下载中止的临时产物留在本次 `tmp` 中，环境记录保留实际数量。

最终源码清单为 101 个白名单文件，全部 SHA-256 与当前本机源码匹配。归档不包含本机账号、真实配置、数据库或管理员令牌。远程保留原始日志及 `egress-evidence.tar.gz`；本地副本为 `build/linux-egress-evidence/`，包含测试、安装、依赖检查、`environment.json` 和源码哈希清单。证据目录已被 Git 忽略。

## 尚未现场验证

本次证明执行进程隔离、真实可执行文件路径、资源清理、公共 API/CLI 和模拟游戏的运行行为。未登录真实账号，未修改或验证现用 Clash TUN 的实际出口。IP 检测的模拟结果不能证明真实节点可用；实际 HTTPS、游戏 TCP/WSS 的规则命中，以及游戏服务器允许的同时在线数量，需配置节点后现场核对。
