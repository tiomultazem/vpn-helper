using CommunityToolkit.Mvvm.ComponentModel;

namespace VpnHelper.Services;

public partial class StateService : ObservableObject
{
    [ObservableProperty]
    private bool _isConnected;

    [ObservableProperty]
    private bool _isConnecting;

    [ObservableProperty]
    private string _assignedIp = string.Empty;

    [ObservableProperty]
    private int? _tunnelPid;

    [ObservableProperty]
    private string _statusMessage = "Siap.";

    [ObservableProperty]
    private string _activeProtocol = "Fortinet"; // Default FORTI

    [ObservableProperty]
    private bool _isGpMode = false;

    [ObservableProperty]
    private bool _automateMode;

    [ObservableProperty]
    private string? _lastError;

    // Status Cards (100% identik dengan WebUI)
    [ObservableProperty]
    private string _ssoStatus = "Idle";

    [ObservableProperty]
    private string _ssoMeta = "Belum ada callback.";

    [ObservableProperty]
    private string _vpnStatus = "Idle";

    [ObservableProperty]
    private string _vpnMeta = "Tunnel belum aktif.";

    [ObservableProperty]
    private string _gatewayStatus = "Belum Siap";

    [ObservableProperty]
    private string _gatewayMeta = "Menunggu sesi gateway.";

    // Bypass info
    [ObservableProperty]
    private string _bypassIface = "—";

    [ObservableProperty]
    private string _bypassGateway = "—";

    [ObservableProperty]
    private string _bypassRoutes = "—";

    // Data sesi runtime internal
    public string? GpToken { get; set; }
    public string? GpAuthCookie { get; set; }
    public string? GpPortal { get; set; }
    public string? GpHipMd5 { get; set; }
    public string? GpUsername { get; set; }
    public bool GpResumedSession { get; set; }

    public string? FortinetSessionCookie { get; set; }
    public string? FortinetAuthId { get; set; }

    public string? PhysicalAdapterName { get; set; }
    public string? PhysicalAdapterOldDns { get; set; }
}