using System;
using System.Diagnostics;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using VpnHelper.Services;

namespace VpnHelper.ViewModels;

public partial class MainViewModel : ObservableObject
{
    public StateService State { get; }
    public LoggerService Logger { get; }
    public ConfigService Config { get; }

    private readonly GlobalProtectService _gp;
    private readonly FortinetVpnService _fortinet;
    private readonly TunnelProcessService _tunnel;
    private readonly RoutingBypassService _bypass;
    private readonly DnsManagementService _dns;
    private readonly CallbackListenerService _callback;

    public MainViewModel(
        StateService state,
        LoggerService logger,
        ConfigService config,
        GlobalProtectService gp,
        FortinetVpnService fortinet,
        TunnelProcessService tunnel,
        RoutingBypassService bypass,
        DnsManagementService dns,
        CallbackListenerService callback)
    {
        State = state;
        Logger = logger;
        Config = config;
        _gp = gp;
        _fortinet = fortinet;
        _tunnel = tunnel;
        _bypass = bypass;
        _dns = dns;
        _callback = callback;

        State.PropertyChanged += async (s, e) =>
        {
            if (e.PropertyName == nameof(State.AutomateMode))
            {
                if (State.AutomateMode)
                {
                    Logger.AddLog("Mode otomatis diaktifkan.", "info");
                    if (!State.IsConnected && !State.IsConnecting)
                    {
                        Logger.AddLog("Jaringan terdeteksi aktif. Melakukan autentikasi otomatis...", "info");
                        await ConnectAsync();
                    }
                }
                else
                {
                    Logger.AddLog("Mode otomatis dinonaktifkan.", "info");
                }
            }
        };
    }

    [RelayCommand]
    public async Task ConnectAsync()
    {
        if (State.IsConnecting || State.IsConnected) return;

        State.IsConnecting = true;
        State.VpnStatus = "Menghubungkan...";
        State.VpnMeta = "Menyiapkan sesi terowongan...";
        State.SsoStatus = "Meminta SSO...";
        State.SsoMeta = "Mengarahkan autentikasi...";

        try
        {
            if (State.IsGpMode)
            {
                var resumed = await _gp.TryResumeSessionAsync();
                if (!resumed)
                {
                    Logger.AddLog("Membuka browser untuk SSO GlobalProtect...", "info");
                    var samlHtml = await _gp.GetPreloginSamlRequestHtmlAsync();
                    _callback.GpSamlHtml = samlHtml;

                    var url = "http://127.0.0.1:8765/gp/saml-form";
                    Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
                }
            }
            else
            {
                if (!string.IsNullOrEmpty(Config.SsoUsername) && !string.IsNullOrEmpty(Config.SsoPassword))
                {
                    await _fortinet.PerformHeadlessSsoAsync();
                }
                else
                {
                    var startUrl = $"https://{Config.VpnHost}:{Config.VpnPort}/remote/saml/start";
                    Logger.AddLog($"Membuka browser untuk SSO Fortinet...", "info");
                    Process.Start(new ProcessStartInfo(startUrl) { UseShellExecute = true });
                }
            }
        }
        catch (Exception ex)
        {
            State.IsConnecting = false;
            State.VpnStatus = "Gagal Terhubung";
            State.VpnMeta = ex.Message;
            State.SsoStatus = "Gagal";
            State.SsoMeta = ex.Message;
            Logger.AddLog($"Gagal memulai VPN: {ex.Message}", "error");
        }
    }

    [RelayCommand]
    public void Disconnect()
    {
        if (State.AutomateMode)
        {
            Logger.AddLog("Interaksi user terdeteksi (Logout).", "info");
            State.AutomateMode = false;
        }

        _tunnel.StopTunnel();
        _bypass.CleanupRoutes();
        _dns.RestorePhysicalAdapterDns();

        State.IsConnected = false;
        State.IsConnecting = false;
        State.AssignedIp = string.Empty;
        State.VpnStatus = "Tidak Terhubung";
        State.VpnMeta = "Tunnel belum aktif.";
        State.SsoStatus = "Idle";
        State.SsoMeta = "Belum ada callback.";
        State.GatewayStatus = "Belum Siap";
        State.GatewayMeta = "Menunggu sesi gateway.";
        Logger.AddLog("Koneksi VPN dihentikan.", "warning");
    }

    [RelayCommand]
    public void SetVpnMode(string mode)
    {
        if (State.IsConnected || State.IsConnecting) return;
        var isGp = mode.Equals("gp", StringComparison.OrdinalIgnoreCase) || mode.Equals("tls", StringComparison.OrdinalIgnoreCase);
        State.IsGpMode = isGp;
        State.ActiveProtocol = isGp ? "GlobalProtect" : "Fortinet";
        Logger.AddLog($"Mode VPN dialihkan ke: {(isGp ? "GP (vpn.bps.go.id)" : "FORTI (akses.bps.go.id)")}", "info");
    }

    [RelayCommand]
    public void ToggleAutomate()
    {
        State.AutomateMode = !State.AutomateMode;
        var status = State.AutomateMode ? "diaktifkan" : "dinonaktifkan";
        Logger.AddLog($"Mode otomatisasi {status}.", "info");
    }

    [RelayCommand]
    public void CopyLogs()
    {
        try
        {
            var sb = new StringBuilder();
            foreach (var log in Logger.Logs)
            {
                sb.AppendLine(log.FormattedText);
            }
            System.Windows.Clipboard.SetText(sb.ToString());
            Logger.AddLog("Riwayat log berhasil disalin ke clipboard.", "info");
        }
        catch { }
    }

    [RelayCommand]
    public void ClearLogs() => Logger.ClearLogs();

    [RelayCommand]
    public async Task RefreshBypassAsync()
    {
        if (!State.IsConnected)
        {
            Logger.AddLog("VPN belum aktif. Refresh rute hanya berlaku saat terhubung.", "warning");
            return;
        }

        Logger.AddLog("Memperbarui rute bypass split-tunnel...", "info");
        await _bypass.ApplyBypassRoutesAsync();
    }
}