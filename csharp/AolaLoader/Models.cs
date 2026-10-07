using System.Text.Json;

namespace AolaLoader;

public sealed class AccountItem
{
    public required string Label { get; init; }
    public string Status { get; init; } = "离线";
}

public sealed class ScriptItem
{
    public required string Module { get; init; }
    public required string Name { get; init; }
    public string Description { get; init; } = "";
}

public sealed class MessageItem
{
    public int Sequence { get; init; }
    public int Id { get; init; }
    public string ExtensionId => Id < 0 ? "—" : Id.ToString();
    public required string Cmd { get; init; }
    public JsonElement Param { get; init; }
}

public sealed class CombinationStepItem
{
    public required string Kind { get; init; }
    public required string Description { get; init; }
    public IReadOnlyList<MessageItem>? Messages { get; init; }
    public ScriptItem? Script { get; init; }
}

public sealed class LogItem
{
    public required string Time { get; init; }
    public required string Message { get; init; }
    public string Category { get; init; } = "info";
}

public sealed class AccountForm
{
    public string Label { get; set; } = "";
    public string Account { get; set; } = "";
    public string Password { get; set; } = "";
    public int CharId { get; set; }
    public int ZoneIndex { get; set; } = 1025;
}
