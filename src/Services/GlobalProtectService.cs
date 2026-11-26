using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Xml.Linq;
using VpnHelper.Models;

namespace VpnHelper.Services;

public partial class GlobalProtectService
{
    private readonly ConfigService _config;
    private readonly LoggerService _logger;
    private readonly StateService _state;
    private readonly TunnelProcessService _tunnel;
    private readonly HttpClient _http;
    private readonly string _sessionFilePath;

    public GlobalProtectService(
        ConfigService config,
        LoggerService logger,
        StateService state,
        TunnelProcessService tunnel)
    {
        _config = config;
        _logger = logger;
        _state = state;
        _tunnel = tunnel;
        _sessionFilePath = Path.Combine(_config.BaseDirectory, ".gp_session.json");

        var handler = new HttpClientHandler
        {
            ServerCertificateCustomValidationCallback = (m, cert, chain, errors) =>
            {
                if (!_config.VerifyTls) return true;
                return errors == System.Net.Security.SslPolicyErrors.None;
            }
        };

        _http = new HttpClient(handler)
        {
            Timeout = TimeSpan.FromSeconds(15)
        };
        _http.DefaultRequestHeaders.UserAgent.ParseAdd("PAN GlobalProtect/6.0.7-14 (Windows 10)");
    }

    public async Task<bool> TryResumeSessionAsync()
    {
        if (!File.Exists(_sessionFilePath))
        {
            return false;
        }

        GpSession? session;
        try
        {
            var json = await File.ReadAllTextAsync(_sessionFilePath);
            session = JsonSerializer.Deserialize<GpSession>(json);
        }
        catch
        {
            return false;
        }

        if (session == null || session.IsExpired || string.IsNullOrEmpty(session.AuthCookie))
        {
            ClearSession();
            return false;
        }

        var gateway = string.IsNullOrEmpty(session.Gateway) ? _config.GpGateway : session.Gateway;
        var username = string.IsNullOrEmpty(session.Username) ? _config.SsoUsername : session.Username;

        _logger.AddLog($"Sesi GP tersimpan ditemukan ({username}). Menguji resume...", "info");

        var url = $"https://{gateway}:{_config.GpPort}/ssl-vpn/login.esp";
        var form = new Dictionary<string, string>
        {
            ["user"] = username,
            ["passwd"] = "",
            ["clientVer"] = "4100",
            ["clientos"] = "Windows",
            ["clientgpversion"] = "6.3.3-1121",
            ["portal-userauthcookie"] = session.AuthCookie,
            ["computer"] = Environment.MachineName,
            ["server"] = gateway
        };

        try
        {
            var res = await _http.PostAsync(url, new FormUrlEncodedContent(form));
            var xmlText = await res.Content.ReadAsStringAsync();
            var doc = XDocument.Parse(xmlText);
            var newAuthCookie = doc.Root?.Element("authcookie")?.Value ?? "";

            if (!string.IsNullOrEmpty(newAuthCookie) && !newAuthCookie.Equals("empty", StringComparison.OrdinalIgnoreCase))
            {
                _logger.AddLog("Resume sesi GP berhasil. Memulai terowongan...", "success");
                _state.GpResumedSession = true;
                _state.GpUsername = username;
                _state.GpToken = newAuthCookie;

                await _tunnel.StartTunnelAsync(
                    "gp",
                    $"{gateway}:{_config.GpPort}",
                    newAuthCookie,
                    username,
                    "gateway:prelogin-cookie"
                );
                return true;
            }
        }
        catch (Exception ex)
        {
            _logger.AddLog($"Resume sesi GP gagal: {ex.Message}", "warning");
        }

        ClearSession();
        return false;
    }

    public async Task<string> GetPreloginSamlRequestHtmlAsync()
    {
        var gateway = _config.GpGateway;
        var url = $"https://{gateway}:{_config.GpPort}/ssl-vpn/prelogin.esp?tmp=tmp&clientVer=4100&clientos=Windows";

        var res = await _http.PostAsync(url, new StringContent(string.Empty));
        res.EnsureSuccessStatusCode();

        var xmlText = await res.Content.ReadAsStringAsync();
        var doc = XDocument.Parse(xmlText);
        var b64Saml = doc.Root?.Element("saml-request")?.Value;

        if (string.IsNullOrEmpty(b64Saml))
        {
            throw new InvalidOperationException("Respon prelogin tidak mengandung saml-request.");
        }

        var htmlBytes = Convert.FromBase64String(b64Saml);
        return Encoding.UTF8.GetString(htmlBytes);
    }

    public void SaveSession()
    {
        var cookie = _state.GpAuthCookie;
        if (string.IsNullOrEmpty(cookie)) return;

        var session = new GpSession
        {
            AuthCookie = cookie,
            Username = _state.GpUsername ?? _config.SsoUsername,
            Gateway = _config.GpGateway,
            PortalName = _state.GpPortal ?? _config.GpHost,
            ExpiresAt = DateTimeOffset.UtcNow.AddHours(72).ToUnixTimeSeconds()
        };

        try
        {
            var json = JsonSerializer.Serialize(session, new JsonSerializerOptions { WriteIndented = true });
            File.WriteAllText(_sessionFilePath, json, Encoding.UTF8);
            _logger.AddLog("Sesi GP disimpan (berlaku 72 jam).", "info", userVisible: false);
        }
        catch (Exception ex)
        {
            _logger.AddLog($"Gagal menyimpan sesi GP: {ex.Message}", "warning", userVisible: false);
        }
    }

    public void ClearSession()
    {
        try
        {
            if (File.Exists(_sessionFilePath)) File.Delete(_sessionFilePath);
        }
        catch { }
    }

    public async Task SubmitHipReportAsync(string assignedIp)
    {
        if (string.IsNullOrEmpty(assignedIp)) return;

        SaveSession();

        var authcookie = _state.GpAuthCookie ?? _state.GpToken ?? "";
        var username = _state.GpUsername ?? _config.SsoUsername;
        var gateway = _config.GpGateway;
        var portal = _state.GpPortal ?? _config.GpHost;
        var computer = Environment.MachineName;
        var hipMd5 = _state.GpHipMd5 ?? "6ec16d97df2ab20bc2f32eb117ffce";
        var nowStr = DateTime.Now.ToString("MM/dd/yyyy HH:mm:ss");

        var reportXml = $@"<?xml version=""1.0"" encoding=""UTF-8""?>
<hip-report>
	<md5-sum>{hipMd5}</md5-sum>
	<user-name>{username}</user-name>
	<domain></domain>
	<host-name>{computer}</host-name>
	<host-id>c880a7d0-a9e5-4a2f-8559-062e63600fba</host-id>
	<ip-address>{assignedIp}</ip-address>
	<generate-time>{nowStr}</generate-time>
	<hip-report-version>4</hip-report-version>
	<categories>
		<entry name=""host-info"">
			<client-version>6.3.3-1121</client-version>
			<os>Microsoft Windows 11 Home Single Language , 64-bit</os>
			<os-vendor>Microsoft</os-vendor>
			<domain></domain>
			<host-name>{computer}</host-name>
			<host-id>c880a7d0-a9e5-4a2f-8559-062e63600fba</host-id>
		</entry>
	</categories>
</hip-report>";

        var url = $"https://{gateway}:{_config.GpPort}/ssl-vpn/hipreport.esp";
        var form = new Dictionary<string, string>
        {
            ["user"] = username,
            ["domain"] = "",
            ["portal"] = portal,
            ["authcookie"] = authcookie,
            ["client-ip"] = assignedIp,
            ["computer"] = computer,
            ["md5"] = hipMd5,
            ["client-role"] = "global-protect-full",
            ["report"] = reportXml
        };

        try
        {
            var res = await _http.PostAsync(url, new FormUrlEncodedContent(form));
            var respText = await res.Content.ReadAsStringAsync();

            if (respText.Contains("status=\"success\"", StringComparison.OrdinalIgnoreCase) ||
                respText.Contains("<response status='success'>", StringComparison.OrdinalIgnoreCase))
            {
                _logger.AddLog("Laporan HIP berhasil dikirim ke gateway. Akses internal dibuka.", "success");
            }
            else
            {
                _logger.AddLog($"Laporan HIP terkirim dengan status: {res.StatusCode}", "warning", userVisible: false);
            }
        }
        catch (Exception ex)
        {
            _logger.AddLog($"Gagal mengirim laporan HIP: {ex.Message}", "warning");
        }
    }
}
