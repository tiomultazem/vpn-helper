using System.Text.Json.Serialization;

namespace VpnHelper.Models;

public class GpSession
{
    [JsonPropertyName("authcookie")]
    public string AuthCookie { get; set; } = string.Empty;

    [JsonPropertyName("username")]
    public string Username { get; set; } = string.Empty;

    [JsonPropertyName("gateway")]
    public string Gateway { get; set; } = string.Empty;

    [JsonPropertyName("portal_name")]
    public string PortalName { get; set; } = string.Empty;

    [JsonPropertyName("expires_at")]
    public double ExpiresAt { get; set; }

    [JsonIgnore]
    public bool IsExpired => DateTimeOffset.UtcNow.ToUnixTimeSeconds() >= (long)ExpiresAt;
}
