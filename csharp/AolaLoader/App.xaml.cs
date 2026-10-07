using System.Windows;

namespace AolaLoader;

public partial class App : System.Windows.Application
{
    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        if (e.Args.Contains("--smoke-test", StringComparer.OrdinalIgnoreCase))
        {
            ShutdownMode = ShutdownMode.OnExplicitShutdown;
            try
            {
                await using var bridge = new BackendBridge();
                await bridge.StartAsync();
                await bridge.SendAsync("get_state");
                var accounts = await bridge.SendAsync("get_accounts");
                _ = accounts.GetProperty("accounts");
                _ = accounts.GetProperty("connection_busy");
            }
            catch
            {
                Shutdown(1);
                return;
            }
            Shutdown(0);
            return;
        }

        var window = new MainWindow();
        MainWindow = window;
        window.Show();
    }
}
