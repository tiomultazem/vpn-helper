using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using DnsClient;
using VpnHelper.Interop;

namespace VpnHelper.Services;

public partial class RoutingBypassService
{
    private readonly ConfigService _config;
    private readonly LoggerService _logger;
    private readonly StateService _state;
    private readonly HttpClient _http = new();
    private readonly LookupClient _dnsClient;
    private readonly HashSet<string> _activeRoutes = new();
    private readonly object _lock = new();

    private CancellationTokenSource? _periodicCts;

    [GeneratedRegex(@"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(/\d+)?$")]
    private static partial Regex RegexIpv4Cidr();

    public RoutingBypassService(ConfigService config, LoggerService logger, StateService state)
    {
        _config = config;
        _logger = logger;
        _state = state;

        var options = new LookupClientOptions(IPAddress.Parse("1.1.1.1"), IPAddress.Parse("8.8.8.8"))
        {
            Timeout = TimeSpan.FromSeconds(3)
        };
        _dnsClient = new LookupClient(options);
    }

    public void StartPeriodicRefresh()
    {
        StopPeriodicRefresh();
        _periodicCts = new CancellationTokenSource();

        _ = Task.Run(async () =>
        {
            await ApplyBypassRoutesAsync();

            while (!_periodicCts.Token.IsCancellationRequested)
            {
                try
                {
                    await Task.Delay(TimeSpan.FromMinutes(10), _periodicCts.Token);
                    if (_state.IsConnected && _config.BypassEnabled)
                    {
                        await ApplyBypassRoutesAsync();
                    }
                }
                catch (OperationCanceledException) { break; }
                catch (Exception ex)
                {
                    _logger.AddLog($"Bypass refresh error: {ex.Message}", "warning", userVisible: false);
                }
            }
        });
    }

    public void StopPeriodicRefresh()
    {
        _periodicCts?.Cancel();
        CleanupRoutes();
    }

    public async Task ApplyBypassRoutesAsync()
    {
        if (!_config.BypassEnabled)
        {
            CleanupRoutes();
            return;
        }

        var (gateway, ifIndex) = GetPhysicalGatewayInfo();
        if (string.IsNullOrEmpty(gateway))
        {
            _logger.AddLog("Bypass warning: Gateway fisik tidak ditemukan. Rute bypass dilewati.", "warning");
            return;
        }

        var resolvedTargets = new Dictionary<string, string>(); // ip_or_cidr -> domain

        foreach (var domain in _config.BypassDomains)
        {
            if (domain.StartsWith("*."))
            {
                var cidrs = await FetchWildcardCidrsAsync(domain);
                foreach (var cidr in cidrs)
                {
                    resolvedTargets[cidr] = domain;
                }
            }
            else
            {
                var ips = await ResolveDomainPublicAsync(domain);
                foreach (var ip in ips)
                {
                    resolvedTargets[ip] = domain;
                }
            }
        }

        lock (_lock)
        {
            // Hapus rute yang tidak lagi valid
            foreach (var existing in _activeRoutes.ToList())
            {
                if (!resolvedTargets.ContainsKey(existing))
                {
                    DeleteRoute(existing);
                    _activeRoutes.Remove(existing);
                }
            }

            // Pasang rute baru
            foreach (var (target, domain) in resolvedTargets)
            {
                if (!_activeRoutes.Contains(target))
                {
                    if (AddRoute(target, gateway, ifIndex))
                    {
                        _activeRoutes.Add(target);
                        _logger.AddLog($"Bypass dipasang: {target} via {gateway}", "info", userVisible: false);
                    }
                }
            }
        }

        UpdateHostsFile(resolvedTargets.Where(kv => !kv.Key.Contains('/')).ToDictionary(kv => kv.Key, kv => kv.Value));
    }

    private async Task<List<string>> FetchWildcardCidrsAsync(string pattern)
    {
        var result = new List<string>();

        try
        {
            if (pattern.Contains("google") || pattern.Contains("youtube"))
            {
                var json = await _http.GetStringAsync("https://www.gstatic.com/ipranges/goog.json");
                var node = JsonNode.Parse(json);
                var prefixes = node?["prefixes"]?.AsArray();
                if (prefixes != null)
                {
                    foreach (var p in prefixes)
                    {
                        var cidr = p?["ipv4Prefix"]?.GetValue<string>();
                        if (!string.IsNullOrEmpty(cidr)) result.Add(cidr);
                    }
                }
            }
            else if (pattern.Contains("cloudflare"))
            {
                var text = await _http.GetStringAsync("https://www.cloudflare.com/ips-v4");
                foreach (var line in text.Split('\n'))
                {
                    var clean = line.Trim();
                    if (RegexIpv4Cidr().IsMatch(clean)) result.Add(clean);
                }
            }
        }
        catch (Exception ex)
        {
            _logger.AddLog($"Gagal unduh CIDR {pattern}: {ex.Message}", "warning", userVisible: false);
        }

        return result.Distinct().ToList();
    }

    private async Task<List<string>> ResolveDomainPublicAsync(string domain)
    {
        var ips = new List<string>();
        try
        {
            var res = await _dnsClient.QueryAsync(domain, QueryType.A);
            foreach (var record in res.Answers.ARecords())
            {
                var ip = record.Address.ToString();
                if (!IsPrivateIp(ip) && !ips.Contains(ip))
                {
                    ips.Add(ip);
                }
            }
        }
        catch { }
        return ips;
    }

    private static bool IsPrivateIp(string ip)
    {
        if (IPAddress.TryParse(ip, out var addr))
        {
            var bytes = addr.GetAddressBytes();
            if (bytes[0] == 10) return true;
            if (bytes[0] == 172 && bytes[1] >= 16 && bytes[1] <= 31) return true;
            if (bytes[0] == 192 && bytes[1] == 168) return true;
            if (bytes[0] == 127) return true;
        }
        return false;
    }

    private (string? Gateway, uint? IfIndex) GetPhysicalGatewayInfo()
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = "powershell",
                Arguments = "-NoProfile -Command \"Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1 NextHop, InterfaceIndex | ConvertTo-Json\"",
                RedirectStandardOutput = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            using var proc = Process.Start(psi);
            var json = proc?.StandardOutput.ReadToEnd();
            proc?.WaitForExit(3000);

            if (!string.IsNullOrEmpty(json))
            {
                var node = JsonNode.Parse(json);
                var gw = node?["NextHop"]?.GetValue<string>()?.Trim();
                var idx = node?["InterfaceIndex"]?.GetValue<uint>();
                if (!string.IsNullOrEmpty(gw) && gw != "0.0.0.0")
                {
                    return (gw, idx);
                }
            }
        }
        catch { }

        return (null, null);
    }

    private bool AddRoute(string target, string gateway, uint? ifIndex)
    {
        try
        {
            string mask = "255.255.255.255";
            string net = target;

            if (target.Contains('/'))
            {
                var parts = target.Split('/');
                net = parts[0];
                int prefix = int.Parse(parts[1]);
                uint maskInt = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF;
                mask = $"{(maskInt >> 24) & 0xFF}.{(maskInt >> 16) & 0xFF}.{(maskInt >> 8) & 0xFF}.{maskInt & 0xFF}";
            }

            var args = ifIndex.HasValue
                ? $"add {net} mask {mask} {gateway} metric 5 if {ifIndex.Value}"
                : $"add {net} mask {mask} {gateway} metric 5";

            var psi = new ProcessStartInfo("route", args) { CreateNoWindow = true, UseShellExecute = false };
            using var p = Process.Start(psi);
            p?.WaitForExit(2000);
            return p?.ExitCode == 0;
        }
        catch
        {
            return false;
        }
    }

    private void DeleteRoute(string target)
    {
        try
        {
            string net = target;
            string? mask = null;

            if (target.Contains('/'))
            {
                var parts = target.Split('/');
                net = parts[0];
                int prefix = int.Parse(parts[1]);
                uint maskInt = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF;
                mask = $"{(maskInt >> 24) & 0xFF}.{(maskInt >> 16) & 0xFF}.{(maskInt >> 8) & 0xFF}.{maskInt & 0xFF}";
            }

            var args = mask != null ? $"delete {net} mask {mask}" : $"delete {net}";
            var psi = new ProcessStartInfo("route", args) { CreateNoWindow = true, UseShellExecute = false };
            using var p = Process.Start(psi);
            p?.WaitForExit(2000);
        }
        catch { }
    }

    public void CleanupRoutes()
    {
        lock (_lock)
        {
            foreach (var r in _activeRoutes)
            {
                DeleteRoute(r);
            }
            _activeRoutes.Clear();
        }
        CleanupHostsFile();
    }

    private static void UpdateHostsFile(Dictionary<string, string> domainIpMap)
    {
        var hostsPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), @"drivers\etc\hosts");
        if (!File.Exists(hostsPath)) return;

        try
        {
            var lines = File.ReadAllLines(hostsPath).Where(l => !l.Contains("# VPN Helper Bypass")).ToList();
            foreach (var (ip, dom) in domainIpMap)
            {
                lines.Add($"{ip}\t{dom}\t# VPN Helper Bypass");
            }
            File.WriteAllLines(hostsPath, lines);
        }
        catch { }
    }

    private static void CleanupHostsFile()
    {
        var hostsPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), @"drivers\etc\hosts");
        if (!File.Exists(hostsPath)) return;

        try
        {
            var lines = File.ReadAllLines(hostsPath).Where(l => !l.Contains("# VPN Helper Bypass")).ToList();
            File.WriteAllLines(hostsPath, lines);
        }
        catch { }
    }
}
