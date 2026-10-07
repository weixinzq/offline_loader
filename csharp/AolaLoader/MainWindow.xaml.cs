using Microsoft.Win32;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace AolaLoader;

public partial class MainWindow : Window
{
    public ObservableCollection<AccountItem> Accounts { get; } = [];
    public ObservableCollection<ScriptItem> Scripts { get; } = [];
    public ObservableCollection<MessageItem> Messages { get; } = [];
    public ObservableCollection<CombinationStepItem> CombinationSteps { get; } = [];
    public ObservableCollection<LogItem> Logs { get; } = [];

    private BackendBridge? _bridge;
    private CancellationTokenSource? _statusPolling;
    private Task? _statusPollingTask;
    private bool _allowClose;

    public MainWindow()
    {
        InitializeComponent();
        DataContext = this;
    }

    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        _bridge = new BackendBridge();
        _bridge.EventReceived += Bridge_EventReceived;
        _bridge.Disconnected += reason =>
            _ = Dispatcher.InvokeAsync(() => AddLog(reason, "error"));
        try
        {
            await _bridge.StartAsync();
            _statusPolling = new CancellationTokenSource();
            _statusPollingTask = PollAccountStateAsync(
                _bridge, _statusPolling.Token
            );
        }
        catch (Exception ex)
        {
            AddLog($"后台启动失败：{ex.Message}", "error");
            MessageBox.Show(this, ex.Message, "后台启动失败", MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }

    private void Bridge_EventReceived(object? sender, BackendEventArgs e)
    {
        JsonElement payload = e.Payload;
        _ = Dispatcher.InvokeAsync(() =>
        {
            string eventName = payload.GetProperty("event").GetString() ?? "";
            switch (eventName)
            {
                case "accounts": UpdateAccounts(payload.GetProperty("accounts")); break;
                case "scripts": UpdateScripts(payload.GetProperty("scripts")); break;
                case "messages": UpdateMessages(payload.GetProperty("messages")); break;
                case "log":
                    AddLog(
                        payload.GetProperty("message").GetString() ?? "",
                        payload.TryGetProperty("category", out var category) ? category.GetString() ?? "info" : "info"
                    );
                    break;
                case "task":
                    if (payload.GetProperty("kind").GetString() == "connection")
                        SetConnectionButtonsEnabled(true);
                    break;
            }
        });
    }

    private async Task PollAccountStateAsync(
        BackendBridge bridge, CancellationToken cancellationToken
    )
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                JsonElement state = await bridge.SendAsync("get_accounts");
                await Dispatcher.InvokeAsync(() =>
                {
                    UpdateAccounts(state.GetProperty("accounts"));
                    SetConnectionButtonsEnabled(
                        !state.GetProperty("connection_busy").GetBoolean()
                    );
                });
                await Task.Delay(350, cancellationToken);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            catch
            {
                try
                {
                    await Task.Delay(1000, cancellationToken);
                }
                catch (OperationCanceledException)
                {
                    return;
                }
            }
        }
    }

    private void UpdateAccounts(JsonElement values)
    {
        HashSet<string> selected = AccountList.SelectedItems.Cast<AccountItem>().Select(x => x.Label).ToHashSet();
        var incoming = values.EnumerateArray()
            .Select(value => new AccountItem
            {
                Label = value.GetProperty("label").GetString() ?? "",
                Status = value.GetProperty("status").GetString() ?? "离线"
            }).ToList();
        Accounts.Clear();
        foreach (var account in incoming) Accounts.Add(account);
        foreach (var account in Accounts.Where(item => selected.Contains(item.Label)))
            AccountList.SelectedItems.Add(account);
    }

    private void UpdateScripts(JsonElement values)
    {
        string? selected = (ScriptList.SelectedItem as ScriptItem)?.Module;
        Scripts.Clear();
        foreach (JsonElement value in values.EnumerateArray())
        {
            Scripts.Add(new ScriptItem
            {
                Module = value.GetProperty("module").GetString() ?? "",
                Name = value.GetProperty("name").GetString() ?? "",
                Description = value.GetProperty("description").GetString() ?? ""
            });
        }
        ScriptList.SelectedItem = Scripts.FirstOrDefault(script => script.Module == selected) ?? Scripts.FirstOrDefault();
        CombinationScript.SelectedItem = Scripts.FirstOrDefault();
    }

    private void UpdateMessages(JsonElement values)
    {
        Messages.Clear();
        foreach (JsonElement value in values.EnumerateArray())
        {
            Messages.Add(new MessageItem
            {
                Sequence = value.GetProperty("sequence").GetInt32(),
                Id = value.GetProperty("id").GetInt32(),
                Cmd = value.GetProperty("cmd").GetString() ?? "",
                Param = value.GetProperty("param").Clone()
            });
        }
    }

    private void AddLog(string message, string category = "info")
    {
        if (string.IsNullOrWhiteSpace(message)) return;
        Logs.Add(new LogItem { Time = DateTime.Now.ToString("HH:mm:ss"), Message = message, Category = category });
        if (Logs.Count > 1000) Logs.RemoveAt(0);
        LogList.ScrollIntoView(Logs.Last());
    }

    private async Task<bool> CommandAsync(string action, object? payload = null, bool showError = true)
    {
        if (_bridge is null)
        {
            if (showError) MessageBox.Show(this, "后台尚未启动。", "操作失败", MessageBoxButton.OK, MessageBoxImage.Warning);
            return false;
        }
        try
        {
            await _bridge.SendAsync(action, payload);
            return true;
        }
        catch (Exception ex)
        {
            AddLog(ex.Message, "error");
            if (showError) MessageBox.Show(this, ex.Message, "操作失败", MessageBoxButton.OK, MessageBoxImage.Warning);
            return false;
        }
    }

    private List<string> SelectedLabels() => AccountList.SelectedItems.Cast<AccountItem>().Select(item => item.Label).ToList();
    private List<string> AllLabels() => Accounts.Select(item => item.Label).ToList();

    private List<string>? RequireSelected(bool onlineOnly = false)
    {
        List<string> labels = SelectedLabels();
        if (labels.Count == 0)
        {
            MessageBox.Show(this, "请至少选择一个账号。", "账号", MessageBoxButton.OK, MessageBoxImage.Information);
            return null;
        }
        if (onlineOnly)
        {
            var offline = Accounts.Where(item => labels.Contains(item.Label) && item.Status != "在线").Select(item => item.Label).ToList();
            if (offline.Count > 0)
            {
                MessageBox.Show(this, $"以下账号不在线：{string.Join("、", offline)}", "账号", MessageBoxButton.OK, MessageBoxImage.Information);
                return null;
            }
        }
        return labels;
    }

    private void AccountList_PreviewMouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        DependencyObject? current = e.OriginalSource as DependencyObject;
        while (current is not null && current is not ListBoxItem)
            current = VisualTreeHelper.GetParent(current);
        if (current is not ListBoxItem item) return;
        item.IsSelected = !item.IsSelected;
        e.Handled = true;
    }

    private void SelectAll_Click(object sender, RoutedEventArgs e) => AccountList.SelectAll();
    private void ClearSelection_Click(object sender, RoutedEventArgs e) => AccountList.UnselectAll();

    private async void AddAccount_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new AccountDialog(new AccountForm(), false) { Owner = this };
        if (dialog.ShowDialog() != true) return;
        await CommandAsync("add_account", new
        {
            label = dialog.Value.Label,
            account = dialog.Value.Account,
            password = dialog.Value.Password,
            char_id = dialog.Value.CharId,
            zone_index = dialog.Value.ZoneIndex
        });
    }

    private async void EditAccount_Click(object sender, RoutedEventArgs e)
    {
        List<string>? labels = RequireExactlyOne();
        if (labels is null || _bridge is null) return;
        try
        {
            JsonElement details = await _bridge.SendAsync("get_account", new { label = labels[0] });
            var value = new AccountForm
            {
                Label = details.GetProperty("label").GetString() ?? "",
                Account = details.GetProperty("account").GetString() ?? "",
                CharId = details.GetProperty("char_id").GetInt32(),
                ZoneIndex = details.GetProperty("zone_index").GetInt32()
            };
            var dialog = new AccountDialog(value, true) { Owner = this };
            if (dialog.ShowDialog() != true) return;
            await CommandAsync("edit_account", new
            {
                old_label = labels[0],
                label = value.Label,
                account = value.Account,
                password = value.Password,
                char_id = value.CharId,
                zone_index = value.ZoneIndex
            });
        }
        catch (Exception ex)
        {
            AddLog(ex.Message, "error");
            MessageBox.Show(this, ex.Message, "编辑账号失败", MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    private async void DeleteAccount_Click(object sender, RoutedEventArgs e)
    {
        List<string>? labels = RequireExactlyOne();
        if (labels is null) return;
        if (MessageBox.Show(this, $"确定删除账号“{labels[0]}”吗？", "删除账号", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
        await CommandAsync("delete_account", new { old_label = labels[0] });
    }

    private List<string>? RequireExactlyOne()
    {
        List<string> labels = SelectedLabels();
        if (labels.Count == 1) return labels;
        MessageBox.Show(this, "此操作需要恰好选择一个账号。", "账号", MessageBoxButton.OK, MessageBoxImage.Information);
        return null;
    }

    private async Task ConnectionActionAsync(string action, List<string>? labels)
    {
        if (labels is null || labels.Count == 0) return;
        SetConnectionButtonsEnabled(false);
        if (!await CommandAsync(action, new { labels }))
            SetConnectionButtonsEnabled(true);
    }

    private void SetConnectionButtonsEnabled(bool enabled)
    {
        ConnectSelectedButton.IsEnabled = enabled;
        ConnectAllButton.IsEnabled = enabled;
        ReconnectSelectedButton.IsEnabled = enabled;
        DisconnectSelectedButton.IsEnabled = enabled;
        DisconnectAllButton.IsEnabled = enabled;
    }

    private async void ConnectSelected_Click(object sender, RoutedEventArgs e) => await ConnectionActionAsync("connect", RequireSelected());
    private async void ConnectAll_Click(object sender, RoutedEventArgs e) => await ConnectionActionAsync("connect", AllLabels());
    private async void ReconnectSelected_Click(object sender, RoutedEventArgs e) => await ConnectionActionAsync("reconnect", RequireSelected());
    private async void DisconnectSelected_Click(object sender, RoutedEventArgs e) => await ConnectionActionAsync("disconnect", RequireSelected());
    private async void DisconnectAll_Click(object sender, RoutedEventArgs e) => await ConnectionActionAsync("disconnect", AllLabels());

    private void ShowPanel(Grid panel, Button active)
    {
        MessagesPanel.Visibility = panel == MessagesPanel ? Visibility.Visible : Visibility.Collapsed;
        ScriptsPanel.Visibility = panel == ScriptsPanel ? Visibility.Visible : Visibility.Collapsed;
        CombinationPanel.Visibility = panel == CombinationPanel ? Visibility.Visible : Visibility.Collapsed;
        foreach (Button button in new[] { MessagesTab, ScriptsTab, CombinationTab })
        {
            button.ClearValue(BackgroundProperty);
            button.ClearValue(ForegroundProperty);
        }
        active.Background = (System.Windows.Media.Brush)FindResource("BlueBrush");
        active.Foreground = System.Windows.Media.Brushes.White;
    }

    private void MessagesTab_Click(object sender, RoutedEventArgs e) => ShowPanel(MessagesPanel, MessagesTab);
    private void ScriptsTab_Click(object sender, RoutedEventArgs e) => ShowPanel(ScriptsPanel, ScriptsTab);
    private void CombinationTab_Click(object sender, RoutedEventArgs e) => ShowPanel(CombinationPanel, CombinationTab);

    private async void ParseMessage_Click(object sender, RoutedEventArgs e) =>
        await CommandAsync("parse_message", new { text = MessageInput.Text });

    private async void ChooseMessageFile_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFileDialog { Filter = "消息文件 (*.txt;*.json)|*.txt;*.json|所有文件 (*.*)|*.*" };
        if (dialog.ShowDialog(this) == true)
            await CommandAsync("parse_message_file", new { path = dialog.FileName });
    }

    private async void ClearMessages_Click(object sender, RoutedEventArgs e)
    {
        MessageInput.Clear();
        await CommandAsync("clear_messages");
    }

    private async void SendMessages_Click(object sender, RoutedEventArgs e)
    {
        List<string>? labels = RequireSelected(true);
        if (labels is null) return;
        if (Messages.Count == 0)
        {
            MessageBox.Show(this, "请先解析消息或消息文件。", "发送", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        if (MessageBox.Show(this, $"向 {labels.Count} 个账号发送 {Messages.Count} 条消息？", "确认发送", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
        await CommandAsync("send_messages", new { labels });
    }

    private async void CancelSend_Click(object sender, RoutedEventArgs e) => await CommandAsync("cancel_task", new { kind = "send" });

    private async void RunScript_Click(object sender, RoutedEventArgs e)
    {
        List<string>? labels = RequireSelected(true);
        if (labels is null) return;
        if (ScriptList.SelectedItem is not ScriptItem script)
        {
            MessageBox.Show(this, "请选择一个脚本。", "专用脚本", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        if (MessageBox.Show(this, $"在 {labels.Count} 个账号上运行“{script.Name}”？", "确认运行脚本", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
        await CommandAsync("run_script", new { labels, module = script.Module });
    }

    private async void CancelScript_Click(object sender, RoutedEventArgs e) => await CommandAsync("cancel_task", new { kind = "script" });

    private void AddMessagesStep_Click(object sender, RoutedEventArgs e)
    {
        if (Messages.Count == 0)
        {
            MessageBox.Show(this, "请先在“通用消息”中解析消息。", "组合任务", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        var snapshot = Messages.Select(message => new MessageItem
        {
            Sequence = message.Sequence,
            Id = message.Id,
            Cmd = message.Cmd,
            Param = message.Param.Clone()
        }).ToList();
        string commands = string.Join("、", snapshot.Take(3).Select(item => item.Cmd));
        if (snapshot.Count > 3) commands += "…";
        CombinationSteps.Add(new CombinationStepItem
        {
            Kind = "messages",
            Description = $"通用消息 {snapshot.Count} 条：{commands}",
            Messages = snapshot
        });
    }

    private void AddScriptStep_Click(object sender, RoutedEventArgs e)
    {
        if (CombinationScript.SelectedItem is not ScriptItem script)
        {
            MessageBox.Show(this, "请选择一个脚本。", "组合任务", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        CombinationSteps.Add(new CombinationStepItem { Kind = "script", Description = $"专用脚本：{script.Name}", Script = script });
    }

    private void MoveStep(int offset)
    {
        int index = CombinationList.SelectedIndex;
        int destination = index + offset;
        if (index < 0 || destination < 0 || destination >= CombinationSteps.Count) return;
        CombinationSteps.Move(index, destination);
        CombinationList.SelectedIndex = destination;
    }

    private void MoveStepUp_Click(object sender, RoutedEventArgs e) => MoveStep(-1);
    private void MoveStepDown_Click(object sender, RoutedEventArgs e) => MoveStep(1);
    private void RemoveStep_Click(object sender, RoutedEventArgs e)
    {
        if (CombinationList.SelectedItem is CombinationStepItem item) CombinationSteps.Remove(item);
    }
    private void ClearSteps_Click(object sender, RoutedEventArgs e) => CombinationSteps.Clear();

    private async void RunCombination_Click(object sender, RoutedEventArgs e)
    {
        List<string>? labels = RequireSelected(true);
        if (labels is null) return;
        if (CombinationSteps.Count == 0)
        {
            MessageBox.Show(this, "请先添加组合步骤。", "组合任务", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        if (!int.TryParse(RepetitionsBox.Text, out int repetitions) || repetitions < 1 ||
            !double.TryParse(IntervalBox.Text, out double interval) || interval < 0)
        {
            MessageBox.Show(this, "执行次数必须至少为 1，轮间隔不能小于 0。", "组合任务", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        object[] steps = CombinationSteps.Select(step => step.Kind == "messages"
            ? (object)new
            {
                kind = "messages",
                messages = step.Messages!.Select(message => new { id = message.Id, cmd = message.Cmd, param = message.Param }).ToArray()
            }
            : new { kind = "script", module = step.Script!.Module }).ToArray();
        if (MessageBox.Show(this, $"在 {labels.Count} 个账号上执行完整组合 {repetitions} 次？", "确认组合任务", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
        await CommandAsync("run_combination", new { labels, steps, repetitions, interval });
    }

    private async void CancelCombination_Click(object sender, RoutedEventArgs e) => await CommandAsync("cancel_task", new { kind = "combination" });
    private void ClearLog_Click(object sender, RoutedEventArgs e) => Logs.Clear();

    private async void Window_Closing(object? sender, CancelEventArgs e)
    {
        if (_allowClose) return;
        e.Cancel = true;
        _statusPolling?.Cancel();
        if (_statusPollingTask is not null)
            await _statusPollingTask;
        if (_bridge is not null) await _bridge.DisposeAsync();
        _statusPolling?.Dispose();
        _allowClose = true;
        Close();
    }
}
