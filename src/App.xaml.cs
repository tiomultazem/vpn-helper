using System;
using System.IO;
using System.Threading.Tasks;
using System.Windows;
using VpnHelper.Services;
using VpnHelper.ViewModels;
using VpnHelper.Views;

namespace VpnHelper;

public partial class App : System.Windows.Application
{
    private ConfigService? _config;
    private LoggerService? _logger;
    private StateService? _state;
    private TunnelProcessService? _tunnel;
    private GlobalProtectService? _gp;
    private FortinetVpnService? _fortinet;
    private RoutingBypassService? _bypass;
    private DnsManagementService? _dns;
    private CallbackListenerService? _callback;
    private MainViewModel? _viewModel;

    private void Application_Startup(object sender, StartupEventArgs e)
    {
        // Hentikan instans VPN Helper lama dan orphan openconnect.exe
        try
        {
            var currentPid = Environment.ProcessId;
            foreach (var proc in System.Diagnostics.Process.GetProcessesByName("VPN Helper"))
            {
                if (proc.Id != currentPid)
                {
                    try { proc.Kill(entireProcessTree: true); proc.WaitForExit(1000); } catch { }
                }
            }
            foreach (var proc in System.Diagnostics.Process.GetProcessesByName("openconnect"))
            {
                try { proc.Kill(entireProcessTree: true); proc.WaitForExit(1000); } catch { }
            }
        }
        catch { }

        var baseDir = ConfigService.ResolveBaseDirectory();
        SetupGlobalErrorLogger(baseDir);

#if !MITRA_BUILD
        // 0. Versi Production: Cek Ketersediaan .env / Ekstraksi melalui pertanyaan sestama "zulkipli"
        var envPath = Path.Combine(baseDir, ".env");
        if (!File.Exists(envPath))
        {
            var setupDialog = new SetupKeyWindow(baseDir);
            var ok = setupDialog.ShowDialog();
            if (ok != true || !File.Exists(envPath))
            {
                Shutdown();
                return;
            }
        }
#endif

        // 1. Inisialisasi Service Singletons
        _config = new ConfigService();
        _logger = new LoggerService(_config);
        _state = new StateService();

        _tunnel = new TunnelProcessService(_config, _logger, _state);
        _gp = new GlobalProtectService(_config, _logger, _state, _tunnel);
        _fortinet = new FortinetVpnService(_config, _logger, _state, _tunnel);
        _bypass = new RoutingBypassService(_config, _logger, _state);
        _dns = new DnsManagementService(_logger, _state);
        _callback = new CallbackListenerService(_config, _logger, _state);

        _viewModel = new MainViewModel(
            _state,
            _logger,
            _config,
            _gp,
            _fortinet,
            _tunnel,
            _bypass,
            _dns,
            _callback
        );

        // 2. Hubungkan Sinyal Terowongan (Tunnel Events)
        _tunnel.OnRouteConfigured += async () =>
        {
            _state.IsConnected = true;
            _state.IsConnecting = false;
            _state.VpnStatus = "Terhubung";
            _state.VpnMeta = $"IP: {_state.AssignedIp}";
            _state.GatewayStatus = "Terverifikasi";
            _state.GatewayMeta = "Sesi tunnel aktif.";
            _state.SsoStatus = "Terautentikasi";
            _state.SsoMeta = "Sesi SAML valid.";

            if (_config.BypassEnabled)
            {
                _bypass.StartPeriodicRefresh();
            }

            if (_state.IsGpMode && !string.IsNullOrEmpty(_state.AssignedIp))
            {
                await _gp.SubmitHipReportAsync(_state.AssignedIp);
            }

            _dns.EnforceDnsOnPhysicalAdapter();
        };

        _tunnel.OnCookieRejected += () =>
        {
            if (_state.IsGpMode)
            {
                _gp.ClearSession();
            }
            _state.GatewayStatus = "Ditolak";
            _state.GatewayMeta = "Sesi ditolak oleh gateway.";
        };

        _tunnel.OnTunnelStopped += async (reason) =>
        {
            _state.VpnStatus = "Tidak Terhubung";
            _state.VpnMeta = reason;
            if (_state.AutomateMode)
            {
                _logger.AddLog($"Auto-reconnect dipicu karena: {reason}", "warning");
                await Task.Delay(3000);
                if (_state.AutomateMode && !_state.IsConnected && !_state.IsConnecting)
                {
                    await _viewModel!.ConnectAsync();
                }
            }
        };

        // 3. Hubungkan Sinyal Callback SSO
        _callback.OnFortinetAuthIdReceived += async (authId) =>
        {
            try
            {
                _state.SsoStatus = "Terverifikasi";
                _state.SsoMeta = "Auth ID diterima dari SSO.";
                _state.GatewayStatus = "Menghubungkan";
                _state.GatewayMeta = "Menukar cookie gateway...";

                var cookie = await _fortinet.ExchangeAuthIdForCookieAsync(authId);
                _state.FortinetSessionCookie = cookie;
                _logger.AddLog("SVPNCOOKIE berhasil didapatkan via callback.", "success");

                await _tunnel.StartTunnelAsync(
                    "fortinet",
                    $"{_config.VpnHost}:{_config.VpnPort}",
                    cookie
                );
            }
            catch (Exception ex)
            {
                _logger.AddLog($"Gagal memproses login Fortinet: {ex.Message}", "error");
                _state.IsConnecting = false;
                _state.VpnStatus = "Gagal";
                _state.VpnMeta = ex.Message;
            }
        };

        _callback.OnGpTokenReceived += async (token, username) =>
        {
            try
            {
                _state.GpToken = token;
                _state.GpUsername = username;
                _state.SsoStatus = "Terverifikasi";
                _state.SsoMeta = $"Token GP untuk {username}";
                _state.GatewayStatus = "Menghubungkan";
                _state.GatewayMeta = "Memulai terowongan GP...";

                _logger.AddLog($"Memulai terowongan GP untuk pengguna {username}...", "info");

                await _tunnel.StartTunnelAsync(
                    "gp",
                    $"{_config.GpGateway}:{_config.GpPort}",
                    token,
                    username,
                    "gateway:prelogin-cookie"
                );
            }
            catch (Exception ex)
            {
                _logger.AddLog($"Gagal memulai terowongan GP: {ex.Message}", "error");
                _state.IsConnecting = false;
                _state.VpnStatus = "Gagal";
                _state.VpnMeta = ex.Message;
            }
        };

        // 4. Mulai Listener Port 8020 & 8765
        _callback.Start();

        // 5. Buka Jendela Utama
        var mainWindow = new MainWindow(_viewModel);
        mainWindow.Show();
    }

    private static void SetupGlobalErrorLogger(string baseDir)
    {
        var errorFile = Path.Combine(baseDir, "error.txt");

        void LogError(string source, Exception? ex)
        {
            var ts = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
            var content = $"[{ts}] [{source}]\n{ex?.ToString() ?? "Unknown error"}\n";
            try { File.WriteAllText(errorFile, content); } catch { }
        }

        AppDomain.CurrentDomain.UnhandledException += (s, e) =>
        {
            LogError("AppDomain.UnhandledException", e.ExceptionObject as Exception);
        };

        if (System.Windows.Application.Current != null)
        {
            System.Windows.Application.Current.DispatcherUnhandledException += (s, e) =>
            {
                LogError("DispatcherUnhandledException", e.Exception);
            };
        }

        TaskScheduler.UnobservedTaskException += (s, e) =>
        {
            LogError("TaskScheduler.UnobservedTaskException", e.Exception);
        };
    }

    private void Application_Exit(object sender, ExitEventArgs e)
    {
        _tunnel?.StopTunnel();
        _bypass?.CleanupRoutes();
        _dns?.RestorePhysicalAdapterDns();
        _callback?.Stop();
    }
}