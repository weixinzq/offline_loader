> 连接与任务模型已更新：所有账号统一使用 IP 池（含可选直连），任务只提交账号和脚本。现行操作见 [IP 池说明](docs/ip-pool.md)；下文涉及固定出口绑定或旧任务编排的内容已停用。


独立 Python 后台统一管理账号、游戏会话和任务，CLI 与 Vue 浏览器前端通过同一套 `/api/v1` 接口操作。支持 Linux 部署；关闭网页或退出 CLI 后，已提交任务继续由后台执行。

## 启动

需要 Python 3.12、Node.js 22.12 或更高版本和 npm。克隆仓库后，在项目根目录创建虚拟环境。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Linux：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

安装后台依赖：

```bash
python -m pip install -r requirements.txt
```

使用 Node.js 22.12 或更高版本构建浏览器前端：

```bash
cd frontend
npm ci
npm run build
cd ..
```

启动后台，浏览器打开 `http://127.0.0.1:8765/`：

```bash
python cli_main.py serve
```


```bash
python cli_main.py --help
python cli_main.py --json accounts list
```

默认监听 `127.0.0.1:8765`，运行数据保存在 `data/service`。账号、出口分组和任务在安装后自行设置，不随源码发布。网页关闭后任务仍由后台执行；后台重启不会自动重放未完成任务。

HTTP 认证首次失败后再尝试两次，总共最多三次，失败重试间隔为 0.5 秒；遇到明确的人机验证、403 或 429 拒绝时停止。成功取得 SID 后的 TCP 失败只走原有 TCP 重试窗口，不重新发起 HTTP 认证。浏览器交互登录不自动重复。

普通 IP 池任务按账号独立推进：登录和初始化成功的账号立即执行后续脚本，其他账号的登录、初始化或脚本失败不会阻止它。部分账号失败时，总任务可能标记为失败，应查看各账号结果，不能据此认为全部账号都没有执行。验证码或明确服务器限制仅停止受影响账号，不取消同批其他账号。

## 正式服务器的版本管理

本机开发仍使用上面的启动命令。正式服务器采用一个持久 systemd 服务，统一入口模板保存在 `tools/server/`；这些模板部署到服务器根目录使用，不替换本机开发入口。

```text
/root/workspace/offline_loader/
├── manage.sh、run-service.sh、cli_main.py   # 稳定入口，不写死发布版本
├── tools/server/manage.py                 # 稳定管理实现
├── current -> releases/<当前版本>
├── releases/<版本>/                      # 完整源码、requirements.lock、frontend/dist
├── .venv/                                # 共用 Python 3.12 环境
├── data/service/                         # 共用数据库和账号配置
└── logs/                                 # service.log、deployments.log
```

`tools/server/aola-offline-loader-root.service` 是服务模板；正式服务的 `ExecStart` 固定为根目录 `run-service.sh`。启动脚本解析 `current` 后，使用根目录 `.venv/bin/python` 运行选中版本，监听 `127.0.0.1:8765`，登录并发保持 44。无需修改系统默认 Python。

```bash
cd /root/workspace/offline_loader
./manage.sh status             # 显示所选版本和实际运行版本
./manage.sh releases           # 列出保留的版本
./manage.sh start              # 幂等启动正式后台
./manage.sh logs               # 查看最新后台日志
python cli_main.py --help      # 服务器转发入口会使用项目 Python 3.12
```

先把完整候选版本放入 `releases/<版本名>/`，前端需提前构建。暂停网页和 CLI 的任务提交，确认无未完成任务且所有游戏账号已断开后，在维护窗口执行：

```bash
./manage.sh switch <版本名>
# 切回某个旧版也使用同一个 switch 命令。
```

管理操作使用文件锁；不提供强制切换。切换前验证候选目录、前端资源、共享环境中的锁定依赖以及数据库 Alembic 版本，不自动安装依赖或升级/降级数据库。数据库版本不一致或依赖不匹配时，需要另行安排升级，不要绕过检查。

切换会先停止原服务并确认进程、端口退出，再原子更新 `current` 并启动新版本。在 30 秒内检查进程目录、健康接口和前端内容；失败则停止新版本并恢复原版本，不恢复或覆盖数据库。记录见 `logs/deployments.log` 和 `logs/service.log`。版本目录应视为发布快照，发布后不要直接修改；共享环境变化也需要重新验证保留版本的兼容性。

`./manage.sh restart` 同样要求任务和账号空闲；`./manage.sh stop` 是管理员主动停止服务的操作。需要诊断 systemd 时使用 `systemctl status aola-offline-loader-root.service`。不要手工修改 `current`、使用旧版本启动脚本或再运行 `serve` 启动第二个正式后台。

首次从旧部署迁移时，应备份原入口、systemd 单元和数据库；将 `current` 指向原运行版本，再部署统一入口和服务单元、重载 systemd。旧的 `src/interfaces/cli.py` 转发器须同步指向根目录统一 CLI，不能继续写死某个 release。既有发布目录缺少锁文件时，只有在核对现有锁文件和安装环境一致后才能补入。失败时恢复原入口与服务单元，不删除旧发布目录或历史日志。

## 源码与构建

- `src/`：账号登录、游戏通信、协议、消息处理、后台服务和接口。
- `scripts/`：随程序提供的专用脚本。
- `frontend/`：Vue 网页源码、依赖锁文件和构建配置；`npm run build` 输出到 `frontend/dist`。
- `tools/`：通用接口导出和服务器探测工具。
- `csharp/AolaLoader/`：旧版 WPF 桌面源码，使用命名管道后台，与网页服务分别启动。



如需构建旧版 Windows 桌面程序，安装 .NET 8 SDK，在已激活的 Python 3.12 环境运行：

```powershell
python -m pip install -r requirements-build.txt
.\build_wpf.ps1
```

输出为 `dist/AolaLoader/AolaLoader.exe` 及命名管道后台 `AolaBackend.exe`。桌面账号配置参考随包提供的 `config.example.json`，自行创建 `config.json`。该模板仅含占位值。
