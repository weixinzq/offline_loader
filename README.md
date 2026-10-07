# 奥拉星长连接客户端

这是一个用于登录奥拉星服务器、维持游戏长连接并按需执行网络交互脚本的项目。保留 C# WPF 桌面端构建和 Python CLI 启动；桌面端的登录、协议和脚本后端使用 Python。



## 战斗就绪后执行消息脚本

`scripts/auto_battle.py` 预留了 `BEFORE_STEPS` 和 `AFTER_STEPS`。两者都可填写多个 `MessageBatchStep`，每个步骤包含按顺序执行的多条 `SendMessage`。脚本先执行前置步骤，再等待本次新战斗收齐入口消息，最后执行后置步骤；入口最长等待 60 秒。

具体消息尚未填写，当前运行会提示补充，不会发送占位消息。开始运行时应没有正在进行的战斗。桌面打包配置已包含这个脚本；以后填写消息后，需要重新打包后端并重启加载器。

## 验证

```powershell
python -m scripts.verify
python -m compileall -q cli_main.py backend_main.py src scripts
dotnet build .\csharp\AolaLoader\AolaLoader.csproj -c Release
```

## 启动 CLI

安装运行依赖并从项目根目录启动：

```powershell
python -m pip install -r requirements.txt
python .\cli_main.py
```

## 构建 WPF 桌面程序

安装构建依赖并执行：

```powershell
python -m pip install -r requirements-build.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_wpf.ps1
```

输出文件：

```text
dist/AolaLoader/AolaLoader.exe
dist/AolaLoader/AolaBackend.exe
dist/AolaLoader/config.example.json
```


调试工具保留在 `scripts/debug/`，以模块方式运行，例如：

```powershell
python -m scripts.debug.wss_test2
```
