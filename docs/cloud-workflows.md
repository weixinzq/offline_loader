# 云服务器：10 个出口、300 个账号工作流

服务目录：`/root/workspace/offline_loader`，通过 `ssh aliyun` 登录。所有组共用一个后台，每组拥有独立的游戏执行进程，使用本机提供的香港 S01～S10 节点。

## 账号与出口

| 出口 | 账号标签，每组30个 | 节点编号 |
|---|---|---|
| 01 | 违心11～40 | 香港S01 |
| 02 | 违心41～70 | 香港S02 |
| 03 | 违心71～100 | 香港S03 |
| 04 | 违心101～131，跳过104 | 香港S04 |
| 05 | 违心132～161 | 香港S05 |
| 06 | 违心162～191 | 香港S06 |
| 07 | 违心192～221 | 香港S07 |
| 08 | 违心222～252，跳过244 | 香港S08 |
| 09 | 违心253～282 | 香港S09 |
| 10 | 违心283～312 | 香港S10 |

完整节点名称以服务器分组及配置文件为准，包含旗帜、IEPL、倍率等后缀。固定映射保存在 `data/service/cloud-workflows.json`，其中只有账号ID、标签、分组和出口信息，没有账号密码。正常运行不会重新排序或重新分组；账号被删除、改名或改组时会报错，需先核对映射。

## 可重复运行的300账号初始化

只需要“全部账号重新连接 → 执行初始化文件一次”时，直接使用专用入口，不需要修改 `GROUP_WORKFLOWS`：

```bash
cd /root/workspace/offline_loader
.venv/bin/python run_cloud_initialization.py --dry-run
.venv/bin/python run_cloud_initialization.py
```

每次运行重新读取 `Script/初始化.txt` 的最新内容，解析 `#send`、`#time`、`#wait` 和字符串外的 `|` 分隔符；每个就绪账号执行一遍整个文件，不附加任何专用战斗脚本。修改文本后再次运行即可，不需要重新编译。

每次正常运行都会强制重连这300个账号，包括已有在线账号。每组连接任务申请并发20，所有组共享后台的全局登录及初始化上限；每账号包含首次在内最多5次连接尝试。只补试未就绪账号，验证码及限流继续停止对应组新增登录。连接阶段重新核对所有账号的就绪状态，发送前掉线且尚有剩余次数的账号也会补试。执行期间掉线或发送失败不会自动重放消息。

指定其他初始化文件可使用 `--file Script/其他初始化.txt`，相对路径以项目根目录为基准。`--dry-run` 不重连、不发送、不创建任务或日志目录；后台未运行时也不会启动它。

每次正常运行单独保存到 `logs/cloud-initialization/<运行编号>/`：`run.log`、`status.json`、`result.json` 和 `accounts.csv`。CSV列出全部300个账号的连接尝试次数、执行状态和网络写入统计。全部完成退出码0，有未完成账号退出码1；发送完成与服务器业务确认分开记录。任务完成后保留连接。

需要关闭SSH后继续运行：

```bash
nohup .venv/bin/python -u run_cloud_initialization.py > logs/cloud-initialization-console.log 2>&1 &
tail -f logs/cloud-initialization-console.log
```

前台按 Ctrl+C 仅取消本轮创建的任务，保留就绪连接。每次重新启动该入口都是新一轮初始化，不是恢复旧轮次。

后台至少需要 `--max-online 300 --login-concurrency 20`。为支持44个服务器条目并行测试，部署脚本使用 `--max-online 500 --login-concurrency 44`；普通云工作流也受这44个全局登录及初始化名额约束，每组任务仍申请并发20。预检不再拒绝高于20的后台，并在 `backend_login_concurrency` 中显示实际全局上限。调整启动参数后需通过 `./manage.sh restart` 生效，该操作会断开现有连接；启动脚本不会擅自重启一个仍在运行的旧并发后台。

## 自定义每个出口的任务

初始化和战斗是两个独立入口。`run_cloud_initialization.py` 只在明确需要初始化时执行；战斗入口从不读取或执行初始化文件。

编辑根目录的 `run_cloud_workflows.py`，找到顶部 `GROUP_WORKFLOWS`。每个出口各有一条配置：

```python
{"exit": 1, "steps": [{"kind": "script", "script_id": "double_auto_battle"}], "repetitions": 9999, "repeat_interval": 0.2, "message_delay": 0.2}
```

- `steps`：按顺序执行的消息、专用脚本步骤。
- `repetitions`：每个账号执行整组步骤的次数。
- `repeat_interval`：两轮之间的等待秒数。
- `message_delay`：消息步骤内部的消息间隔，不改写专用脚本自身节奏。

查询真实脚本ID：

```bash
cd /root/workspace/offline_loader
export AOLA_ADMIN_TOKEN="$(cat data/service/admin.token)"
.venv/bin/python cli_main.py scripts list
```

例如将一个出口的配置改为：

```python
{
    "exit": 1,
    "steps": [
        {"kind": "messages", "messages": [
            {"id": -1, "cmd": "#time", "param": {"seconds": 1}}
        ]},
        {"kind": "script", "script_id": "替换为脚本列表中的真实ID"},
    ],
    "repetitions": 10,
    "repeat_interval": 0.5,
    "message_delay": 0.2,
}
```

10个出口可以使用不同脚本、组合顺序和执行次数。当前默认全部使用 `double_auto_battle`，每个账号执行9999轮，完整脚本结束后等待0.2秒再开始下一轮。脚本内部消息间隔及等待战斗逻辑保持原样。把 `steps` 清空会在登录前报错。

## 检查和启动

```bash
cd /root/workspace/offline_loader
.venv/bin/python run_cloud_workflows.py --dry-run
.venv/bin/python run_cloud_workflows.py
```

`--dry-run` 只校验配置和已有后台信息，不启动服务、不探测新出口、不登录账号、不创建任务；后台未运行时会提示无法完成在线预检。正常运行在后台未运行时调用已有 `manage.sh start` 启动同一个后台，不另开第二个实例。管理员凭据从环境变量或本地 `data/service/admin.token` 读取，无需写进脚本。

日常后台运行使用独立管理入口，断开SSH后继续，不会重复初始化，也不会开机自启：

```bash
./manage-cloud-battles.sh start
./manage-cloud-battles.sh status
./manage-cloud-battles.sh logs
./manage-cloud-battles.sh stop
```

重复 `start` 不会启动第二份任务。`stop` 请求取消本轮任务并保留就绪连接；后端停止时战斗进程也会停止。每次新启动是新一轮9999次，不恢复旧轮次，也不自动重放失败战斗。修改 `GROUP_WORKFLOWS` 后，先停止当前战斗，再启动以读取新配置。

本次初始化失败的账号应排除，不自动补做初始化：

```bash
./manage-cloud-battles.sh start --exclude-account-id ACCOUNT_ID --exclude-account-id ANOTHER_ID
```

排除仅对本轮有效，同时禁止该账号登录和执行。账号ID来自初始化 `result.json` 的逐账号结果及固定映射；无效ID和排除全部账号会报错。战斗日志保存在 `logs/cloud-battles/<运行编号>/`，包括 `run.log`、持续更新的 `status.json` 和结束后的 `result.json`；控制台日志为 `logs/cloud-battles-console.log`。状态中的完成轮次由后端在该账号本次执行结束时更新，运行中显示0不表示未发生战斗。日志会包含每组连接轮次、就绪数量、跳过标签、执行任务编号和账号失败原因。关闭日志查看命令不会取消后台任务。

## 启动后的实际流程

1. 检查固定映射、分组路由、脚本ID、消息内容及容量。后台至少配置 `--max-online 300 --login-concurrency 20`。
2. 通过10个实际工作进程重新检查公网出口。必须取得10个不同IP，否则不开始游戏登录。
3. 10组连接任务共用后台配置的全局名额，名额覆盖登录、初始化及失败清理。自定义工作流会跳过已有就绪账号；专用初始化入口则在本轮首次连接时强制重连。
4. 登录包含首次在内最多尝试5次，只补试未就绪账号，两轮之间等待2秒；验证码、限流和其他现有登录阻断会停止该组新增登录，不影响其他组。
5. 全部组结束连接尝试后，再汇总实际就绪账号。各组使用已有会话并发30个，满员共300个执行并发；登录失败的账号跳过，空组不创建执行任务。
6. 任务完成后保留连接。执行期间掉线或脚本失败只停止该账号后续步骤，不自动重新登录或重放任务。

“执行并发300”表示300个账号可以同时执行各自流程；不保证服务器在同一毫秒接收所有账号的消息。各组脚本等待、战斗时长和完成时间可以不同。

前台运行时按 Ctrl+C 会取消本次脚本创建的任务，保留已就绪连接和其他任务。若后台清理超时，脚本会打印待核查的任务编号，不能把退出终端当作已经清理完成。后台启动的脚本也可在网页任务记录中按编号取消任务；不会自动断开常驻连接。

## 运维及结果边界

- `./manage.sh status` 查看后台；`./clash/manage.sh status` 查看本项目Mihomo实例。
- 修改代理节点或工作进程规则前，先停止受影响任务并断开相应账号；修改后重新核对公网出口。
- Mihomo控制接口和代理配置只供本机访问，节点配置权限为600。不要打印或上传整个配置文件用于排错。
- 服务依赖本项目的Mihomo运行；代理服务退出会停止依赖后台，避免其继续以直连方式运行。
- 当前方案不增加开机自启。重启服务器后通过项目管理脚本启动即可。
- 模拟300账号并发测试证明调度规则，不代表真实游戏已经接受300个账号或全部业务操作。真实任务结果以逐账号记录及服务器业务确认分别判断。

部署与出口验收结果以当次报告为准；若任一选定节点不可用或出口IP重复，不会擅自换用未授权节点。
