# 多出口账号分组

一个后台统一保存账号、占用和任务。分组账号的 HTTP 登录、TCP/WSS 长连接、初始化和脚本执行全部位于该组的独立执行进程中；Clash TUN 依据进程完整路径选择出口。网页和 CLI 仍然连接同一个后台。

每组默认最多30个在线账号，可设置为1–100；所有组与默认出口共用全局容量，默认为100，可通过 `--max-online` 调整。全局 `--login-concurrency` 单独限制登录和初始化，不限制已在线账号的任务执行。这里的30是本程序的分组容量设置，不能证明游戏服务器存在固定的30账号IP限额。游戏实际接受数量需要现场验证。10组300账号的部署与使用见[云端工作流手册](cloud-workflows.md)。

## Windows 网页操作

1. 等当前任务结束，按需断开现有连接，正常停止旧服务，再用原数据目录启动更新后的后台。数据库自动增加分组表，已有账号保持未分组。网页强制刷新一次。不要在第二个终端重新创建正在使用的虚拟环境；使用已有 `.venv\Scripts\python.exe`。
2. 打开“出口分组”。创建“出口一”“出口二”等组，上限先保留 30。策略名称填写 Clash 中**已经存在**的节点或策略组名称，必须完全一致。为各组选择不同的实际出口，避免它们最终选到同一个节点。
3. 点击“生成 Clash 规则”。将 `rules` 条目合并到 Clash 当前规则最前面，保留原有其他规则与节点配置。如果编辑 Clash Verge 当前订阅的“附加规则”文件，应把列表条目放入该文件已有的 `prepend:` 列表开头，而不是在其中新增 `rules:`。设置 `mode: rule`、`find-process-mode: strict`，保持 TUN 开启。推荐通过 Clash Verge 当前版本提供的持久覆写功能合并，避免更新订阅时丢失。程序不自动改写 Clash，也不管理节点订阅。
4. 在每组点击“检测出口”，核对显示的公网 IP 与进程路径。检测使用该组进程请求 `https://api.ipify.org?format=json`，不执行游戏登录。多个组出现同一 IP 时，页面给出提示。IP 检测只证明这次 HTTPS 请求的出口；仍应在 Clash 连接记录中核对游戏实际目标的进程、规则与节点。
5. 编辑分组，勾选“已在 Clash 中启用本组进程规则，并核对出口”，保存。修改策略名称后确认会被清除，需要更新规则并再次确认。
6. 在“账号管理”勾选第一批最多 30 个账号，选择“出口一”，点击“分配所选账号”；下一批分配到“出口二”。只允许调整离线且未被任务占用的账号。已有在线连接需要先断开后再分配。未分组账号继续使用默认系统出口。
7. 选中分配后的账号，点击“连接”。例如 90 个账号可分为 30 / 30 / 30，分别绑定三个不同出口。不要只在一个组内一次连接 90 个账号。

规则片段形如（以页面生成的真实路径为准）：

```yaml
mode: rule
find-process-mode: strict
rules:
  - PROCESS-PATH,D:\game\offline_loader\data\service\egress-workers\GROUP_ID_1\AolaWorker.exe,节点一
  - PROCESS-PATH,D:\game\offline_loader\data\service\egress-workers\GROUP_ID_2\AolaWorker.exe,节点二
```

使用完整进程路径区分组；单独按 `AolaWorker.exe` 进程名匹配会把多个组归到同一个规则。移动数据目录或更换服务器后，重新生成并部署当地的规则。策略组中途换节点可能影响已有长连接，应先断开受影响的组再调整。

## CLI 与接口

CLI 与网页使用同一套 `/api/v1` 接口；CLI 不读取服务数据库。先设置 `AOLA_ADMIN_TOKEN`，远程时增加 `--url`。以下命令在已安装项目的虚拟环境中运行；Windows 也可使用 `.venv\Scripts\python.exe cli_main.py` 代替 `aola`。

```text
aola --json egress create --name 出口一 --policy 节点一 --max-online 30
aola --json egress list
aola --json egress rules
aola --json egress probe GROUP_ID
aola --json egress confirm GROUP_ID
aola --json egress assign ACCOUNT_ID_1 ACCOUNT_ID_2 --group GROUP_ID
aola --json sessions connect ACCOUNT_ID_1 ACCOUNT_ID_2 --concurrency 2
aola --json egress assign ACCOUNT_ID_1 --clear
aola --json egress update GROUP_ID --file group.json
aola --json egress delete GROUP_ID
```

`egress rules` 返回 JSON，其中 `yaml` 是待合并片段。`confirm` 应在规则生效、出口核对后执行。`update` 文件内容：

```json
{"name":"出口一","clash_policy":"节点一","max_online":30,"routing_confirmed":true}
```

| 方法与路径 | 用途 |
|---|---|
| `GET/POST /egress-groups` | 查询 / 创建 |
| `GET/PUT/DELETE /egress-groups/{group_id}` | 详情 / 修改 / 删除 |
| `GET /egress-groups/rules` | 生成进程规则片段 |
| `POST /egress-groups/{group_id}/probe` | 检测本组公网 IP |
| `POST /accounts/assign-egress` | `account_ids` 批量分配；`egress_group_id: null` 移出分组 |

账号、会话和逐账号任务结果增加可空 `egress_group_id`。任务仍按账号 ID 提交，不携带节点凭据或客户端路径。SSE 新增 `egress` 通知。分配操作先检查整批账号，再一次性保存，避免部分成功。

## 容量、取消和进程生命周期

- 常驻连接、`all_online` 临时任务：启动前一次性检查并预留每组和全局容量。超过上限返回 `egress_capacity_exceeded` 或 `capacity_exceeded`，不开始登录。
- `bounded` 临时任务：按组限制完整的连接、初始化、执行、断开生命周期；目标账号可超过本组上限，后续账号等待清理后进入。同一真实登录账号的互斥仍由中央服务统一检查，跨组也不能绕过。
- 取消等待执行进程确认停止，再清理临时连接并释放占用。借用的常驻会话保留。本地写入计数与服务器业务确认仍分别记录，脚本完成不自动表示服务器业务成功。
- 人工验证停止受影响的整个任务批次；其他独立任务继续运行。单组进程退出会使本组连接离线，其他组独立运行。程序不会自动重新登录或重放被中断的任务。
- 正常停服先取消任务、关闭连接，再退出执行进程。Windows 使用父进程持有的 Job 对象，Linux 使用父进程死亡信号，避免服务崩溃后遗留游戏连接。进程测试使用模拟游戏验证这些行为。
- 专用可执行文件位于服务数据目录 `egress-workers/GROUP_ID/`，启动时从当前基础 Python 复制。Windows 同时复制所需 Python 运行库；Linux 使用当前基础运行时。仅支持 Windows/Linux 非冻结 Python，仍只启动一个 Uvicorn worker。凭据经父子管道传递，不写入命令行或分组运行文件。
- 更换基础 Python 后如提示 `worker_runtime_changed`，先正常停服，保留服务数据库，将对应的 `egress-workers/GROUP_ID` 运行目录移到备份位置，再启动服务并重新生成规则。不要删除账号数据库。

Clash 配置及目标网络策略决定实际路由。参考 [Mihomo 规则文档](https://wiki.metacubex.one/config/rules/)。模拟进程测试不能证明真实节点可用、不同节点必然具有不同出口，或游戏服务器允许更多登录。
