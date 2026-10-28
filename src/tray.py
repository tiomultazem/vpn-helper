import os
import time
import threading
import webbrowser

from src.state import state, add_log, update_tray_status, BASE_DIR
from src.config import (
    SSO_USERNAME, SSO_PASSWORD, build_app_url,
    ENABLE_TRAY, AUTO_HIDE_CONSOLE
)
import src.state as state_mod

ASSETS_DIR = os.path.join(BASE_DIR, "assets")

# Flag untuk tracking state console window (True = visible, False = hidden)
_console_visible = [True]

def load_tray_modules():
    try:
        import pystray
        from PIL import Image, ImageDraw
        return pystray, Image, ImageDraw
    except Exception as e:
        add_log(f"System tray tidak aktif: {e}", "warning")
        return None

def build_tray_icon_image(Image, ImageDraw):
    logo_path = os.path.join(ASSETS_DIR, "logo.png")
    if os.path.isfile(logo_path):
        try:
            return Image.open(logo_path).convert("RGBA")
        except Exception as e:
            add_log(f"Logo system tray tidak bisa dimuat: {e}", "warning")

    image = Image.new("RGBA", (64, 64), (18, 24, 38, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 56, 56), radius=14, fill=(43, 108, 176, 255))
    draw.rounded_rectangle((18, 28, 46, 46), radius=6, fill=(240, 245, 250, 255))
    draw.arc((20, 16, 44, 40), 190, 350, fill=(240, 245, 250, 255), width=5)
    draw.ellipse((29, 34, 35, 40), fill=(43, 108, 176, 255))
    return image

def hide_console_window():
    if os.name != "nt" or not AUTO_HIDE_CONSOLE:
        return

    try:
        import ctypes
        handle = ctypes.windll.kernel32.GetConsoleWindow()
        if handle:
            ctypes.windll.user32.ShowWindow(handle, 0)
            _console_visible[0] = False
    except Exception as e:
        add_log(f"Console tidak bisa disembunyikan: {e}", "warning")

def show_console_window():
    if os.name != "nt":
        return

    try:
        import ctypes
        handle = ctypes.windll.kernel32.GetConsoleWindow()
        if handle:
            ctypes.windll.user32.ShowWindow(handle, 9)
            ctypes.windll.user32.SetForegroundWindow(handle)
            _console_visible[0] = True
    except Exception as e:
        add_log(f"Console tidak bisa ditampilkan: {e}", "warning")

def setup_console_tray_behavior():
    if os.name != "nt" or not AUTO_HIDE_CONSOLE:
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        handle = kernel32.GetConsoleWindow()
        if not handle:
            return

        SC_CLOSE = 0xF060
        MF_BYCOMMAND = 0x00000000
        SW_HIDE = 0
        SIZE_MINIMIZED = 1
        WM_SIZE = 0x0005
        GWL_WNDPROC = -4

        HWND = ctypes.c_void_p
        UINT = ctypes.c_uint
        WPARAM = ctypes.c_size_t
        LPARAM = ctypes.c_ssize_t
        LRESULT = ctypes.c_ssize_t
        LONG_PTR = ctypes.c_ssize_t

        user32.CallWindowProcW.argtypes = [LONG_PTR, HWND, UINT, WPARAM, LPARAM]
        user32.CallWindowProcW.restype = LRESULT
        user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
        user32.IsIconic.argtypes = [HWND]
        user32.IsIconic.restype = ctypes.c_bool
        user32.GetSystemMenu.argtypes = [HWND, ctypes.c_bool]
        user32.GetSystemMenu.restype = HWND
        user32.DeleteMenu.argtypes = [HWND, UINT, UINT]
        user32.DrawMenuBar.argtypes = [HWND]

        if ctypes.sizeof(ctypes.c_void_p) == ctypes.sizeof(ctypes.c_longlong):
            get_window_long = user32.GetWindowLongPtrW
            set_window_long = user32.SetWindowLongPtrW
        else:
            get_window_long = user32.GetWindowLongW
            set_window_long = user32.SetWindowLongW

        get_window_long.argtypes = [HWND, ctypes.c_int]
        get_window_long.restype = LONG_PTR
        set_window_long.argtypes = [HWND, ctypes.c_int, LONG_PTR]
        set_window_long.restype = LONG_PTR

        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

        old_proc = get_window_long(handle, GWL_WNDPROC)

        def window_proc(hwnd, msg, wparam, lparam):
            if msg == WM_SIZE and int(wparam) == SIZE_MINIMIZED:
                user32.ShowWindow(hwnd, SW_HIDE)
                return 0

            return user32.CallWindowProcW(old_proc, hwnd, msg, wparam, lparam)

        console_proc = WNDPROC(window_proc)
        setup_console_tray_behavior._console_proc = console_proc
        set_window_long(handle, GWL_WNDPROC, ctypes.cast(console_proc, ctypes.c_void_p).value)

        system_menu = user32.GetSystemMenu(handle, False)
        if system_menu:
            user32.DeleteMenu(system_menu, SC_CLOSE, MF_BYCOMMAND)
            user32.DrawMenuBar(handle)

        def watch_console_minimize():
            SW_HIDE = 0
            while True:
                current_handle = kernel32.GetConsoleWindow()
                if current_handle and user32.IsIconic(current_handle):
                    user32.ShowWindow(current_handle, SW_HIDE)
                    _console_visible[0] = False
                    try:
                        icon_ref = state_mod.global_tray_icon
                        if icon_ref:
                            icon_ref.update_menu()
                    except Exception:
                        pass
                time.sleep(0.1)

        threading.Thread(target=watch_console_minimize, daemon=True).start()
    except Exception as e:
        add_log(f"Perilaku console ke tray tidak bisa diaktifkan: {e}", "warning")

def open_app_in_browser(url=None, delay=0.8):
    if url is None:
        url = build_app_url()
    def open_later():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception as e:
            add_log(f"Browser default tidak bisa dibuka: {e}", "warning")

    threading.Thread(target=open_later, daemon=True).start()

def exit_from_tray(icon, on_stop_tunnel):
    add_log("VPN Helper ditutup dari system tray.", "warning")
    if on_stop_tunnel:
        on_stop_tunnel()
    icon.stop()
    os._exit(0)

def run_tray_mode(
    on_start_callback_listener,
    on_start_app_server,
    on_start_auto_connect,
    on_stop_tunnel
):
    tray_modules = load_tray_modules()
    if not tray_modules:
        return False

    pystray, Image, ImageDraw = tray_modules
    threading.Thread(target=on_start_callback_listener, daemon=True).start()
    threading.Thread(target=on_start_app_server, daemon=True).start()
    if SSO_USERNAME and SSO_PASSWORD:
        threading.Thread(target=on_start_auto_connect, daemon=True).start()

    def open_app(icon, item):
        webbrowser.open(build_app_url())

    def is_console_visible():
        return _console_visible[0]

    def toggle_console(icon, item):
        if is_console_visible():
            hide_console_window()
        else:
            show_console_window()
        try:
            icon.update_menu()
        except Exception:
            pass

    def get_console_toggle_text(item):
        return "Hide Console" if is_console_visible() else "Show Console"

    def quit_app(icon, item):
        exit_from_tray(icon, on_stop_tunnel)

    def get_status_text(item):
        status = "connected" if state["vpn_connected"] else "disconnected"
        return f"({status})"

    def on_left_click(icon):
        try:
            import ctypes
            ctypes.windll.user32.mouse_event(0x0008, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(0x0010, 0, 0, 0, 0)
        except Exception:
            pass

    icon = pystray.Icon(
        "vpn-helper",
        build_tray_icon_image(Image, ImageDraw),
        "VPN Helper (disconnected)",
        pystray.Menu(
            pystray.MenuItem(get_status_text, None, enabled=False),
            pystray.MenuItem("Open VPN Helper", open_app),
            pystray.MenuItem(get_console_toggle_text, toggle_console),
            pystray.MenuItem("Exit", quit_app),
        ),
        action=on_left_click
    )
    state_mod.global_tray_icon = icon

    add_log("VPN Helper aktif di system tray.", "info")
    add_log(f"Buka dari tray atau akses {build_app_url()}", "info")
    open_app_in_browser()
    setup_console_tray_behavior()
    hide_console_window()
    icon.run()
    return True
