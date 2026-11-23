using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace VpnHelper.Services;

public class ConfigService
{
    private readonly Dictionary<string, string> _envVars = new(StringComparer.OrdinalIgnoreCase);
    public string BaseDirectory { get; }

    public static string ResolveBaseDirectory()
    {
        var dir = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
        while (dir != null)
        {
            if (File.Exists(Path.Combine(dir.FullName, ".env")) || File.Exists(Path.Combine(dir.FullName, ".env.enc")))
            {
                return dir.FullName;
            }
            dir = dir.Parent;
        }

        return AppDomain.CurrentDomain.BaseDirectory;
    }

    public ConfigService()
    {
        BaseDirectory = ResolveBaseDirectory();
        InitConfiguration();
    }

    private void InitConfiguration()
    {
#if MITRA_BUILD
        // 1. Versi Mitra: Dekripsi payload terenkripsi AES langsung ke memori (RAM)
        var decryptedEnv = FernetDecryptor.GetMitraDecryptedEnv();
        if (!string.IsNullOrWhiteSpace(decryptedEnv))
        {
            ParseEnvString(decryptedEnv);
        }

        // Jika terdapat file .env fisik di folder kerja, izinkan untuk menimpa konfigurasi
        LoadDotEnv();
#else
        // 2. Versi Production: Memuat file fisik .env dari disk
        LoadDotEnv();
#endif
    }

    private void ParseEnvString(string content)
    {
        using var reader = new StringReader(content);
        string? line;
        while ((line = reader.ReadLine()) != null)
        {
            var trimmed = line.Trim();
            if (string.IsNullOrWhiteSpace(trimmed) || trimmed.StartsWith('#') || !trimmed.Contains('='))
            {
                continue;
            }

            var parts = trimmed.Split('=', 2);
            var key = parts[0].Trim();
            var val = parts[1].Trim().Trim('"', '\'');
            _envVars[key] = val;
        }
    }

    private void LoadDotEnv()
    {
        var envPath = Path.Combine(BaseDirectory, ".env");
        if (!File.Exists(envPath))
        {
            return;
        }

        foreach (var line in File.ReadAllLines(envPath, Encoding.UTF8))
        {
            var trimmed = line.Trim();
            if (string.IsNullOrWhiteSpace(trimmed) || trimmed.StartsWith('#') || !trimmed.Contains('='))
            {
                continue;
            }

            var parts = trimmed.Split('=', 2);
            var key = parts[0].Trim();
            var val = parts[1].Trim().Trim('"', '\'');
            _envVars[key] = val;
        }
    }

    public string Get(string key, string defaultValue = "")
    {
        return _envVars.TryGetValue(key, out var val) ? val : defaultValue;
    }

    public int GetInt(string key, int defaultValue = 0)
    {
        var val = Get(key);
        return int.TryParse(val, out var res) ? res : defaultValue;
    }

    public bool GetBool(string key, bool defaultValue = false)
    {
        var val = Get(key).ToLowerInvariant();
        if (string.IsNullOrEmpty(val)) return defaultValue;
        return val is "true" or "1" or "yes" or "on";
    }

    public string[] GetList(string key, string defaultValue = "", char separator = ';')
    {
        var val = Get(key, defaultValue);
        return val.Split(separator, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
    }

    // Properti Umum
    public string AppHost => Get("app_host", "127.0.0.1");
    public int AppPort => GetInt("app_port", 5000);
    public bool AppDebug => GetBool("app_debug", false);
    public string OpenConnectPath => Get("openconnect_path", "");
    public string[] OpenConnectSearchPaths => GetList("openconnect_search_paths", "C:\\Program Files\\OpenConnect-GUI\\openconnect.exe;C:\\Program Files (x86)\\OpenConnect-GUI\\openconnect.exe;C:\\Program Files\\OpenConnect\\openconnect.exe");
    public string TunnelInterface => Get("tunnel_interface", "VPN Helper");
    public bool VerifyTls => GetBool("verify_tls", false);

    // Konfigurasi Fortinet
    public string VpnHost => Get("vpn_host", "akses.bps.go.id");
    public int VpnPort => GetInt("vpn_port", 443);
    public string SsoUsername => Get("sso_username", "");
    public string SsoPassword => Get("sso_password", "");

    // Konfigurasi GlobalProtect
    public string GpHost => Get("gp_host", "vpn.bps.go.id");
    public int GpPort => GetInt("gp_port", 443);
    public string GpGateway => Get("gp_gateway", "vpn-gw01.bps.go.id");

    // Konfigurasi Split-Tunnel Bypass
    public bool BypassEnabled => GetBool("bypass_enabled", true);
    public string[] BypassDomains => GetList("bypass_domains", "rs-ny.rustdesk.com;https://d21.team;*.google.com;*.youtube.com;*.cloudflare.com;*.github.com");
    public string[] BypassSubnets => GetList("bypass_subnets", "10.0.0.0/8;172.16.0.0/12;192.168.0.0/16");
}
