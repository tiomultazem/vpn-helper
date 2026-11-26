using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Interop;
using System.Windows.Media.Imaging;
using VpnHelper.Interop;
using VpnHelper.Services;
using VpnHelper.ViewModels;

namespace VpnHelper;

public partial class MainWindow : Window
{
    private readonly MainViewModel _viewModel;
    private NativeTray.NOTIFYICONDATA _trayData;
    private bool _isTrayAdded;
    private bool _isExplicitExit;
    private ContextMenu? _trayMenu;
    private IntPtr _hIconSmall;
    private IntPtr _hIconLarge;

    public MainWindow(MainViewModel viewModel)
    {
        InitializeComponent();
        _viewModel = viewModel;
        DataContext = _viewModel;

        Loaded += MainWindow_Loaded;

        // Auto-scroll ke log paling akhir
        _viewModel.Logger.LogAdded += entry =>
        {
            Dispatcher.InvokeAsync(() =>
            {
                if (LogsList.Items.Count > 0)
                {
                    LogsList.ScrollIntoView(entry);
                }
            });
        };

        // Auto-minimize ke systray dengan jeda visual (1,5 detik) agar seluruh kartu hijau terlihat oleh pengguna
        _viewModel.State.PropertyChanged += async (s, e) =>
        {
            if (e.PropertyName == nameof(StateService.IsConnected) && _viewModel.State.IsConnected)
            {
                await Task.Delay(1500);
                if (_viewModel.State.IsConnected && IsVisible)
                {
                    await Dispatcher.InvokeAsync(() =>
                    {
                        Hide();
                        ShowBalloonTip("VPN Helper", "VPN Terhubung. Berjalan di latar belakang.");
                    });
                }
            }
        };
    }

    private void MainWindow_Loaded(object sender, RoutedEventArgs e)
    {
        _trayMenu = (ContextMenu)Resources["TrayContextMenu"];
        InitNativeTray();
    }

    private void InitNativeTray()
    {
        var helper = new WindowInteropHelper(this);
        var hwnd = helper.Handle;

        var source = HwndSource.FromHwnd(hwnd);
        source?.AddHook(WndProc);

        IntPtr hIcon = IntPtr.Zero;
        try
        {
            var procPath = Environment.ProcessPath;
            if (!string.IsNullOrEmpty(procPath) && File.Exists(procPath))
            {
                NativeTray.ExtractIconEx(procPath, 0, out _hIconLarge, out _hIconSmall, 1);
                hIcon = _hIconSmall != IntPtr.Zero ? _hIconSmall : _hIconLarge;

                if (hIcon != IntPtr.Zero)
                {
                    Icon = Imaging.CreateBitmapSourceFromHIcon(
                        hIcon,
                        Int32Rect.Empty,
                        BitmapSizeOptions.FromEmptyOptions()
                    );
                }
            }
        }
        catch { }

        _trayData = new NativeTray.NOTIFYICONDATA
        {
            cbSize = (uint)Marshal.SizeOf<NativeTray.NOTIFYICONDATA>(),
            hWnd = hwnd,
            uID = 1,
            uFlags = NativeTray.NIF_MESSAGE | NativeTray.NIF_ICON | NativeTray.NIF_TIP,
            uCallbackMessage = NativeTray.WM_TRAYICON,
            hIcon = hIcon,
            szTip = "VPN Helper - Enterprise Client"
        };

        _isTrayAdded = NativeTray.Shell_NotifyIcon(NativeTray.NIM_ADD, ref _trayData);
    }

    private IntPtr WndProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (msg == NativeTray.WM_TRAYICON)
        {
            var eventType = lParam.ToInt32();
            if (eventType is NativeTray.WM_LBUTTONDBLCLK or NativeTray.WM_LBUTTONUP)
            {
                RestoreWindow();
                handled = true;
            }
            else if (eventType == NativeTray.WM_RBUTTONUP)
            {
                ShowTrayContextMenu();
                handled = true;
            }
        }

        return IntPtr.Zero;
    }

    private void ShowTrayContextMenu()
    {
        if (_trayMenu == null) return;

        var isConnected = _viewModel.State.IsConnected;
        foreach (var item in _trayMenu.Items)
        {
            if (item is MenuItem menuItem)
            {
                var header = menuItem.Header as string;
                if (header == "Hubungkan VPN")
                {
                    menuItem.Visibility = isConnected ? Visibility.Collapsed : Visibility.Visible;
                    menuItem.IsEnabled = !_viewModel.State.IsConnecting;
                }
                else if (header == "Putuskan VPN")
                {
                    menuItem.Visibility = isConnected ? Visibility.Visible : Visibility.Collapsed;
                }
            }
        }

        NativeTray.GetCursorPos(out var pt);
        var helper = new WindowInteropHelper(this);
        NativeTray.SetForegroundWindow(helper.Handle);

        _trayMenu.Placement = PlacementMode.AbsolutePoint;
        _trayMenu.HorizontalOffset = pt.X;
        _trayMenu.VerticalOffset = pt.Y;
        _trayMenu.IsOpen = true;
    }

    private void ShowBalloonTip(string title, string message)
    {
        if (!_isTrayAdded) return;

        _trayData.uFlags |= NativeTray.NIF_INFO;
        _trayData.szInfoTitle = title;
        _trayData.szInfo = message;
        _trayData.dwInfoFlags = NativeTray.NIIF_INFO;

        NativeTray.Shell_NotifyIcon(NativeTray.NIM_MODIFY, ref _trayData);

        _trayData.uFlags &= ~NativeTray.NIF_INFO;
    }

    private void TrayOpen_Click(object sender, RoutedEventArgs e) => RestoreWindow();

    private async void TrayConnect_Click(object sender, RoutedEventArgs e) => await _viewModel.ConnectAsync();

    private void TrayDisconnect_Click(object sender, RoutedEventArgs e) => _viewModel.Disconnect();

    private void TrayExit_Click(object sender, RoutedEventArgs e)
    {
        _isExplicitExit = true;
        RemoveTrayIcon();
        System.Windows.Application.Current.Shutdown();
    }

    private void RemoveTrayIcon()
    {
        if (_isTrayAdded)
        {
            NativeTray.Shell_NotifyIcon(NativeTray.NIM_DELETE, ref _trayData);
            _isTrayAdded = false;
        }

        if (_hIconSmall != IntPtr.Zero)
        {
            NativeTray.DestroyIcon(_hIconSmall);
            _hIconSmall = IntPtr.Zero;
        }
        if (_hIconLarge != IntPtr.Zero)
        {
            NativeTray.DestroyIcon(_hIconLarge);
            _hIconLarge = IntPtr.Zero;
        }
    }

    private void AutomateCheckBox_Click(object sender, RoutedEventArgs e)
    {
        bool wasActive = _viewModel.State.AutomateMode;

        if (!wasActive)
        {
            if (string.IsNullOrEmpty(_viewModel.Config.SsoUsername) || string.IsNullOrEmpty(_viewModel.Config.SsoPassword))
            {
                System.Windows.MessageBox.Show(
                    "Silakan atur sso_username dan sso_password di file .env terlebih dahulu.",
                    "Kredensial Belum Diatur",
                    System.Windows.MessageBoxButton.OK,
                    System.Windows.MessageBoxImage.Error
                );
                AutomateCheckBox.SetCurrentValue(ToggleButton.IsCheckedProperty, false);
                return;
            }

            var res = System.Windows.MessageBox.Show(
                "Mengaktifkan handshake otomatis berisiko jika kredensial Anda salah atau sesi bermasalah.",
                "Do with your own risk",
                System.Windows.MessageBoxButton.OKCancel,
                System.Windows.MessageBoxImage.Warning
            );

            if (res == System.Windows.MessageBoxResult.OK)
            {
                AutomateCheckBox.SetCurrentValue(ToggleButton.IsCheckedProperty, true);
                _viewModel.State.AutomateMode = true;
            }
            else
            {
                AutomateCheckBox.SetCurrentValue(ToggleButton.IsCheckedProperty, false);
                _viewModel.State.AutomateMode = false;
            }
        }
        else
        {
            AutomateCheckBox.SetCurrentValue(ToggleButton.IsCheckedProperty, false);
            _viewModel.State.AutomateMode = false;
        }
    }

    private void RestoreWindow()
    {
        Show();
        WindowState = WindowState.Normal;
        Activate();
    }

    protected override void OnStateChanged(EventArgs e)
    {
        base.OnStateChanged(e);
        if (WindowState == WindowState.Minimized)
        {
            Hide();
        }
    }

    protected override void OnClosing(CancelEventArgs e)
    {
        if (!_isExplicitExit)
        {
            e.Cancel = true;
            Hide();
            ShowBalloonTip("VPN Helper", "Aplikasi tetap berjalan di system tray.");
        }
        else
        {
            RemoveTrayIcon();
            base.OnClosing(e);
        }
    }
}
