# 独立浏览器人工验证

`python -m Com.load_second` 已启用浏览器验证回退。普通 HTTP 登录成功时不会打开浏览器；角色查询或登录收到腾讯验证码页面时，先保存原响应，再启动独立的 Edge 窗口。

## 使用

1. 安装 Microsoft Edge，并执行 `python -m pip install -r requirements.txt`。
2. 运行 `python -m Com.load_second`。
3. 出现 Edge 验证窗口时，手动完成网站提供的验证。如果浏览器询问是否重新提交表单，确认继续即可。
4. 程序等待官方页面刷新后的登录结果。角色 ID 和 sid 校验通过后，窗口自动关闭，继续连接配置指定的区服并执行原有脚本。

每个请求最多等待 5 分钟。关闭窗口、验证超时、浏览器登录失败都会停止本次任务，避免再次循环弹窗。再次运行命令才会重新尝试。

## 会话与诊断

浏览器内重新查询角色并登录，两步共享同一个隔离会话。官方页面执行自身的验证脚本，程序不处理验证码答案或复用验证码票据。Cookie 仅保留在本次浏览器会话中，不读取日常 Edge 配置，不导出浏览器凭据，也不把 Cookie 转回 aiohttp。成功登录结果只在内存中传给后续连接流程。

失败响应仍保存到 `evidence/login-failures/`。浏览器阶段文件带 `browser_role_query` 或 `browser_login` 标记。需要进一步排查时，提供新的诊断 JSON 文件路径和窗口表现即可；不需要提供账号密码、Cookie、sid 或验证码票据。

浏览器回退目前只在 `Com.load_second` 默认启用；其他使用 `authenticate(config)` 的入口可显式设置 `browser_verification: true`。打包版 WPF 未重新构建或验证。

## 验证范围

`python -m scripts.test_browser_login` 使用本地 HTTP 服务和无界面 Edge，检查 HTTP 失败后切入浏览器、模拟人工验证后的 Cookie/POST 保留、窗口关闭、超时和停止重试。测试不访问游戏服务器、不自动完成真实验证码。

本地流程通过不代表真实站点已接受登录。首次实际验证需人工操作；站点仍可能依据自身策略拒绝请求。
