# 奥拉工作台

独立 Python 后台统一管理账号、游戏会话和任务，CLI 与 Vue 浏览器前端通过同一套 `/api/v1` 接口操作。支持 Linux 部署；关闭网页或退出 CLI 后，已提交任务继续由后台执行。

## 启动

使用 Python 3.12，在项目根目录创建并激活虚拟环境，然后安装依赖：

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

首次启动自动创建 `data/service/admin.token`，启动输出只显示文件路径。浏览器使用该文件中的管理员凭据登录；CLI 从 `AOLA_ADMIN_TOKEN` 环境变量读取凭据。账号密码不会由账号查询接口返回。

```bash
python cli_main.py --help
python cli_main.py --json accounts list
```

批量登录、选择就绪账号、组合任务与断线处理的操作步骤见 [批量操作手册](batch_operations.md)。完整安装、认证、任务配置与迁移、HTTP 边界、Clash TUN 和 Linux 运行说明见 [服务使用说明](docs/service.md)。接口契约见 [OpenAPI 快照](docs/openapi.json)，运行中的交互文档位于 `/docs`。

需要按每批账号使用不同节点时，见[多出口账号分组](docs/egress-groups.md)：每组独立进程，Clash 按完整进程路径路由，默认每组30个、全局100个在线名额；容量与登录并发可单独配置。[300账号云端工作流](docs/cloud-workflows.md)提供10出口配置和可自行填写任务的启动脚本。

旧桌面端、命名管道和旧 CLI 实现源码保留用于参考。当前 `cli_main.py` 是 HTTP 客户端，`backend_main.py` 启动独立服务；旧 WPF 打包和运行兼容性不属于新入口的承诺。
