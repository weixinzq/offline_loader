# CLI 详细使用教程

适用项目：`D:\game\offline_loader`。按 2026-10-13 本地实现核对。

本教程介绍原交互菜单，以及 Linux 上通过 JSON 配置启动多个独立工作进程的方法。各工作进程可以选择自己的账号、代理节点、组合步骤和执行轮次。示例不包含真实账号密码或节点认证信息。

## 目录

1. [两种启动方式](#1-两种启动方式)
2. [服务器目录与运行环境](#2-服务器目录与运行环境)
3. [快速上手：仅登录与初始化](#3-快速上手仅登录与初始化)
4. [CLI 命令详解](#4-cli-命令详解)
5. [任务配置字段](#5-任务配置字段)
6. [账号配置与选择规则](#6-账号配置与选择规则)
7. [执行模式、并发和时间](#7-执行模式并发和时间)
8. [组合步骤与消息格式](#8-组合步骤与消息格式)
9. [专用脚本与战斗任务](#9-专用脚本与战斗任务)
10. [独立代理与多次运行](#10-独立代理与多次运行)
11. [状态、日志与发送结果](#11-状态日志与发送结果)
12. [原交互菜单](#12-原交互菜单)
13. [更新脚本和配置](#13-更新脚本和配置)
14. [常见问题](#14-常见问题)
15. [离线验证与已验证范围](#15-离线验证与已验证范围)

## 1. 两种启动方式

| 方式 | 命令入口 | 适用场景 |
| --- | --- | --- |
| 原交互菜单 | `python cli_main.py` | 手动选择账号、发送消息、查看连接状态 |
| 配置驱动任务 | `python cli_main.py start --job tasks.json` | Linux 服务器上批量执行、独立代理、后台管理 |

无参数启动始终进入原交互菜单。添加参数才进入配置驱动接口。

配置驱动的 `start`、`status`、`stop` 使用 Linux `/proc`，目前不支持 Windows 进程管理。`validate` 可以在 Windows 检查配置，但所引用的账号、消息、节点文件及 Mihomo 文件必须在本机存在；它不会自动检查仅存在于服务器的路径。

原交互菜单不是桌面端所有功能的完整复制，也不提供 JSON 总配置的组合任务管理界面。服务器组合任务使用 `start --job`。

## 2. 服务器目录与运行环境

当前测试服务器连接方式：

```powershell
ssh -l codex-test aliyun-test
```

本项目服务器测试只使用 `codex-test`，操作目录限定在 `/home/codex-test/workspace`；不使用 sudo/root，不修改生产环境。系统级依赖需由管理员安装。

2026-10-12 完成的部署目录为：

```text
/home/codex-test/workspace/offline-loader-deploy-20261012/
├── app/
│   ├── .venv/                         Python 虚拟环境
│   ├── cli_main.py                    CLI 入口
│   ├── config.json                    账号配置
│   ├── tasks.example.json             通用任务模板，节点名需要填写
│   ├── tasks.proxy-check.json         两账号初始化验收任务
│   ├── tasks.proxy-check-account1.json 账号1单次补测任务
│   ├── scripts/                       专用脚本
│   └── logs/jobs/                     默认运行目录
├── clash/nodes.yaml                   已授权的两个节点定义
├── runtime/mihomo/mihomo              独立 Mihomo 可执行文件
├── backups/                           回退包
└── evidence/                          部署和测试证据
```

连接服务器后，以下命令均从项目目录执行：

```bash
cd /home/codex-test/workspace/offline-loader-deploy-20261012/app
.venv/bin/python --version
.venv/bin/python cli_main.py --help
```

已部署的虚拟环境使用 Python 3.12.15，无需重新安装或激活。直接调用 `.venv/bin/python` 可以避免误用服务器的旧系统 Python。

如需激活环境后使用简短的 `python` 命令：

```bash
source .venv/bin/activate
python cli_main.py --help
```

在其他新环境安装依赖时，先确保下面的 `python` 使用已经验证过的 Python 3.12，而不是旧系统 Python：

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

这只安装项目虚拟环境依赖，不安装系统服务。配置驱动任务还需要 Linux Mihomo 文件及有效节点 YAML；安装 Python 依赖不会自动下载这两项。

## 3. 快速上手：仅登录与初始化

已有的 `tasks.proxy-check.json` 使用两个独立 worker，分别选择账号配置序号 1、2，使用端口 17891、17892。组合内容只有本地 `#time=0`：会真实登录并初始化玩家资料，但不发送匹配、出手或逃跑消息。

### 3.1 先检查配置

```bash
.venv/bin/python cli_main.py validate --job tasks.proxy-check.json
```

成功时返回类似：

```json
{
  "valid": true,
  "workers": [
    {"id": "proxy-check-1", "accounts": 1, "mode": "bounded", "port": 17891},
    {"id": "proxy-check-2", "accounts": 1, "mode": "bounded", "port": 17892}
  ]
}
```

这里的 `accounts` 是选中账号数量。预检成功不会登录，不证明代理节点可连通，也不证明游戏服务器会接受后续任务。

### 3.2 前台执行一次

```bash
.venv/bin/python cli_main.py start --job tasks.proxy-check.json
```

开始时输出运行编号、日志目录和主管进程 PID，例如：

```json
{
  "run_id": "20261013-100000-ab12cd34",
  "directory": "/home/codex-test/workspace/offline-loader-deploy-20261012/app/logs/jobs/20261013-100000-ab12cd34",
  "pid": 12345
}
```

上面的编号是格式示例，请保存实际返回的 `run_id`。前台命令会等待任务结束，详细过程写入日志，不保证逐条显示在当前终端。主动取消用 `Ctrl+C`，会进入停止清理流程。

### 3.3 查看结果

```bash
.venv/bin/python cli_main.py status --run 20261013-100000-ab12cd34
```

将示例编号替换为实际编号。检查总状态、每个 worker 中的账号状态、完成轮次和失败原因。

任务自然结束后，账号断开，主管关闭本次 Mihomo 并退出。这两个代理端口不会作为常驻服务保留。

### 3.4 后台执行

```bash
.venv/bin/python cli_main.py start --job tasks.proxy-check.json --detach
```

命令返回运行编号后即可退出 SSH。之后重新连接，使用同一运行编号查询或停止。后台启动返回成功，只表示已创建主管进程，不表示全部账号已经登录或任务已经成功。

## 4. CLI 命令详解

### 4.1 `validate`：只做预检

```bash
.venv/bin/python cli_main.py validate --job tasks.json
```

检查引用文件能否读取、账号选择、重复登录账号、worker ID、端口、节点名称、执行参数、消息解析及脚本发现列表。

它不启动 worker 或 Mihomo，不发送登录请求，也不执行 Mihomo 的 `-t` 检查。`start` 会再次预检，并在实际登录前运行 Mihomo 配置检查和监听端口归属检查。

JSON 不支持注释或末尾多余逗号；文档中的 `//` 注释规则只适用于消息文本文件。

### 4.2 `start`：启动新的一次运行

```bash
# 前台
.venv/bin/python cli_main.py start --job tasks.json

# 后台
.venv/bin/python cli_main.py start --job tasks.json --detach
```

每次 `start` 创建新的运行编号和私有快照，并启动一个主管、一个运行中的 Mihomo 和每个 worker 对应的 Python 进程。Mihomo 配置预检还会短时启动一个检查进程。

worker 登录所选账号、等待玩家资料初始化，然后执行所有轮次。它不会在未初始化完成时开始组合任务。

`start` 不弹出交互发送确认；任务文件就是本次运行指令。需要先检查账号范围和 `steps`，尤其是包含战斗脚本的任务。

### 4.3 `status`：查看一次运行

```bash
.venv/bin/python cli_main.py status --run RUN_ID
```

`RUN_ID` 是运行编号，不是 PID，也不是完整目录路径。当前命令不提供“列出全部运行”的接口，可查看目录：

```bash
ls -1 logs/jobs
```

`status` 不重连账号、不继续任务、不发送业务消息。命令成功读取状态时可以返回退出码 0，即使该任务状态是 `failed`；判断任务结果要看输出的 `state` 和账号记录。

### 4.4 `stop`：停止并清理

```bash
.venv/bin/python cli_main.py stop --run RUN_ID
```

程序核对 PID 及进程出生标识，向本次运行发停止信号，取消排队与正在执行的任务，断开账号连接并关闭自己的 Mihomo。最多等待 20 秒，再结束仍未退出的已核对进程。

主管异常退出后，`stop` 也会检查记录中的子进程。已经全部退出的任务不会重新启动。停止保留日志，不恢复到上次轮次，也不回滚已经发生的服务器业务操作。

不要用按进程名批量结束全部 Python/Mihomo 的命令代替 `stop`，否则容易影响其他运行。

### 4.5 自定义运行目录

```bash
.venv/bin/python cli_main.py start --job tasks.json --detach \
  --runs-dir /home/codex-test/workspace/offline-loader-deploy-20261012/runs

.venv/bin/python cli_main.py status --run RUN_ID \
  --runs-dir /home/codex-test/workspace/offline-loader-deploy-20261012/runs

.venv/bin/python cli_main.py stop --run RUN_ID \
  --runs-dir /home/codex-test/workspace/offline-loader-deploy-20261012/runs
```

三个命令必须使用同一个目录。`--runs-dir` 的相对路径以执行命令时的当前目录为基准；它不是任务 JSON 字段。

默认目录为项目根目录下的 `logs/jobs`，不随任务文件位置改变。

### 4.6 退出码

| 情况 | 退出码/判断方法 |
| --- | --- |
| `validate` 通过 | 0 |
| 配置读取或 CLI 运行错误 | 通常为 1 |
| 命令行参数缺失或无法解析 | 参数解析器可能返回 2，不代表已启动 worker |
| 前台任务全部完成 | 0 |
| worker 存在账号失败或未完成 | 主管通常返回 2 |
| 前台主动取消/主管正常停止 | 通常为 130 |
| 后台启动 | 启动命令通常为 0，最终结果另查 `status` |
| `status`、`stop` 成功读取结果 | 通常为 0，任务成功与否另看状态 |

Linux 可在命令完成后使用 `echo $?` 查看退出码。不要仅凭后台启动、`status` 或 `stop` 的退出码判断业务成功。

## 5. 任务配置字段

以下是可用的两进程初始化模板，可另存为项目目录下的 `tasks.json`。它会登录账号配置中的第 1、2 项，仅执行本地暂停，不进行战斗：

```json
{
  "accounts_file": "config.json",
  "clash": {
    "binary": "../runtime/mihomo/mihomo",
    "config": "../clash/nodes.yaml"
  },
  "workers": [
    {
      "id": "worker-one",
      "accounts": {"ranges": [[1, 1]], "labels": []},
      "proxy": {"port": 17891, "node": "🇭🇰 香港S01"},
      "mode": "bounded",
      "concurrency": 1,
      "repetitions": 1,
      "repeat_interval": 0.5,
      "message_delay": 0.2,
      "steps": [
        {"kind": "messages", "messages": [
          {"id": -1, "cmd": "#time", "param": {"seconds": 0}}
        ]}
      ]
    },
    {
      "id": "worker-two",
      "accounts": {"ranges": [[2, 2]], "labels": []},
      "proxy": {"port": 17892, "node": "🇭🇰 香港S02 | IEPL"},
      "mode": "all_online",
      "concurrency": 1,
      "repetitions": 1,
      "repeat_interval": 0.5,
      "steps": [
        {"kind": "messages", "messages": [
          {"id": -1, "cmd": "#time", "param": {"seconds": 0}}
        ]}
      ]
    }
  ]
}
```

| 字段 | 含义与要求 | 默认值 |
| --- | --- | --- |
| `accounts_file` | 账号 JSON 文件路径 | 必填 |
| `clash.binary` | 已存在且可执行的 Mihomo 路径 | 必填 |
| `clash.config` | 节点 YAML 路径 | 必填 |
| `workers` | 非空工作进程数组 | 必填 |
| `workers[].id` | 本次运行中唯一，1–64 位字母、数字、下划线或连字符；不能为 `mihomo` | 必填 |
| `accounts.ranges` | 1 起算的闭区间数组 | 可省略，但必须最终选中账号 |
| `accounts.labels` | 账号标签数组，与区间合并 | 可省略，但必须最终选中账号 |
| `proxy.port` | 唯一整数，范围 1025–65535 | 必填 |
| `proxy.node` | 节点或合法代理组的完整名称 | 必填 |
| `mode` | `bounded` 或 `all_online` | `bounded` |
| `concurrency` | 正整数，含义由 `mode` 决定 | 5 |
| `repetitions` | 每个成功登录账号执行完整组合的次数，正整数 | 1 |
| `repeat_interval` | 该账号相邻两轮之间的非负秒数 | 0.5 |
| `message_delay` | 通用消息步骤使用的间隔，非负秒数 | 0.2 |
| `steps` | 按顺序执行的非空组合步骤数组 | 必填 |

时间可以为小数，不能为负数、无穷大或 NaN。并发数和轮数应使用 JSON 整数，例如 `5`，不要填写 `"5"` 或 `true`。

任务 JSON 中的 `accounts_file`、`clash.binary`、`clash.config`、消息步骤 `file`，相对路径都以**任务文件所在目录**为基准。

例如把任务放到 `app/jobs/daily.json` 后，引用 `app/config.json` 应写 `../config.json`，引用部署根目录的节点配置应写 `../../clash/nodes.yaml`。执行 `start --job jobs/daily.json` 不会改变这些解析规则。

任务文件引用账号配置，不要求把密码写入总任务 JSON。启动后生成的内部 `job.json` 会包含所选账号配置，因此仍须作为私有文件保护。

## 6. 账号配置与选择规则

### 6.1 账号文件结构

示意配置如下，账号和密码占位内容必须换成自己的实际值：

```json
{
  "login_url": "https://login-aola.100bt.com/newLogin.jsp",
  "register_url": "https://service-aola.100bt.com/newRegister.jsp",
  "timeout": 30,
  "zone_index": 1025,
  "accounts": [
    {"label": "账号一", "account": "your_account_1", "password": "your_password_1", "char_id": 0},
    {"label": "账号二", "account": "your_account_2", "password": "your_password_2", "char_id": 0}
  ]
}
```

根级共享设置应用于各账号；账号项自己的同名字段可以覆盖共享设置。`char_id` 为非负整数，`0` 可以是有效角色序号。`label` 必须非空且唯一，不应直接使用登录账号作为标签。

当前也兼容没有 `accounts` 数组、在根级直接填写一组 `account/password` 的旧单账号格式；它被视为一个账号，序号为 1。

本地源码默认读取项目根目录的 `config.json`。桌面打包目录 `dist/AolaLoader/config.json` 是另一个文件，不会因为编辑其中一个而自动同步另一个。

### 6.2 序号区间与标签

```json
{"ranges": [[1, 2], [5, 6]], "labels": ["额外账号"]}
```

含义是选第 1、2、5、6 项，再加入标签等于“额外账号”的项。区间两端都包含；区间可以选择单项，例如 `[2, 2]`。

区间与标签合并后，相同配置项会去重，并按账号文件中的序号排序。不要把 `ranges` 写成字符串 `"1-6"`，也不要用从 0 起算的数组下标。

### 6.3 同一登录账号只能选择一次

同一次运行中，跨所有 worker 检查登录账号重复。以下情况都不能启动：

- 同一登录账号被两个 worker 选中。
- 同一登录账号在配置中有两项，即使两项 `char_id` 不同，也被一起选中。

标签不同不代表登录账号不同。预检会报出冲突序号，不会自动删除或合并账号。

此前真实配置第 3、4 项属于相同登录账号、不同角色的情况；教程不改写原配置，选择任务范围时需避开同时选中这两项。后续配置顺序若发生变化，应以新的预检结果为准。

检查范围是**同一次运行**，不是服务器上的所有运行。多个 `start` 之间以及服务器与本机桌面客户端之间，没有跨进程统一账号锁；应由你分配不重叠的账号。

## 7. 执行模式、并发和时间

### 7.1 `bounded`：限制完整生命周期

假设一个 worker 选择 20 个账号，`concurrency=5`：

```text
取得一个名额 → 登录 → 玩家资料初始化 → 执行全部轮次 → 断开 → 释放名额
```

同时最多有 5 个账号处于这个生命周期中。一个账号执行完全部轮次并清理连接后，才把名额交给下一个账号。初始化和断开也在并发限制内。

### 7.2 `all_online`：限制登录阶段并发

同样选择 20 个账号、`concurrency=5`：

```text
最多5个账号同时登录和初始化
→ 全部账号的登录尝试结束
→ 成功账号并行执行组合任务
→ 完成后统一清理连接
```

此模式可能同时在线 20 个账号，并不是只保持 5 个在线。已经登录的账号会等待后续账号完成登录尝试，再一起进入任务执行阶段。

两个 worker 之间独立运行：每个有自己的并发限制、账号集合和代理。一个 worker 的验证码或限流不会自动停止另一个 worker。

### 7.3 轮次与消息间隔

若 `steps=[A, B, C]`、`repetitions=3`，每个账号执行：

```text
A → B → C → 轮间隔 → A → B → C → 轮间隔 → A → B → C
```

不是先把 A 重复三次，再执行 B。最后一轮之后不会再额外等待 `repeat_interval`。

| 参数/等待 | 作用范围 |
| --- | --- |
| `repeat_interval` | 该账号完成整个组合后，到下一轮前的等待 |
| `message_delay` | 通用 `messages` 步骤的发送节奏 |
| `#time` | 明确插入当前位置的本地暂停 |
| `#wait` | 等待当前战斗结束 |
| 专用脚本内部等待 | 由脚本自己的实现决定 |
| 服务器响应等待 | 房间、进入战斗、回合、结束等状态所需的等待 |

总耗时还包括登录、初始化、匹配、服务器状态推进和连接清理。三条消息、间隔 0.2 秒，不代表完整一轮必定在 0.6 秒结束。`completed_repetitions` 只统计完整成功结束的轮次，失败到一半的轮次不计入。

外层 `message_delay` 不会强行覆盖 `scripts.auto_battle` 等专用脚本内部再次调用执行器时采用的间隔。

### 7.4 失败处理

普通账号失败时，停止该账号后续步骤和轮次，其他账号继续按所选模式处理。出现登录限流、验证码或登录流程明确禁止继续时，该 worker 停止后续登录，并取消清理自己的任务和连接。

没有自动绕过验证、自动重启或无限执行选项。断线或失败后不会自动从失败轮次续跑；新的 `start` 是新的运行。

## 8. 组合步骤与消息格式

`steps` 支持两种 `kind`：`messages` 和 `script`。所有步骤在同一账号连接上按数组顺序执行。

### 8.1 内联消息

```json
{
  "kind": "messages",
  "messages": [
    {"id": 13, "cmd": "1101", "param": {}},
    {"id": -1, "cmd": "#time", "param": {"seconds": 1.5}}
  ]
}
```

`1101` 是项目初始化中使用的查询消息格式示例。具体业务消息仍要按已经核对的协议参数填写。

真正发往游戏的消息字段为：

| 字段 | 要求 |
| --- | --- |
| `id` | Extension ID，0–65535 的整数；不是账号序号或用户 ID |
| `cmd` | 字符串，即使命令是数字也应写 `"1101"` |
| `param` | JSON 对象，空参数写 `{}` |

消息对象只允许 `id`、`cmd`、`param` 三个字段。内联 `messages` 必须是对象数组，不能直接填一整段 `#send=...|` 文本字符串。

### 8.2 从消息文件读取

```json
{"kind": "messages", "file": "messages/query.txt"}
```

创建 UTF-8 文本文件，例如 `messages/query.txt`：

```text
// 查询与暂停示例
#send={"id":13,"cmd":"1101","param":{}}|
#time = 1.5|
#send={"id":13,"cmd":"1212","param":{}}|
```

`file` 与 `messages` 二选一，不能在同一个步骤中同时填写。每个消息批次必须至少有一条可执行内容。

消息文件识别规则：

- 用 JSON 字符串外的 `|` 分段，消息不必一行一条。
- JSON 字符串内的竖线，例如 `"a|b"`，保留为参数内容。
- 连续 `||`、仅空格或换行的空段跳过。
- 开头去掉空白后以 `//` 开始的整行视为注释；不支持在 JSON 后随意追加行尾注释。
- 兼容文件末尾最后一条没有 `|`，但建议每条都写结束竖线。
- JSON 可排成多行；消息文件可以使用 UTF-8 BOM。

例如下面等价于两次暂停，不会把中间空段当作消息：

```text
|| #time=0.2|   |

#time=0.5||
```

### 8.3 `#time`：本地暂停

文件格式：

```text
#time = 2.5|
```

内联格式：

```json
{"id": -1, "cmd": "#time", "param": {"seconds": 2.5}}
```

单位为秒，可用小数，0 表示不额外暂停。该指令执行本地异步等待，不作为 EXT 消息发送，不代表服务器已处理之前的消息。

### 8.4 `#wait`：等待当前战斗结束

文件格式：

```text
#wait|
```

内联格式：

```json
{"id": -1, "cmd": "#wait", "param": {}}
```

它检查当前账号的战斗状态，正在战斗时等待本场结束，再继续后续消息。正常结束由本场服务器 `2403` 推进状态。等待期间断线、状态未知、战斗身份改变或战斗错误，会停止该账号的流程。

**`#wait` 不是等待下一场战斗开始。**当前是 `idle`、不在战斗中时，它立即继续。若前一条只是发起活动匹配、战斗还未被确认，就不能仅靠追加 `#wait` 来保证等到这场战斗结束；应使用会明确等待进入战斗的专用脚本或已经支持的战斗进入序列。

本指令的等待循环没有单独的固定总超时；如果连接和状态一直正常、服务器一直不结束，会持续等待。需要主动停止时使用 `stop`。

数字 `id=-1` 专用于上述本地指令，不是游戏 Extension ID。

### 8.5 会话维护消息

消息文件导入会过滤 `id=18 / cmd=55_2`，因为它由会话服务维护；若过滤后没有任何可执行内容，预检报错。

不要在任务里手动维护 `55_1`、`55_2`、`55_9` 会话消息。内联消息不会简单照搬文件过滤行为，执行器会拒绝会话服务拥有的这些命令。出现相关错误时应删除手工维护消息，而不是增加发送轮次。

## 9. 专用脚本与战斗任务

### 9.1 查看当前可选脚本

在项目目录运行以下只读命令，不会调用脚本的 `run`，不登录账号：

```bash
.venv/bin/python -c 'from src.scripting.loader import discover_scripts; [print(s.module_name + " : " + s.name) for s in discover_scripts()]'
```

2026-10-13 本地发现列表：

| 模块名 | 界面名称 |
| --- | --- |
| `scripts.auto_battle` | 多排自动匹配战斗 |
| `scripts.auto_escape` | 多排自动逃跑 |
| `scripts.rank_info` | 段位信息 |
| `scripts.mt250816_1` | 四象一 |
| `scripts.mt250816_2` | 四象二 |
| `scripts.mt250816_3` | 四象三 |
| `scripts.mt250816_4` | 四象四 |

应以实际部署目录的发现结果为准。任务中填写的是 `scripts.auto_escape` 这样的模块名，不是中文名称或文件绝对路径。

```json
{"kind": "script", "module": "scripts.rank_info"}
```

当前 `script` 步骤只提供模块选择，不提供 `params`、`args` 或前后置消息覆盖接口。

### 9.2 自动战斗与自动逃跑

两者共有的流程是：

```text
发送脚本前置消息 → 等待本次新战斗就绪 → 发送脚本后置消息
```

按当前本地脚本：

- `auto_battle`：前置发送活动相关消息及匹配消息，战斗就绪后发送 `1409/useAiType=1`，再执行 `#wait` 等待结束。
- `auto_escape`：前置发送活动相关消息及匹配消息，战斗就绪后发送 `1404`，再执行 `#wait` 等待结束。
- 两者等待新战斗就绪的脚本超时当前为 60 秒；它不是 `#wait` 等待结束的超时。

前置活动参数和后置消息已经写在脚本中。更换活动、匹配类别或后续处理，需要修改对应脚本的 `BEFORE_STEPS/AFTER_STEPS`，再同步到服务器。任务 JSON 的 `repetitions` 和 `repeat_interval` 只控制外层次数与间隔，不重写脚本内部消息。

逃跑执行器按己方已确认的实际战场位置补齐目标参数；服务器拒绝逃跑时记录错误并停止该账号后续步骤。`#wait` 保证正常情况下等到本场结束后，脚本才返回给外层下一轮。

### 9.3 多步骤组合示例

下面仅展示 `steps` 的结构，放入某个 worker 内。它包含真实战斗脚本，执行 `start` 会发起战斗，不能把它当成初始化测试模板：

```json
[
  {"kind": "script", "module": "scripts.rank_info"},
  {"kind": "messages", "messages": [
    {"id": -1, "cmd": "#time", "param": {"seconds": 1}}
  ]},
  {"kind": "script", "module": "scripts.auto_escape"}
]
```

如果同一个 worker 的 `repetitions=10`，每个账号会按“查询段位 → 暂停 → 匹配并逃跑”的完整顺序执行十轮。多个账号之间并行，不保证它们在同一时刻处于同一个步骤。

新增专用脚本必须放入服务器项目 `scripts` 目录，提供 `async def run(context)`；可填写 `SCRIPT_NAME` 和 `SCRIPT_DESCRIPTION`。以下文件不会成为可选脚本：下划线开头、`__init__.py`、`verify.py`，以及没有符合接口的异步 `run` 的工具文件。

## 10. 独立代理与多次运行

每个 worker 固定使用自己的 SOCKS5 端口。HTTP 角色查询、HTTP 登录、TCP/WSS 游戏连接均走该端口；域名交由代理处理，代理失败不会回退到直连。WSS 不叠加额外的环境代理。

程序只导入节点 YAML 中的出站定义，生成本次运行配置。不会启用本机配置的 TUN、DNS设置、控制接口或进程路由规则；无需修改服务器系统代理或系统路由。

同一个 worker 中的所有账号共享一个代理节点。若要账号分别走不同节点，应放入不同 worker。

节点名必须完整匹配，包含国旗字符、空格及 `|`，例如：

```json
{"port": 17892, "node": "🇭🇰 香港S02 | IEPL"}
```

端口只监听 `127.0.0.1`，不需要为它们开放服务器公网安全组。它们在任务启动时出现，任务结束时关闭。

每次运行内部端口必须唯一，且启动时不能被其他程序占用。两个并行的 `start` 不能同时使用同一组 17891/17892 端口；第二份任务需选择其他端口，如 17893/17894，并使用不重叠账号。

节点或静态代理组不能包含 DIRECT 直连路由。文件型代理集合文件应位于节点配置所在目录内；HTTP 型集合缓存会写入本次运行目录。实际节点参数是否可用，还需 Mihomo 和网络实测。

2026-10-12 已授权部署的两个节点及实测出口：

| 端口 | 节点名称 | 当时实测出口 |
| --- | --- | --- |
| 17891 | 🇭🇰 香港S01 | 141.11.146.77 |
| 17892 | 🇭🇰 香港S02 \| IEPL | 103.151.172.152 |

这是当时验收结果，不保证节点出口永久不变。不同节点名称也不天然保证不同公网 IP。

## 11. 状态、日志与发送结果

### 11.1 运行状态

| `state` | 含义 |
| --- | --- |
| `starting` | 正在准备 Mihomo 或启动 worker |
| `running` | worker 已启动；不表示所有账号都已登录 |
| `completed` | 全部 worker 成功结束 |
| `failed` | 启动异常、代理异常，或至少有 worker 未成功完成 |
| `stopped` | 已执行停止流程 |
| `interrupted` | 主管已不存活，但保存状态仍是 starting/running |

账号状态可能为 `queued`、`connecting`、`ready`、`running`、`completed`、`failed`、`skipped`、`stopped`。其中 `ready` 表示该账号已完成登录初始化，尚未开始任务。

重点查看：`workers[].accounts[].state`、`completed_repetitions`、`error`、`login_blocked`，以及 worker 的 `process.alive`。

失败运行中可以有已经成功完成的账号，不要只看总 `failed` 就重复登录全部账号。需要补测时另建仅选择失败账号的有限轮次任务，并确认原进程已退出。

### 11.2 文件结构

以 worker ID 为 `worker-one` 为例：

```text
logs/jobs/RUN_ID/
├── launch.json                  主管 PID、出生标识和启动时间
├── run.json                     运行总状态、子进程记录、最终退出码
├── job.json                     私有任务快照，含所选账号配置
├── supervisor-console.log       主管异常输出
├── mihomo-check.log             Mihomo -t 检查输出
├── mihomo.log                   Mihomo 运行输出
├── worker-one-console.log       worker 的进程级输出
├── mihomo/
│   ├── config.yaml              本次生成的私有代理配置
│   └── providers/               代理集合缓存（如使用）
└── worker-one/
    ├── accounts.json            账号当前进度与失败原因
    ├── runtime-YYYYMMDD.log     登录、初始化、消息及脚本过程
    ├── aola-recv-*.jsonl        已解码的相关游戏接收记录
    ├── send.jsonl               有可追踪网络发送结果时创建
    ├── steps.jsonl              各轮组合结果
    └── login-failures/          发生登录/角色查询错误时的诊断
```

不是所有文件都会存在：没有可追踪的网络发送时可能没有 `send.jsonl`，没有登录错误时可能没有 `login-failures`。接收 JSONL 只记录配置的相关消息范围，不是全部 HTTP/TCP 抓包。

检查日志的常用命令：

```bash
# 把下面示例替换为实际运行编号和 worker ID
tail -n 80 logs/jobs/RUN_ID/worker-one/runtime-20261013.log
tail -f logs/jobs/RUN_ID/worker-one/runtime-20261013.log
cat logs/jobs/RUN_ID/worker-one/accounts.json
```

`tail -f` 中的 `Ctrl+C` 只退出日志查看，不停止后台任务。停止任务仍使用 `cli_main.py stop --run RUN_ID`。

### 11.3 发送成功与业务成功

`send.jsonl` 的 `success=true` 表示该条消息的本地发送流程成功，不应单独解释成服务器已执行对应技能、逃跑或其他业务。战斗业务还要结合服务器回包与战斗管理状态。

当前 `send.jsonl` 只记录执行器返回的网络消息结果，排除 `#time/#wait` 等本地指令，也不是登录初始化和会话维护所有发包的完整统计。

旧脚本可能返回 `None`，不返回发送明细。即使 `steps.jsonl` 中 `send_results` 为空，也不能据此说该脚本“实际发送0条”；`accounts.json` 中的 `send_details` 会提示明细的追踪限制。

### 11.4 私有文件

运行目录权限为 700，内部快照权限为 600。`job.json` 含账号配置，`mihomo/config.yaml` 和节点 YAML 含代理认证参数，均不作为可分享日志。

日志对已知密码、会话令牌和认证字段做脱敏；分享业务日志前仍需核对脚本自定义数据。不要把真实 `config.json`、节点密码或快照贴到公共讨论中。

## 12. 原交互菜单

Windows 本地：

```powershell
cd D:\game\offline_loader
python cli_main.py
```

Linux 部署：

```bash
.venv/bin/python cli_main.py
```

主菜单：

```text
1. 单账号登录
2. 多账号登录
3. 退出
```

单账号登录后提供粘贴消息序列、选择消息文件、运行专用脚本、查看连接状态、断开并返回。

多账号登录菜单提供消息粘贴/文件发送、状态、指定账号重连、指定账号断开和断开全部；原菜单登录并发上限为 5。选择多个账号可输入 `1,2,5`，允许全部选择的菜单可输入 `a`。这里的账号选择字符串不同于任务 JSON 的 `ranges` 数组。

原多账号菜单没有组合脚本步骤编辑器，也没有批量专用脚本选择入口；需要组合任务时使用 JSON 配置接口。

交互菜单不会因为服务器有 `clash/nodes.yaml` 就自动创建独立 Mihomo 和各 worker 代理。需要不同进程分别走指定节点时，使用配置驱动任务。

## 13. 更新脚本和配置

修改脚本时，建议按以下顺序：

1. 用 `stop` 停掉正在使用该脚本的运行，保留日志。
2. 在本地修改脚本，确认其消息和等待流程。
3. 将脚本同步到服务器项目 `app/scripts/`，保留旧文件用于回退。
4. 在服务器检查语法、查看脚本发现列表。
5. 执行任务预检，再启动新的有限轮次运行。

例如服务器检查某个脚本的语法：

```bash
.venv/bin/python -m py_compile scripts/auto_escape.py
.venv/bin/python cli_main.py validate --job tasks.json
```

Python CLI 不需要编译 WPF，也不需要把脚本打包进 `AolaBackend.exe`。本地文件修改不会自动传到服务器。

任务消息和账号选择在启动时进入私有快照，运行中编辑原 JSON 或消息文件不会重写该次任务。脚本代码不是快照的一部分，不应在运行期间替换；停止后更新再启动可以避免不同进程加载不同版本。

停止后再次 `start` 会重新登录并从第一轮开始，没有“续跑上次剩余轮次”的接口。

## 14. 常见问题

| 现象 | 排查与处理 |
| --- | --- |
| `Mihomo 可执行文件不存在` | 核对路径相对任务文件的位置，并确认服务器文件可执行；Linux 可检查 `ls -l ../runtime/mihomo/mihomo` |
| 节点不存在 | 对照节点 YAML 的完整 `name`，包括国旗、空格和竖线；通用模板中的“节点A/B”必须替换 |
| 登录账号重复 | 按报错序号缩小选择范围；不同角色、不同标签不能消除登录账号重复 |
| 代理端口重复/被占用 | 同一任务各 worker 用不同端口；检查其他运行；只停止自己的冲突运行或改用空闲端口 |
| `运行编号不存在` | 使用启动输出的 `run_id`，确认 `--runs-dir` 与启动时一致 |
| Windows 提示进程管理用于 Linux | 到 Linux 执行配置任务；Windows 原菜单和可满足本地引用路径的预检仍可用 |
| 专用脚本不在列表中 | 检查服务器 `scripts` 中是否有文件、模块名是否正确、是否提供异步 `run(context)` |
| JSON 或消息解析失败 | 检查对象字段、数字/字符串类型、逗号、竖线，以及 `param` 是否为对象 |
| `55_2` 等会话消息错误 | 去掉手工会话维护消息，由会话服务管理 |
| 角色查询/登录失败 | 查看账号 `error` 与 `login-failures` 的阶段、HTTP状态和诊断正文；没有HTTP返回不能直接判断密码错误 |
| 限流/验证码 | 当前 worker 会停止后续登录；根据真实错误处理，不继续扩大批量或绕过验证 |
| `#wait` 没有等到匹配开始 | 当时仍为 idle 时会直接继续；用明确等待新战斗就绪的脚本 |
| `#wait` 长时间不返回 | 查看运行日志、接收记录、连接和战斗阶段；必要时主动停止 |
| 明细显示0条或为空 | 区分本地指令、初始化发包、旧脚本缺失明细和实际网络发送；结合 runtime/steps 日志 |
| 轮次耗时远超消息间隔 | 检查匹配、战斗、状态回包、`#time`、脚本内部等待和轮间隔，不只按消息条数计算 |
| `alive=false`、`interrupted` | 主管已退出或异常终止；用 `stop` 检查回收本次记录中的子进程，再查看错误日志 |

`validate` 通过后仍可能遇到真实网络、节点或游戏业务错误。重新预检不能替代检查失败阶段和服务器回包。

## 15. 离线验证与已验证范围

从项目目录执行离线测试：

```bash
.venv/bin/python -B -m unittest scripts.test_cli_jobs scripts.test_rank_info -v
.venv/bin/python -B -m scripts.verify
```

这些用例使用模拟账号/回环服务，不批量登录真实账号。Linux 进程测试使用本地模拟 SOCKS5 和模拟 Mihomo 进程；Windows 会按平台跳过 Linux 进程管理用例。

2026-10-12 的部署验收包括：Linux 15 项 unittest 和原有55项离线检查通过；两个真实节点的出口不同；两个不同账号最终均完成代理 HTTP 登录、TCP 游戏连接与资料初始化。首轮账号1角色查询失败，单次受控补测成功；中间 HTTP 诊断也记录到另一节点超时，没有据此宣称线路始终稳定。

真实 WSS 登录没有另行验收，HTTP/TCP/WSS 的指定代理及拒绝直连行为有模拟测试。此轮真实测试没有执行匹配、自动战斗或逃跑，所以不能把初始化成功解释为所有专用战斗脚本已通过服务器业务验证。

详细记录：

- [CLI 实施与测试报告](build/analysis/linux-deploy-20261012/cli-jobs-implementation-report.md)
- [两节点真实代理验收报告](build/analysis/linux-deploy-20261012/node-reference-result.md)
- [两节点脱敏测试日志](build/analysis/linux-deploy-20261012/server-evidence/node-test-logs.tar.gz)
- [项目通用任务模板](tasks.example.json)

最常用的操作顺序是：**确认任务内容 → validate → start --detach → 保存运行编号 → status/查看日志 → 完成或 stop 清理**。
