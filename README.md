# 奥拉星长连接客户端

这是一个用于登录奥拉星服务器、维持游戏长连接并按需执行网络交互脚本的项目。保留 C# WPF 桌面端构建和 Python CLI 启动；桌面端的登录、协议和脚本后端使用 Python。




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
