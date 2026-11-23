namespace VpnHelper.Models;

public record LogEntry(
    string Time,
    string Message,
    string Level
)
{
    public string FormattedText => $"[{Time}] [{Level.ToUpper()}] {Message}";
}
