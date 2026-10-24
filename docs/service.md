# 独立服务、CLI 与浏览器使用说明

## 运行结构与安装

后台使用 Python 3.12、asyncio、FastAPI/Pydantic 2、Uvicorn、SQLAlchemy 2、SQLite 和 Alembic；CLI 使用 Typer。浏览器前端使用 Vue 3、TypeScript 和 Vite。CLI 和网页共同调用后台 `/api/v1`，客户端不创建游戏连接，也不直接读写数据库。

以下命令均从项目根目录执行。Linux 创建虚拟环境：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell 使用当前 Python 创建虚拟环境；先确认版本为 3.12。直接调用虚拟环境中的 Python，无需激活：

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`py -3.12` 仅适用于已安装 Python 启动器的电脑；`python` 不接受 `-3.12` 参数。若 PATH 中的 Python 不是 3.12，使用实际安装的 Python 3.12 可执行文件完整路径创建虚拟环境。后续 Windows 命令也可将 `python` 替换为 `.\.venv\Scripts\python.exe`。

虚拟环境只需创建一次。打开另一个终端后，直接调用已有的 `.\.venv\Scripts\python.exe` 即可；`python -m venv .venv` 是创建环境，不是进入环境。后台运行时不要重复创建同一环境，否则 Windows 可能因其中的 `python.exe` 正在使用而返回 `Permission denied`。

`requirements.txt` 安装锁定依赖和当前项目，并注册 `aola` 命令。本文统一从仓库运行 `python cli_main.py`；使用安装后的 `aola serve` 时，应通过 `--frontend-dir` 显式指定仓库的 `frontend/dist` 绝对路径，安装包本身不包含网页构建产物。构建网页需要 Node.js 22.12 或更高版本和 npm：

```bash
cd frontend
npm ci
npm run build
cd ..
python cli_main.py serve
```

默认监听 `127.0.0.1:8765`，同一服务提供网页 `/`、业务 API `/api/v1` 和交互文档 `/docs`。前端构建目录默认是项目的 `frontend/dist`；未构建网页时仍可使用 API 和 CLI。可以显式指定数据目录与网页目录：

```bash
python cli_main.py serve --data-dir data/service --frontend-dir frontend/dist
```

`backend_main.py` 是独立服务的便捷入口，相当于预置 `serve`：

```bash
python backend_main.py --host 127.0.0.1 --port 8765 --data-dir data/service
```

后台始终使用单个 Uvicorn worker，统一拥有账号占用、会话和任务状态。每个数据目录只能由一个服务实例持有；不要启动多个进程共同调度该目录。默认同时在线容量为100，可用 `serve --max-online 300 --login-concurrency 5`（或环境变量 `AOLA_MAX_ONLINE`、`AOLA_LOGIN_CONCURRENCY`）设置在线容量与全局登录/初始化并发。未指定登录并发时保持原行为，按在线容量限制。登录名额在初始化或失败清理完成后释放，不限制已在线账号的执行并发。账号配置和 `bounded` 临时任务目标数可以超过在线容量；任务仍需满足当时可用容量。预检返回实际 `max_online` 和 `login_concurrency`。

生产运行时由用户级进程管理器维护服务进程；CLI 本身不提供旧的 `start --detach` 进程管理方式。持久化目录默认相对**服务启动时的工作目录**为 `data/service`，部署时建议固定工作目录或使用绝对 `--data-dir`。

## 管理员认证与远程访问

服务先使用全局 `--token` 或 `AOLA_ADMIN_TOKEN` 指定的管理员凭据；未提供时，在数据目录生成并复用 `admin.token`。启动日志只打印凭据文件位置，不打印凭据值。使用环境变量提供凭据时，不依赖自动生成的文件。

在另一个已激活虚拟环境的终端中，读取本机凭据文件到环境变量，不将其打印到终端：

```bash
export AOLA_ADMIN_TOKEN="$(cat data/service/admin.token)"
python cli_main.py --json accounts list
```

PowerShell 对应方式：

```powershell
$env:AOLA_ADMIN_TOKEN = (Get-Content -Raw data/service/admin.token).Trim()
.\.venv\Scripts\python.exe cli_main.py --json accounts list
```

设置环境变量只供该终端中的 CLI 使用，不会显示凭据，也不会自动填写网页。若服务使用默认凭据文件，可在 PowerShell 将文件内容复制到剪贴板，再粘贴到网页的管理员凭据输入框：

```powershell
(Get-Content -Raw .\data\service\admin.token).Trim() | Set-Clipboard
```

网页需要的是凭据本身，不是文件路径或上述命令。若服务启动时指定了 `--token` 或 `AOLA_ADMIN_TOKEN`，以启动时指定的值为准；后续修改文件或另一个终端的环境变量不会改变已运行服务的凭据。

CLI 使用 `Authorization: Bearer ...`，每次请求通过 HTTP 访问后台。全局 `--url`、`--token` 和 `--json` 放在子命令前；默认地址为 `http://127.0.0.1:8765`，也可以设置 `AOLA_SERVICE_URL`。

浏览器在登录页面输入相同管理员凭据，后台建立 HttpOnly、SameSite=Strict 会话 Cookie；浏览器不把管理员凭据保存在本地存储。退出登录或服务重启后需要重新登录。Cookie 认证的写请求和创建会话请求要求同源 `Origin`。

远程访问通过 HTTPS 反向代理接入，后台保持本机监听，并启用安全 Cookie：

```bash
python cli_main.py serve --host 127.0.0.1 --secure-cookies
```

反向代理应保留浏览器访问的 `Host`，传递正确的 `X-Forwarded-Proto: https`，并关闭 SSE 响应缓冲。可信代理转发信息必须让后台识别出与浏览器一致的外部来源，否则 Cookie 写请求会返回 `origin_mismatch`。客户端 `--url` 使用该 HTTPS 服务地址。不要将 `--secure-cookies` 的网页会话用于普通 HTTP 访问。

数据目录含账号登录凭据及管理员凭据，SQLite 中的账号凭据没有应用层加密。Linux 创建的数据目录限制为当前用户访问；备份和迁移时应保留该访问边界，不分享数据库或 `admin.token`。

## 账号、常驻会话与任务

常用命令：

```bash
python cli_main.py accounts create --label 主账号 --account LOGIN_ACCOUNT
python cli_main.py accounts list
python cli_main.py accounts get ACCOUNT_ID
python cli_main.py accounts update ACCOUNT_ID --label 新备注
python cli_main.py sessions list
python cli_main.py sessions connect ACCOUNT_ID --concurrency 1
python cli_main.py sessions reconnect ACCOUNT_ID --concurrency 1
python cli_main.py sessions disconnect ACCOUNT_ID
python cli_main.py scripts list
```

创建账号时未在命令行提供密码会进入隐藏输入提示。`ACCOUNT_ID` 使用服务返回的稳定 ID，`LOGIN_ACCOUNT` 替换为实际登录账号；备注标签必须唯一，并与登录账号不同。`char_id` 默认为 0，`zone_index` 默认为 1，可通过 `--char-id` 和 `--zone-index` 指定。

连接、重连和断开均返回 `task_id`；收到响应只表示操作已提交。会话状态依次包含 `connecting`、`initializing`、`ready`、`disconnecting` 和 `offline`，只有 `ready` 可执行业务。查询任务结果确认初始化和清理是否完成。

在线或被任务占用的账号不能编辑、删除。按真实登录账号统一互斥：即使配置 ID、角色 ID 不同，也不能重复连接或同时被两个任务使用。编辑其他空闲离线账号不会重建全部会话。

账号管理、会话列表和任务编排中的账号选择每页显示 50 个账号，翻页保留已选账号。“全选”覆盖全部账号，提交的账号集合也包含其他页的选择。

新任务文件是单个任务快照，例如保存为 `task.json`，将 `ACCOUNT_ID` 替换为已创建的账号 ID：

```json
{
  "kind": "execute",
  "account_ids": ["ACCOUNT_ID"],
  "session_policy": "temporary",
  "mode": "bounded",
  "concurrency": 5,
  "repetitions": 1,
  "repeat_interval": 0.5,
  "message_delay": 0.2,
  "steps": [
    {"kind": "messages", "messages": [{"id": -1, "cmd": "#time", "param": {"seconds": 1}}]}
  ]
}
```

该示例步骤仅等待一秒；使用 `temporary` 启动时仍会登录并初始化目标账号。预检不会登录或发送游戏消息：

```bash
python cli_main.py tasks validate --file task.json
python cli_main.py tasks start --file task.json
python cli_main.py tasks list
python cli_main.py tasks status TASK_ID
python cli_main.py tasks results TASK_ID
python cli_main.py tasks cancel TASK_ID
python cli_main.py logs show TASK_ID
python cli_main.py logs follow TASK_ID
```

`logs follow` 不指定任务 ID 时订阅全部实时事件，终止跟随命令不会取消任务。它不重放历史，历史日志通过 `logs show` 或 HTTP 日志分页读取；`logs show` 默认返回前 200 条，HTTP 支持 `after` 游标和 `limit`（最多 1000）。网页的会话、任务和日志实时更新每 0.2 秒合并应用一次；日志缓存最多保留 2000 条，只渲染最近 200 条已载入日志，支持加载更多继续读取后续日志。服务器保存的日志不受网页显示行数限制。

两种会话归属：

| `session_policy` | 启动条件 | 结束或取消后 |
|---|---|---|
| `existing` | 至少一个目标账号已有就绪会话；未就绪账号记录为跳过 | 保留常驻会话，释放任务占用 |
| `temporary` | 目标真实账号尚无连接或占用 | 清理任务创建的临时连接后释放占用 |

临时任务的批处理方式：

| `mode` | `concurrency` 的含义 | 在线容量 |
|---|---|---|
| `bounded` | 覆盖连接、初始化、全部轮次执行及清理的完整生命周期 | 按同时运行的生命周期预留 |
| `all_online` | 限制登录并发，准备完成后成功账号并行执行 | 启动时为全部目标预留容量 |

使用已有会话时，并发数限制同时执行的账号，网页统一提交 `bounded`。步骤按顺序执行，每轮重复同一组步骤；`repeat_interval` 控制轮次间隔，`message_delay` 控制消息步骤间的消息间隔，不改写专用脚本内部时序。

已有会话的预检返回 `account_count`（可执行账号数）和 `skipped_accounts`（账号 ID、名称、原因）。提交时再次检查；被跳过的账号保留在任务的逐账号结果中，状态为 `skipped`，不登录、不认领连接。全部未就绪时返回 `session_not_ready` 并列出账号名称；部分跳过且其余执行成功时任务可为 `succeeded`，不能把跳过账号计为执行成功。任务接受后才断线的账号单独失败，不阻止其他账号。网页提供“选择就绪账号”及所选账号未就绪提示，详见[批量操作手册](../batch_operations.md)。

消息步骤提交 `messages` 内容数组；脚本步骤使用 `{"kind":"script","script_id":"脚本列表中的ID"}`，客户端不提交模块路径。可以使用 `python cli_main.py parse --file messages.txt` 解析原有消息文本，再把返回的 `messages` 放入步骤。文件在客户端读取，API 不接受客户端本机绝对路径。

取消首先进入 `cancelling`；后台完成任务工作停止、临时连接清理和占用释放后才进入终态。清理失败保留 `cancelling` 与 `cleanup_failed`，可重试取消，不应把取消请求返回当作清理已完成。遇到 `verification_required` 等登录阻断时，停止该批新增登录并取消未完成工作、清理临时连接；其他独立任务继续。需人工完成验证后另行提交新任务，服务不自动重试该批。

结果分三层读取：任务和账号的执行状态、`network_write` 的本地网络写入状态/次数、`business_confirmation` 的服务器业务确认。`succeeded` 的任务或本地写入不等于服务器接受业务。写入次数为 `null` 表示未知；没有业务确认时保持 `unknown`。

本地写入统计覆盖任务业务执行阶段；本地等待、登录初始化和后台心跳不计入该次数。进程被强制终止、未能保存最终统计时，重启后仍保留未知结果。

## 旧配置和任务迁移

先导入旧账号配置。兼容旧的单账号配置和 `accounts` 数组，原文件不修改，响应返回稳定账号 ID、导入数量以及省略字段：

```bash
python cli_main.py accounts import --file config.json
python cli_main.py tasks migrate --file tasks.json --output migration-report.json
```

迁移依赖服务内已导入账号的标签及登录账号匹配。旧任务的 `accounts_file` 和消息步骤 `file` 路径相对**旧任务文件所在目录**解析，读取发生在 CLI 所在机器。迁移会展开消息文件内容、把脚本模块映射为 `script_id`，并去掉旧 `clash`、`workers[].proxy` 配置。迁移不启动任务，也不改写原账号、任务或消息文件；`--output` 拒绝覆盖已有文件。

`migration-report.json` 是包含 `tasks` 数组、逐 worker 的 `reports` 及移除配置说明的**完整报告**，不能直接传给 `tasks start --file`。每个旧 worker 转换为 `tasks` 中的一个任务，必须分别保存为单个任务文件，然后逐个预检和启动。例如在 Linux 中从报告创建新文件，已有同名文件时停止：

```bash
python - <<'PY'
import json
from pathlib import Path

report = json.loads(Path("migration-report.json").read_text(encoding="utf-8"))
for index, task in enumerate(report["tasks"], start=1):
    with Path(f"task-{index}.json").open("x", encoding="utf-8") as stream:
        json.dump(task, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
PY
python cli_main.py tasks validate --file task-1.json
python cli_main.py tasks start --file task-1.json
```

迁移只转换结构，不证明脚本可用、当前会话就绪或容量充足，最终以每个任务预检为准。多个转换任务选择同一真实账号时，后启动的任务可能返回占用冲突，应先完成前一任务或调整选择。

旧 `tasks.example.json` 表示旧 worker 配置，属于迁移输入；新运行入口使用上文单任务快照。旧桌面、命名管道及 CLI supervisor 源码仍保留用于参考，当前 `cli_main.py` 和 `backend_main.py` 已切换新入口，不承诺旧 WPF 打包运行兼容。

## 接口边界、持久化与网络

HTTP 路由负责认证、参数转换和错误映射；`src/service` 的应用服务持有账号配置、会话、任务占用、执行和结果。协议、消息路由及脚本运行不依赖浏览器、Typer 或 HTTP 路由。脚本仅使用明确的发送、订阅、等待及游戏业务状态能力。

| 接口组 | 主要入口 |
|---|---|
| 健康与认证 | `GET /api/v1/health`；`POST/GET/DELETE /api/v1/auth/session` |
| 账号 | `/api/v1/accounts`、`/accounts/{account_id}`、`/accounts/import` |
| 会话 | `/api/v1/sessions`；`/sessions/connect`、`reconnect`、`disconnect` |
| 消息 | `/api/v1/messages/parse`、`/messages/validate` |
| 脚本 | `GET /api/v1/scripts` |
| 任务 | `/api/v1/tasks`、`/tasks/validate`、`/tasks/{task_id}`、`cancel`、`results`、`logs` |
| 事件 | `GET /api/v1/events`，可使用 `task_id` 过滤 |

除健康检查及创建浏览器会话外，业务接口要求认证。SSE 数据格式例如 `data: {"id":"1","event":"task","timestamp":"...","data":{...}}`，`event` 为 `account`、`session`、`task`、`log` 或 `egress`。事件队列是有界的实时通知，不是可靠历史回放；网页在重连和定期刷新时重新读取持久化状态。

数据目录的 `service.sqlite3` 保存账号、任务历史和脱敏日志，启动时自动执行 Alembic 迁移。任务记录中的消息参数不作为可重放快照保存。连接只存在于当前进程；服务退出清理会话，重启时账号恢复离线，未完成任务标记 `interrupted`，不会自动重放。保留历史成功/失败结果，恢复执行需要人工重新提交原任务文件。

新服务使用普通系统网络连接，网络出口由**外部 Clash TUN** 负责：

- 应用不启动或停止 Mihomo，不加载代理节点，不设置 worker SOCKS5 端口。
- 新链路禁用应用代理和环境代理；`AOLA_PROXY_URL`、`HTTP_PROXY` 等不是新链路的出口选择方式。
- Clash 规则匹配实际发起登录及 TCP/WSS 连接的后端 Python 进程路径，不匹配网页或 CLI 客户端进程。
- TUN、DNS、分流和代理中断后的网络行为由系统 Clash 配置决定。系统管理员准备 TUN 权限和系统依赖；应用不会提权或修改系统路由。

未分组账号共享后台进程的系统路由。需要多个出口时，使用新增的[出口分组](egress-groups.md)：账号关联分组，每组独立执行进程，通过 Clash 完整进程路径绑定节点；默认每组 30、全局 100 个名额。旧任务中的每 worker 节点配置不会自动转换为新分组。真实出口、服务器登录接受和业务确认需要现场验证，配置和模拟测试不能替代这些证据。

## 开发检查与 Linux 测试边界

接口契约由后端导出，前端类型从契约生成：

```bash
python -m tools.export_openapi
cd frontend
npm run api:generate
npm run api:check
npm test
npm run build
cd ..
```

开发网页可在 `frontend` 中使用 `npm run dev`，Vite 将 `/api` 代理到本机 `127.0.0.1:8765`，无需为开发开启跨域业务访问。生产同源部署使用构建产物与 `serve`。

浏览器模拟检查脚本为 `frontend/tests/browser_smoke.py`，需额外安装 Playwright 并准备可用浏览器；它拦截全部业务 API，不能证明真实登录或游戏行为。后台及协议检查以仓库的实际测试结果与运行报告为准。

安装测试依赖后执行后台回归。Linux 将临时文件也放在当前工作区内：

```bash
mkdir -p build/test-tmp
export TMPDIR="$(pwd)/build/test-tmp"
export PIP_CACHE_DIR="$(pwd)/build/pip-cache"
export XDG_CACHE_HOME="$(pwd)/build/cache"
python -m pip install -r requirements-test.txt
python -B -m unittest discover -s tests -v
python -B -m scripts.verify
python -B -m unittest scripts.test_cli_jobs scripts.test_rank_info scripts.test_wait_escape scripts.test_double_auto_escape -v
```

Linux 进程测试包含实际安装的 `aola` 命令在仓库外启动、SIGTERM、持续 SSE 和 SIGKILL 后恢复；Windows 跳过这组 POSIX 验收。旧分离进程 CLI 的单项测试已退休，由新的服务进程测试替代；其余旧配置、协议及脚本测试继续保留。

指定测试服务器的 SSH 别名为 `aliyun-test`，用户为 `codex-test`，所有测试只能在 `/home/codex-test/workspace` 内进行。不使用 sudo/root，不改生产文件，不访问生产数据库或密钥。缺少系统依赖或 TUN 能力时由管理员准备，测试后保留结果和日志。本说明不声明尚未现场完成的 Linux、真实账号或 TUN 出口验收。

## 一次性服务器登录可用性测试

`aola servers probe --file probe.json` 提交 `kind: server_probe` 任务，复用现有后台、离线账号互斥和出口分组 worker。每个服务器条目仅分配一个代理通道，Flash TCP/H5 WSS 分别认证、登录、断开并记录结果，不执行玩家初始化或业务脚本。`--dry-run` 展示分配与容量，`--at` 在 CLI 等待到指定时间后提交一次。查询和取消使用现有任务接口；`aola servers results TASK_ID --format csv --output results.csv` 导出逐传输报告。输入、状态、时间与实际代理路由的证据边界见[服务器可用性测试](server-probe.md)。
