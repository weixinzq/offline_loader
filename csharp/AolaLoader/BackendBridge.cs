using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;

namespace AolaLoader;

public sealed class BackendEventArgs(JsonElement payload) : EventArgs
{
    public JsonElement Payload { get; } = payload;
}

public sealed class BackendBridge : IAsyncDisposable
{
    private readonly ConcurrentDictionary<string, TaskCompletionSource<JsonElement>> _pending = new();
    private NamedPipeClientStream? _pipe;
    private StreamReader? _reader;
    private StreamWriter? _writer;
    private Process? _backend;
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private CancellationTokenSource? _lifetime;

    public event EventHandler<BackendEventArgs>? EventReceived;
    public event Action<string>? Disconnected;

    public async Task StartAsync()
    {
        string pipeName = $"aola-loader-{Guid.NewGuid():N}";
        _backend = StartBackend(pipeName);
        _pipe = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(15));
        await _pipe.ConnectAsync(timeout.Token);
        _reader = new StreamReader(_pipe, new UTF8Encoding(false), false, 8192, true);
        _writer = new StreamWriter(_pipe, new UTF8Encoding(false), 8192, true) { AutoFlush = true };
        _lifetime = new CancellationTokenSource();
        _ = Task.Run(() => ReadLoopAsync(_lifetime.Token));
    }

    public async Task<JsonElement> SendAsync(string action, object? payload = null)
    {
        if (_writer is null)
            throw new InvalidOperationException("后台尚未启动");
        string id = Guid.NewGuid().ToString("N");
        var completion = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = completion;
        string json = JsonSerializer.Serialize(new { id, action, payload = payload ?? new { } });
        await _writeLock.WaitAsync();
        try { await _writer.WriteLineAsync(json); }
        catch { _pending.TryRemove(id, out _); throw; }
        finally { _writeLock.Release(); }
        return await completion.Task;
    }

    private async Task ReadLoopAsync(CancellationToken token)
    {
        string reason = "后台连接已关闭";
        try
        {
            while (!token.IsCancellationRequested && _reader is not null)
            {
                string? line = await _reader.ReadLineAsync(token);
                if (line is null) break;
                using JsonDocument document = JsonDocument.Parse(line);
                JsonElement root = document.RootElement;
                string type = root.GetProperty("type").GetString() ?? "";
                if (type == "response")
                {
                    string id = root.GetProperty("id").GetString() ?? "";
                    if (!_pending.TryRemove(id, out var completion)) continue;
                    if (root.GetProperty("ok").GetBoolean())
                        completion.TrySetResult(root.TryGetProperty("data", out var data) ? data.Clone() : default);
                    else
                        completion.TrySetException(new InvalidOperationException(root.GetProperty("error").GetString()));
                }
                else if (type == "event")
                {
                    EventReceived?.Invoke(this, new BackendEventArgs(root.Clone()));
                }
            }
        }
        catch (OperationCanceledException) { reason = "后台已停止"; }
        catch (Exception ex) { reason = $"后台通信异常：{ex.Message}"; }
        finally
        {
            foreach (var item in _pending.Values)
                item.TrySetException(new IOException(reason));
            _pending.Clear();
            Disconnected?.Invoke(reason);
        }
    }

    private static Process StartBackend(string pipeName)
    {
        string packaged = Path.Combine(AppContext.BaseDirectory, "AolaBackend.exe");
        ProcessStartInfo start = new()
        {
            WorkingDirectory = File.Exists(packaged) ? AppContext.BaseDirectory : FindProjectRoot(),
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        if (File.Exists(packaged))
        {
            start.FileName = packaged;
        }
        else
        {
            start.FileName = "python";
            start.ArgumentList.Add("-m");
            start.ArgumentList.Add("src.ipc.server");
        }
        start.ArgumentList.Add("--pipe");
        start.ArgumentList.Add(pipeName);
        return Process.Start(start) ?? throw new InvalidOperationException("无法启动 Python 后台");
    }

    private static string FindProjectRoot()
    {
        foreach (string start in new[] { Environment.CurrentDirectory, AppContext.BaseDirectory })
        {
            DirectoryInfo? directory = new(start);
            while (directory is not null)
            {
                if (File.Exists(Path.Combine(directory.FullName, "src", "ipc", "server.py")))
                    return directory.FullName;
                directory = directory.Parent;
            }
        }
        throw new DirectoryNotFoundException("找不到 Python 项目目录");
    }

    public async ValueTask DisposeAsync()
    {
        try
        {
            if (_writer is not null && _pipe?.IsConnected == true)
                await SendAsync("shutdown").WaitAsync(TimeSpan.FromSeconds(3));
        }
        catch { }
        _lifetime?.Cancel();
        _reader?.Dispose();
        _writer?.Dispose();
        _pipe?.Dispose();
        if (_backend is { HasExited: false })
        {
            if (!_backend.WaitForExit(2500)) _backend.Kill(true);
        }
        _backend?.Dispose();
        _lifetime?.Dispose();
        _writeLock.Dispose();
    }
}
