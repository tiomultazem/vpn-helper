using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace VpnHelper.Services;

public partial class TunnelProcessService
{
    private readonly ConfigService _config;
    private readonly LoggerService _logger;
    private readonly StateService _state;
    private Process? _tunnelProcess;
    private CancellationTokenSource? _outputCts;

    [GeneratedRegex(@"(?:Configured as|Got Legacy IP address)\s+([0-9.]+)")]
    private static partial Regex RegexConfiguredIp();

    [GeneratedRegex(@"authcookie=([a-f0-9]{16,})", RegexOptions.IgnoreCase)]
    private static partial Regex RegexAuthCookieDump();

    [GeneratedRegex(@"<authcookie>(.*?)</authcookie>", RegexOptions.IgnoreCase)]
    private static partial Regex RegexAuthCookieXml();

    [GeneratedRegex(@"<portal>(.*?)</portal>", RegexOptions.IgnoreCase)]
    private static partial Regex RegexPortalName();

    [GeneratedRegex(@"md5sum\s+([a-f0-9]{16,})", RegexOptions.IgnoreCase)]
    private static partial Regex RegexHipMd5();

    public event Action<string>? OnIpAssigned;
    public event Action? OnRouteConfigured;
    public event Action? OnCookieRejected;
    public event Action<string>? OnDeadPeer;
    public event Action<string>? OnTunnelStopped;

    public TunnelProcessService(ConfigService config, LoggerService logger, StateService state)
    {
        _config = config;
        _logger = logger;
        _state = state;
    }

    public string? FindTunnelEngine()
    {
        var custom = _config.OpenConnectPath;
        if (!string.IsNullOrEmpty(custom) && File.Exists(custom))
        {
            return custom;
        }

        // Cek direktori pencarian standar
        foreach (var path in _config.OpenConnectSearchPaths)
        {
            if (File.Exists(path))
            {
                return path;
            }
        }

        // Cek PATH environment
        var envPath = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (var dir in envPath.Split(Path.PathSeparator))
        {
            var candidate = Path.Combine(dir, "openconnect.exe");
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        return null;
    }

    public async Task<bool> StartTunnelAsync(
        string protocol,
        string target,
        string tokenOrCookie,
        string? username = null,
        string? usergroup = null)
    {
        StopTunnel();

        var enginePath = FindTunnelEngine();
        if (string.IsNullOrEmpty(enginePath))
        {
            throw new FileNotFoundException("Biner openconnect.exe tidak ditemukan di sistem.");
        }

        var sb = new StringBuilder();
        sb.Append($"--protocol={protocol} ");
        sb.Append($"--interface=\"{_config.TunnelInterface}\" ");
        sb.Append("--non-inter ");

        if (protocol.Equals("gp", StringComparison.OrdinalIgnoreCase))
        {
            sb.Append("--os=win --passwd-on-stdin --dump-http-traffic ");
            if (!string.IsNullOrEmpty(username)) sb.Append($"--user=\"{username}\" ");
            if (!string.IsNullOrEmpty(usergroup)) sb.Append($"--usergroup=\"{usergroup}\" ");
            sb.Append($"--local-hostname=\"{Environment.MachineName}\" ");
        }
        else
        {
            sb.Append("--cookie-on-stdin --no-passwd --no-dtls ");
        }

        sb.Append(target);

        var startInfo = new ProcessStartInfo
        {
            FileName = enginePath,
            Arguments = sb.ToString(),
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };

        _tunnelProcess = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
        _tunnelProcess.Exited += (s, e) =>
        {
            var code = 0;
            try { code = _tunnelProcess?.ExitCode ?? 0; } catch { }
            _state.IsConnected = false;
            _state.IsConnecting = false;
            _state.TunnelPid = null;
            _logger.AddLog($"Proses terowongan terhenti (exit code {code}).", "warning");
            OnTunnelStopped?.Invoke($"Proses tunnel selesai dengan exit code {code}");
        };

        _tunnelProcess.Start();
        _state.TunnelPid = _tunnelProcess.Id;
        _state.IsConnecting = true;
        _logger.AddLog($"Proses OpenConnect dimulai (PID {_tunnelProcess.Id}).", "info");

        // Tulis token/cookie ke STDIN
        using (var writer = _tunnelProcess.StandardInput)
        {
            if (protocol.Equals("gp", StringComparison.OrdinalIgnoreCase))
            {
                await writer.WriteLineAsync(tokenOrCookie);
            }
            else
            {
                await writer.WriteLineAsync($"SVPNCOOKIE={tokenOrCookie}");
            }
        }

        _outputCts = new CancellationTokenSource();
        // Baca StandardOutput DAN StandardError sekaligus untuk mencegah pipe buffer deadlock
        _ = Task.Run(() => ReadStreamLoopAsync(_tunnelProcess.StandardOutput, _outputCts.Token));
        _ = Task.Run(() => ReadStreamLoopAsync(_tunnelProcess.StandardError, _outputCts.Token));

        return true;
    }

    private async Task ReadStreamLoopAsync(StreamReader reader, CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            string? line;
            try
            {
                line = await reader.ReadLineAsync(ct);
            }
            catch
            {
                break;
            }

            if (line == null) break;
            if (string.IsNullOrWhiteSpace(line)) continue;

            ProcessOutputLine(line);
        }
    }

    private void ProcessOutputLine(string line)
    {
        _logger.AddLog($"openconnect: {line}", "info", userVisible: false);

        // 1. Intercept IP sewa
        var ipMatch = RegexConfiguredIp().Match(line);
        if (ipMatch.Success)
        {
            var assignedIp = ipMatch.Groups[1].Value;
            _state.AssignedIp = assignedIp;
            OnIpAssigned?.Invoke(assignedIp);
        }

        // 2. Intercept GlobalProtect AuthCookie
        if (string.IsNullOrEmpty(_state.GpAuthCookie))
        {
            var acMatch = RegexAuthCookieDump().Match(line);
            if (acMatch.Success)
            {
                _state.GpAuthCookie = acMatch.Groups[1].Value;
                _logger.AddLog("Authcookie GP berhasil diintercept dari dump lalu lintas.", "info", userVisible: false);
            }
            else
            {
                var xmlMatch = RegexAuthCookieXml().Match(line);
                if (xmlMatch.Success && !string.Equals(xmlMatch.Groups[1].Value, "empty", StringComparison.OrdinalIgnoreCase))
                {
                    _state.GpAuthCookie = xmlMatch.Groups[1].Value;
                    _logger.AddLog("Authcookie GP berhasil diintercept dari XML.", "info", userVisible: false);
                }
            }
        }

        // 3. Intercept Portal Name
        if (string.IsNullOrEmpty(_state.GpPortal))
        {
            var portalMatch = RegexPortalName().Match(line);
            if (portalMatch.Success)
            {
                _state.GpPortal = portalMatch.Groups[1].Value.Trim();
            }
        }

        // 4. Intercept HIP md5sum
        if (string.IsNullOrEmpty(_state.GpHipMd5))
        {
            var md5Match = RegexHipMd5().Match(line);
            if (md5Match.Success)
            {
                _state.GpHipMd5 = md5Match.Groups[1].Value.Trim();
            }
        }

        // 5. Konfigurasi Rute & Terowongan Aktif
        if ((line.Contains("route configuration done", StringComparison.OrdinalIgnoreCase) ||
             line.Contains("Using Wintun device", StringComparison.OrdinalIgnoreCase)) &&
            !_state.IsConnected)
        {
            _state.IsConnected = true;
            _state.IsConnecting = false;
            _state.StatusMessage = $"Terhubung ({_state.AssignedIp})";
            _state.VpnStatus = "Terhubung";
            _state.VpnMeta = $"IP: {_state.AssignedIp}";
            _state.GatewayStatus = "Terverifikasi";
            _state.GatewayMeta = "Sesi tunnel aktif.";
            _state.SsoStatus = "Terautentikasi";
            _state.SsoMeta = "Sesi SAML valid.";
            _logger.AddLog($"VPN Berhasil Terhubung! IP Address: {_state.AssignedIp}", "success");
            ConfigureTunnelInterface();
            OnRouteConfigured?.Invoke();
        }

        // 6. Penolakan Cookie / Token
        if (line.Contains("Cookie was rejected by server", StringComparison.OrdinalIgnoreCase))
        {
            _logger.AddLog("Sesi ditolak oleh gateway server.", "warning");
            OnCookieRejected?.Invoke();
        }

        // 7. Dead peer
        if (line.Contains("Detected dead peer!", StringComparison.OrdinalIgnoreCase))
        {
            _logger.AddLog("Koneksi terputus: gateway tidak merespons (dead peer).", "warning");
            OnDeadPeer?.Invoke("Dead peer");
        }
    }

    private void ConfigureTunnelInterface()
    {
        try
        {
            var iface = _config.TunnelInterface;
            var psi = new ProcessStartInfo
            {
                FileName = "netsh",
                Arguments = $"interface ipv4 set interface \"{iface}\" metric=1",
                CreateNoWindow = true,
                UseShellExecute = false
            };
            using var p = Process.Start(psi);
            p?.WaitForExit(2000);

            var psSuffix = $"Set-DnsClient -InterfaceAlias '{iface}' -ConnectionSpecificSuffix 'bps.go.id' -ErrorAction SilentlyContinue; Clear-DnsClientCache";
            var psiPs = new ProcessStartInfo
            {
                FileName = "powershell",
                Arguments = $"-NoProfile -NonInteractive -Command \"{psSuffix}\"",
                CreateNoWindow = true,
                UseShellExecute = false
            };
            using var pPs = Process.Start(psiPs);
            pPs?.WaitForExit(3000);

            _logger.AddLog($"Adapter '{iface}' dioptimalkan (Metric=1, Suffix=bps.go.id, DNS flushed).", "info");
        }
        catch { }
    }

    public void StopTunnel()
    {
        _outputCts?.Cancel();

        if (_tunnelProcess != null && !_tunnelProcess.HasExited)
        {
            try
            {
                _tunnelProcess.Kill(entireProcessTree: true);
                _tunnelProcess.WaitForExit(3000);
            }
            catch { }
            finally
            {
                _tunnelProcess.Dispose();
                _tunnelProcess = null;
            }
        }

        // Pastikan tidak ada proses openconnect liar yang mengunci adapter
        try
        {
            using var p = Process.Start(new ProcessStartInfo("taskkill", "/F /IM openconnect.exe /T") { CreateNoWindow = true, UseShellExecute = false });
            p?.WaitForExit(2000);
        }
        catch { }

        _state.IsConnected = false;
        _state.IsConnecting = false;
        _state.TunnelPid = null;
        _state.StatusMessage = "Terputus.";
        _state.VpnStatus = "Tidak Terhubung";
        _state.VpnMeta = "Tunnel belum aktif.";
    }
}