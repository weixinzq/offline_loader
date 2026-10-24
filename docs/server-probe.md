# 服务器可用性测试

通过现有后台和出口分组，执行一次游戏协议登录测试。Flash TCP 与 H5 WSS 分别记录结果；每个服务器条目固定使用一个测试账号及其代理通道，不跨出口重复测试，不自动重试。

## 准备任务文件

创建 `probe.json`，填写后台中已有、离线且未被占用的测试账号 ID。账号必须已经分配到启用路由确认的出口分组。凭据由后台读取，任务文件不包含密码、SID 或节点凭据。

仓库根目录的 [server-probe.example.json](../server-probe.example.json) 已填入本次提供的 44 个服务器条目（索引 0–43），最多进行 44 次 Flash 与 14 次 H5 登录尝试。复制为任务文件并替换其中的账号 ID 占位值即可；末尾属于区服列表的 `1` 没有写入域名。该文件是地址快照，运行时仍需与当次认证响应核对。

```json
{
  "account_ids": ["ACCOUNT_ID_1", "ACCOUNT_ID_2"],
  "concurrency": 5,
  "probe_timeout": 15,
  "server_targets": [
    {"host": "115.159.197.100", "flash_port": 8000, "h5_port": 0, "domain": "", "server_index": 0},
    {"host": "111.231.168.235", "flash_port": 8000, "h5_port": 8100, "domain": "aola-h5-1.100bt.com", "server_index": 2}
  ]
}
```

这些地址仅示例响应字段，不声明当前可用。`server_index` 可省略，用于标记原始响应中的位置；运行时根据完整的 IP、两个端口和域名重新匹配当次认证响应，不能因为索引改变而连接其他服务器。同一完整端点不可重复；同一 IP 的不同端口是不同条目。

按任务中服务器的顺序轮流分配测试账号。例如三台服务器、两个账号分配为 `1→账号1、2→账号2、3→账号1`。多余账号不参与。每个账号保留原出口分组，且只串行执行自己的测试。要让不同服务器并发测试，需要提供多个不同的离线登录账号；同一登录账号的不同配置不能绕过互斥。

每个服务器选择当次 `zn` 响应中关联的最小区服 ID，并记录 ID、名称。结果只证明该代表区服的一次登录；同一入口承载的其他区服可能有不同策略。没有关联区服时不借用别处区服测试。

## 预检与启动

先设置 `AOLA_ADMIN_TOKEN`，远程后台使用全局 `--url` 或 `AOLA_SERVICE_URL`。Windows 在仓库根目录运行：

```powershell
.venv\Scripts\python.exe cli_main.py --json servers probe --file probe.json --dry-run
.venv\Scripts\python.exe cli_main.py --json servers probe --file probe.json
```

Linux 在项目虚拟环境中使用 `aola --json servers probe --file probe.json`。

预检只校验输入、离线状态、出口确认、账号互斥及容量，并展示 `server_assignments`、`effective_concurrency` 和最大传输尝试数 `max_transport_attempts`；不会启动 worker 或执行游戏认证、出口检测。提交时会重新校验。实际并发还受后台全局登录上限和每组容量约束。

指定北京时间启动一次：

```powershell
.venv\Scripts\python.exe cli_main.py --json servers probe --file probe.json --at "2026-10-25T05:59:00+08:00"
```

`--at` 必须包含时区，已过去的时间会报错。命令行在本机等待到点后提交后台，等待期间必须保持进程运行；提交前中断命令行不会启动任务。该参数不会在后台创建持久定时任务。返回任务 ID 后，退出命令行不会取消后台任务。

每轮先在所用 worker 中检测出口 IP，再并发进行测试。每种受支持传输分别获取新 SID；Flash 测试完全断开后才测试 H5。认证、连接、登录响应各自受 `probe_timeout` 秒限制，断开清理使用同样的超时、至少保留 1 秒。测试不执行玩家初始化、脚本或战斗操作。

`planned_at`、后台 `started_at/finished_at`、各服务器及各传输的时间均保存在记录中。响应时间含排队、认证和清理，不能视为所有服务器在同一毫秒接受测试。各阶段耗时位于 `stage_durations_ms`。

## 查询、取消和导出

```powershell
.venv\Scripts\python.exe cli_main.py --json tasks status TASK_ID
.venv\Scripts\python.exe cli_main.py --json tasks cancel TASK_ID
.venv\Scripts\python.exe cli_main.py --json logs show TASK_ID
.venv\Scripts\python.exe cli_main.py --json servers results TASK_ID
.venv\Scripts\python.exe cli_main.py servers results TASK_ID --format csv --output server-results.csv
.venv\Scripts\python.exe cli_main.py servers results TASK_ID --format json --output server-results.json
```

导出文件使用新建模式，不覆盖现有文件；CSV 使用带 BOM 的 UTF-8，每个服务器的 Flash/H5 各一行。进行中的任务也可导出快照。结果和日志均不包含 SID、密码或完整认证响应。

## 结果解释

| `flash.status` / `h5.status` | 含义 |
|---|---|
| `available` | 收到游戏协议登录成功响应：`logOK`，或现有协议支持的无命令名、正数 `id` 成功形式 |
| `not_supported` | 该传输没有有效端口；H5 还要求域名 |
| `authentication_failed` | HTTP 认证失败，游戏登录尚未完成 |
| `endpoint_missing` | 当次认证响应没有提供指定完整服务器条目 |
| `no_zone` | 当次响应没有该入口关联的可测试区服 |
| `connection_failed` | TCP 或 WSS 连接建立失败 |
| `login_rejected` | 连接已建立，但游戏返回 `logKO` |
| `login_unconfirmed` | 未收到可确认成功的游戏登录响应 |
| `timeout` | `stage` 指定阶段超时 |
| `failed` / `cleanup_failed` | 其他错误 / 断开超时，详见错误信息 |
| `not_tested` / `cancelled` / `interrupted` | 尚未执行、取消或服务中断 |

同时查看服务器级 `error`。例如出口 IP 检测失败会记为 `egress_probe_failed`，其下游戏传输仍为 `not_tested`，不能解释为服务器关服。验证码、限流或认证阻断会停止本轮剩余登录，不自动换账号或通道重试。取消等待 worker 确认清理后才释放账号和容量。

`available` 只表示该时间范围、账号、出口和代表区服下的一次协议登录成功，不表示完整游戏业务可用；失败也不能一律解释为关服。单次批次无法确定完整的开关服时间表。

## 代理与 API

账号分配、HTTP 认证、游戏连接均复用[现有分组 worker 与 Clash TUN 规则](egress-groups.md)。CLI 与后台主进程不发起目标游戏连接。出口 IP 检测只证明该次 HTTPS 请求的出口，真实验收还需在 Clash 连接记录中核对游戏 TCP/WSS 的进程、规则与节点；程序不修改 Clash 配置，也不能仅凭 `routing_confirmed` 保证 TUN 故障时系统层不会直连。

沿用现有鉴权接口：`POST /api/v1/tasks/validate`、`POST /api/v1/tasks`，请求中使用 `kind: "server_probe"`、`server_targets`、`probe_timeout`。查询和取消沿用 `/tasks/{id}`、`/tasks/{id}/results`、`/tasks/{id}/logs`、`/tasks/{id}/cancel`。响应增加 `server_results`，既有任务的行为不变。`planned_at` 在后台仅作记录，定时等待由专用 CLI 完成。

## 验证

```text
python -m unittest tests.test_server_probe tests.test_system_network tests.test_egress tests.test_egress_lifecycle -v
```

新增测试使用模拟认证、模拟游戏传输和独立 worker，验证登录判定、映射、路由分派、取消、进程退出及资源释放，不访问真实游戏服务器。真实账号和实际 TUN 出口需另做现场验收。若使用测试服务器，仅允许 `aliyun-test` 的 `codex-test` 用户在 `/home/codex-test/workspace` 内测试，遵守仓库 AGENTS.md，提供当次结果和日志。
