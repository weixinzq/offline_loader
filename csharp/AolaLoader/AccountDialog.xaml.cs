using System.Windows;

namespace AolaLoader;

public partial class AccountDialog : Window
{
    public AccountForm Value { get; }

    public AccountDialog(AccountForm value, bool editing)
    {
        InitializeComponent();
        Value = value;
        Heading.Text = editing ? "编辑账号" : "新增账号";
        PasswordLabel.Text = editing ? "密码（留空表示不修改）" : "密码";
        LabelBox.Text = value.Label;
        AccountBox.Text = value.Account;
        CharIdBox.Text = value.CharId.ToString();
        ZoneBox.Text = value.ZoneIndex.ToString();
    }

    private void Save_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(LabelBox.Text) || string.IsNullOrWhiteSpace(AccountBox.Text))
        {
            MessageBox.Show(this, "账号标签和登录账号不能为空。", "账号", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        if (!int.TryParse(ZoneBox.Text, out int zone) || zone <= 0)
        {
            MessageBox.Show(this, "区服编号必须是正整数。", "账号", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        if (!int.TryParse(CharIdBox.Text, out int charId) || charId < 0)
        {
            MessageBox.Show(this, "角色编号 charId 必须是非负整数。", "账号", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        Value.Label = LabelBox.Text.Trim();
        Value.Account = AccountBox.Text.Trim();
        Value.Password = PasswordInput.Password;
        Value.CharId = charId;
        Value.ZoneIndex = zone;
        DialogResult = true;
    }

    private void Cancel_Click(object sender, RoutedEventArgs e) => DialogResult = false;
}
