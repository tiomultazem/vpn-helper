using System;
using System.Diagnostics;
using System.Linq;

namespace VpnHelper.Services;

public class DnsManagementService
{
    private readonly LoggerService _logger;
    private readonly StateService _state;

    public DnsManagementService(LoggerService logger, StateService state)
    {
        _logger = logger;
        _state = state;
    }

    public void EnforceDnsOnPhysicalAdapter(string[]? dnsServers = null)
    {
        dnsServers ??= ["10.10.11.11", "10.10.11.12"];

        try
        {
            var psFind = "Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1 -ExpandProperty InterfaceAlias";
            var iface = RunPowerShell(psFind)?.Trim();

            if (string.IsNullOrEmpty(iface) || iface.Contains("VPN Helper", StringComparison.OrdinalIgnoreCase))
            {
                var psFallback = "Get-NetAdapter | Where-Object { $_.Status -eq 'Up' -and $_.InterfaceDescription -notmatch 'Wintun|OpenConnect|Virtual|Loopback' } | Select-Object -First 1 -ExpandProperty Name";
                iface = RunPowerShell(psFallback)?.Trim();
            }

            if (string.IsNullOrEmpty(iface)) return;

            var psGetDns = $"(Get-DnsClientServerAddress -InterfaceAlias '{iface}' -AddressFamily IPv4).ServerAddresses -join ','";
            var oldDns = RunPowerShell(psGetDns)?.Trim();

            _state.PhysicalAdapterName = iface;
            _state.PhysicalAdapterOldDns = oldDns;

            var psSet = $"Set-DnsClientServerAddress -InterfaceAlias '{iface}' -ServerAddresses {string.Join(',', dnsServers)}; Clear-DnsClientCache";
            RunPowerShell(psSet);

            _logger.AddLog($"DNS pada '{iface}' dialihkan ke {string.Join(',', dnsServers)}.", "info");
        }
        catch (Exception ex)
        {
            _logger.AddLog($"Gagal mengalihkan DNS: {ex.Message}", "warning", userVisible: false);
        }
    }

    public void RestorePhysicalAdapterDns()
    {
        var iface = _state.PhysicalAdapterName;
        var oldDns = _state.PhysicalAdapterOldDns;

        if (string.IsNullOrEmpty(iface)) return;

        try
        {
            string psRestore;
            if (!string.IsNullOrEmpty(oldDns))
            {
                var formatted = string.Join(',', oldDns.Split(',').Select(d => $"\"{d}\""));
                psRestore = $"Set-DnsClientServerAddress -InterfaceAlias '{iface}' -ServerAddresses {formatted}; Clear-DnsClientCache";
            }
            else
            {
                psRestore = $"Set-DnsClientServerAddress -InterfaceAlias '{iface}' -ResetServerAddresses; Clear-DnsClientCache";
            }

            RunPowerShell(psRestore);
            _logger.AddLog($"DNS pada '{iface}' dikembalikan ke semula.", "info");
        }
        catch (Exception ex)
        {
            _logger.AddLog($"Gagal mengembalikan DNS: {ex.Message}", "warning", userVisible: false);
        }
        finally
        {
            _state.PhysicalAdapterName = null;
            _state.PhysicalAdapterOldDns = null;
        }
    }

    private static string? RunPowerShell(string command)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = "powershell",
                Arguments = $"-NoProfile -NonInteractive -Command \"{command}\"",
                RedirectStandardOutput = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            using var proc = Process.Start(psi);
            var output = proc?.StandardOutput.ReadToEnd();
            proc?.WaitForExit(5000);
            return output;
        }
        catch
        {
            return null;
        }
    }
}
