# 奥拉工作台

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
