using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Text.RegularExpressions;
using System.Threading.Tasks;

namespace VpnHelper.Services;

public partial class FortinetVpnService
{
    private readonly ConfigService _config;
    private readonly LoggerService _logger;
    private readonly StateService _state;
    private readonly TunnelProcessService _tunnel;
    private readonly CookieContainer _cookieContainer;
    private readonly HttpClient _http;

    [GeneratedRegex(@"(?:window\.)?location(?:\.href)?\s*=\s*['""]([^'""]+)['""]", RegexOptions.IgnoreCase)]
    private static partial Regex RegexJsRedirect();

    [GeneratedRegex(@"action\s*=\s*[""']([^""']+)[""']", RegexOptions.IgnoreCase)]
    private static partial Regex RegexActionUrl();

    [GeneratedRegex(@"name=[""']SAMLResponse[""']\s+value=[""']([^""']+)[""']", RegexOptions.IgnoreCase)]
    private static partial Regex RegexSamlResponse();

    [GeneratedRegex(@"name=[""']RelayState[""']\s+value=[""']([^""']+)[""']", RegexOptions.IgnoreCase)]
    private static partial Regex RegexRelayState();

    [GeneratedRegex(@"[?&]id=([^'""&\s]+)", RegexOptions.IgnoreCase)]
    private static partial Regex RegexCallbackId();

    public FortinetVpnService(
        ConfigService config,
        LoggerService logger,
        StateService state,
        TunnelProcessService tunnel)
    {
        _config = config;
        _logger = logger;
        _state = state;
        _tunnel = tunnel;

        _cookieContainer = new CookieContainer();
        var handler = new HttpClientHandler
        {
            CookieContainer = _cookieContainer,
            AllowAutoRedirect = true, // Mengikuti 302 redirect ke SSO dan ACS
            ServerCertificateCustomValidationCallback = (m, cert, chain, errors) =>
            {
                if (!_config.VerifyTls) return true;
                return errors == System.Net.Security.SslPolicyErrors.None;
            }
        };

        _http = new HttpClient(handler)
        {
            Timeout = TimeSpan.FromSeconds(25)
        };
        _http.DefaultRequestHeaders.TryAddWithoutValidation("User-Agent", "Mozilla/5.0 SV1");
        _http.DefaultRequestHeaders.TryAddWithoutValidation("Accept", "*/*");
        _http.DefaultRequestHeaders.TryAddWithoutValidation("Pragma", "no-cache");
    }

    public async Task<bool> PerformHeadlessSsoAsync()
    {
        var username = _config.SsoUsername;
        var password = _config.SsoPassword;

        if (string.IsNullOrEmpty(username) || string.IsNullOrEmpty(password))
        {
            throw new InvalidOperationException("Kredensial SSO (sso_username & sso_password) belum diisi di .env.");
        }

        _logger.AddLog("Memulai autentikasi SSO Fortinet (Headless)...", "info");

        var gatewayUrl = $"https://{_config.VpnHost}:{_config.VpnPort}";
        var startUrl = $"{gatewayUrl}/remote/saml/start?redirect=1";

        // 1. Hubungi Gateway VPN (otomatis redirect ke server SSO IdP)
        var res1 = await _http.GetAsync(startUrl);
        var html1 = await res1.Content.ReadAsStringAsync();

        var jsMatch = RegexJsRedirect().Match(html1);
        if (jsMatch.Success)
        {
            var redirectUrl = jsMatch.Groups[1].Value.Replace("&amp;", "&");
            if (redirectUrl.StartsWith('/'))
            {
                redirectUrl = $"{gatewayUrl}{redirectUrl}";
            }

            _logger.AddLog("Mengikuti JS redirect Gateway ke halaman SSO...", "info", userVisible: false);
            res1 = await _http.GetAsync(redirectUrl);
            html1 = await res1.Content.ReadAsStringAsync();
        }

        // Simpan debug HTML untuk analisis
        try
        {
            var debugFile = Path.Combine(_config.BaseDirectory, "debug_sso.html");
            await File.WriteAllTextAsync(debugFile, html1);
        }
        catch { }

        // Cek jika sesi sudah aktif dan langsung menghasilkan SAMLResponse
        var samlMatch = RegexSamlResponse().Match(html1);
        var acsMatch = RegexActionUrl().Match(html1);
        var relayMatch = RegexRelayState().Match(html1);

        string samlResponse;
        string relayState;
        string acsUrl;

        if (samlMatch.Success && acsMatch.Success)
        {
            _logger.AddLog("Sesi SSO sudah aktif, melanjutkan ke ACS...", "info");
            samlResponse = samlMatch.Groups[1].Value;
            relayState = relayMatch.Success ? relayMatch.Groups[1].Value : "";
            acsUrl = acsMatch.Groups[1].Value.Replace("&amp;", "&");
        }
        else
        {
            // 2. Cari action URL login form Keycloak
            var actionMatch = RegexActionUrl().Match(html1);
            if (!actionMatch.Success)
            {
                throw new InvalidOperationException("Gagal menemukan form login SSO di halaman HTML (periksa debug_sso.html).");
            }

            var actionUrl = actionMatch.Groups[1].Value.Replace("&amp;", "&");
            if (actionUrl.StartsWith('/'))
            {
                var baseUri = res1.RequestMessage?.RequestUri ?? new Uri(gatewayUrl);
                actionUrl = new Uri(baseUri, actionUrl).ToString();
            }

            // 3. POST Kredensial SSO
            _logger.AddLog("Mengirim kredensial autentikasi ke server SSO...", "info");
            var form = new Dictionary<string, string>
            {
                ["username"] = username,
                ["password"] = password,
                ["credentialId"] = ""
            };

            var res2 = await _http.PostAsync(actionUrl, new FormUrlEncodedContent(form));
            var html2 = await res2.Content.ReadAsStringAsync();

            // 4. Ekstrak SAMLResponse dari respon login
            samlMatch = RegexSamlResponse().Match(html2);
            acsMatch = RegexActionUrl().Match(html2);
            relayMatch = RegexRelayState().Match(html2);

            if (!samlMatch.Success || !acsMatch.Success)
            {
                var lower = html2.ToLowerInvariant();
                if (lower.Contains("invalid") || lower.Contains("salah") || lower.Contains("incorrect"))
                {
                    throw new InvalidOperationException("Kredensial login Anda salah.");
                }
                if (lower.Contains("otp") || lower.Contains("2fa") || lower.Contains("authenticator"))
                {
                    throw new InvalidOperationException("Autentikasi OTP/2FA terdeteksi. Silakan gunakan browser manual.");
                }
                throw new InvalidOperationException("Gagal mengekstrak respon SAML dari server autentikasi.");
            }

            samlResponse = samlMatch.Groups[1].Value;
            relayState = relayMatch.Success ? relayMatch.Groups[1].Value : "";
            acsUrl = acsMatch.Groups[1].Value.Replace("&amp;", "&");
        }

        if (acsUrl.StartsWith('/'))
        {
            acsUrl = $"{gatewayUrl}{acsUrl}";
        }

        // 5. POST SAMLResponse ke ACS Gateway
        _logger.AddLog("Verifikasi respon SAML dengan gateway...", "info");
        var acsForm = new Dictionary<string, string>
        {
            ["SAMLResponse"] = samlResponse,
            ["RelayState"] = relayState
        };

        var res3 = await _http.PostAsync(acsUrl, new FormUrlEncodedContent(acsForm));
        var html3 = await res3.Content.ReadAsStringAsync();

        // 6. Ekstrak ID dan tukar dengan cookie sesi
        var finalUrl = res3.RequestMessage?.RequestUri?.ToString() ?? "";
        var idMatch = RegexCallbackId().Match(finalUrl);
        if (!idMatch.Success)
        {
            idMatch = RegexCallbackId().Match(html3);
        }

        if (!idMatch.Success && !string.IsNullOrEmpty(_state.FortinetAuthId))
        {
            idMatch = RegexCallbackId().Match($"?id={_state.FortinetAuthId}");
        }

        if (!idMatch.Success)
        {
            throw new InvalidOperationException("Tidak menemukan ID autentikasi dari respon ACS Gateway.");
        }

        var authId = idMatch.Groups[1].Value;
        _state.FortinetAuthId = authId;

        _state.SsoStatus = "Terautentikasi";
        _state.SsoMeta = "Sesi SAML valid.";
        _state.GatewayStatus = "Menyiapkan Terowongan";
        _state.GatewayMeta = "Menukar cookie gateway...";

        var cookie = await ExchangeAuthIdForCookieAsync(authId);
        _state.FortinetSessionCookie = cookie;

        _state.GatewayStatus = "Siap";
        _state.GatewayMeta = "Cookie sesi gateway aktif.";
        _state.VpnStatus = "Menghubungkan...";
        _state.VpnMeta = "Memulai openconnect...";

        _logger.AddLog("Autentikasi SSO berhasil! Menyiapkan terowongan...", "success");

        await _tunnel.StartTunnelAsync(
            "fortinet",
            $"{_config.VpnHost}:{_config.VpnPort}",
            cookie
        );

        return true;
    }

    public async Task<string> ExchangeAuthIdForCookieAsync(string authId)
    {
        var gatewayUrl = $"https://{_config.VpnHost}:{_config.VpnPort}";
        var authUrl = $"{gatewayUrl}/remote/saml/auth_id?id={Uri.EscapeDataString(authId)}";

        var res = await _http.GetAsync(authUrl);

        var cookies = _cookieContainer.GetCookies(new Uri(gatewayUrl));
        var svpn = cookies["SVPNCOOKIE"]?.Value;

        if (string.IsNullOrEmpty(svpn) && res.Headers.TryGetValues("Set-Cookie", out var setCookies))
        {
            foreach (var sc in setCookies)
            {
                if (sc.Contains("SVPNCOOKIE="))
                {
                    svpn = sc.Split("SVPNCOOKIE=", 2)[1].Split(';')[0].Trim();
                    break;
                }
            }
        }

        if (string.IsNullOrEmpty(svpn))
        {
            throw new InvalidOperationException("SVPNCOOKIE tidak ditemukan dari respon gateway.");
        }

        return svpn;
    }
}