import os
import time
import datetime
import socket
import threading

# DNS caching to eliminate redundant DNS lookups and speed up sequential HTTP requests
_original_getaddrinfo = socket.getaddrinfo
_dns_cache = {}
_dns_cache_lock = threading.Lock()

def _cached_getaddrinfo(*args, **kwargs):
    host = args[0]
    # Do not cache numeric IPs or None/empty hosts
    if not host or host.replace('.', '').isdigit():
        return _original_getaddrinfo(*args, **kwargs)
        
    now = time.time()
    cache_key = (args, tuple(kwargs.items()))
    
    with _dns_cache_lock:
        if cache_key in _dns_cache:
            addr, expiry = _dns_cache[cache_key]
            if now < expiry:
                return addr
                
    try:
        addr = _original_getaddrinfo(*args, **kwargs)
        with _dns_cache_lock:
            # Cache for 10 minutes (600 seconds)
            _dns_cache[cache_key] = (addr, now + 600)
        return addr
    except Exception:
        with _dns_cache_lock:
            if cache_key in _dns_cache:
                return _dns_cache[cache_key][0]
        raise

socket.getaddrinfo = _cached_getaddrinfo


class InvalidCredentialsError(RuntimeError):
    pass


class OTPRequiredError(RuntimeError):
    pass


def format_user_error(e):
    if not e:
        return ""
    if isinstance(e, Exception):
        err_msg = str(e)
        if "ConnectionResetError" in err_msg or "10054" in err_msg:
            return "Koneksi diputus paksa oleh server (Connection Reset)."
        if "ConnectionRefusedError" in err_msg or "111" in err_msg or "10061" in err_msg:
            return "Koneksi ditolak oleh gateway (Connection Refused)."
        if "TimeoutError" in err_msg or "timed out" in err_msg:
            return "Waktu koneksi habis (Timeout)."
        if "SSLError" in err_msg or "certificate verify failed" in err_msg:
            return "Gagal verifikasi sertifikat SSL Gateway."
        if "NameResolutionError" in err_msg or "Failed to resolve" in err_msg or "gai error" in err_msg:
            return "Gagal mencari alamat host (DNS Error)."
            
        if hasattr(e, "args") and e.args:
            first_arg = e.args[0]
            if isinstance(first_arg, str):
                return first_arg
            elif isinstance(first_arg, Exception):
                return format_user_error(first_arg)
            elif isinstance(first_arg, tuple):
                for item in first_arg:
                    if isinstance(item, str):
                        return item
                    elif isinstance(item, Exception):
                        return format_user_error(item)
        return err_msg
    return str(e)


# Base paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Config fallbacks (updated dynamically by config module)
LOG_MAX_ENTRIES = 500
MASK_KEEP_START = 6
MASK_KEEP_END = 4

state = {
    "vpn_connected": False,
    "connect_requested": False,
    "sso_completed": False,
    "svpn_cookie_found": False,
    "vpn_config_loaded": False,
    "vpn_config_summary": {},
    "logs": [],
    "sso_in_progress": False,
    "last_callback": None,
    "last_callback_query": {},
    "last_auth_id": None,
    "sso_url": None,
    "last_error": None,
    "tunnel_engine": None,
    "tunnel_pid": None
}

global_tray_icon = None

def update_tray_status():
    global global_tray_icon
    if not global_tray_icon:
        return
    status = "connected" if state["vpn_connected"] else "disconnected"
    global_tray_icon.title = f"VPN Helper ({status})"
    try:
        global_tray_icon.update_menu()
    except Exception:
        pass


log_file_lock = threading.Lock()

def add_log(message, level="info", user_visible=True):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    full_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Always print to developer console
    print(f"[{ts}] [{level.upper()}] {message}")
    
    # Write to vpn_helper.log with automatic rotation
    msg_to_file = f"[UI] {message}" if user_visible else message
    log_file = os.path.join(BASE_DIR, "vpn_helper.log")
    
    with log_file_lock:
        try:
            # Jika ukuran file log melebihi 200 KB, langsung kosongkan file
            if os.path.exists(log_file) and os.path.getsize(log_file) > 200 * 1024:
                with open(log_file, "w", encoding="utf-8") as f:
                    f.write(f"[{full_ts}] [INFO] Log dikosongkan secara otomatis karena melebihi batas 200KB.\n")
        except Exception:
            pass
                
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{full_ts}] [{level.upper()}] {msg_to_file}\n")
        except Exception:
            pass
        
    if user_visible:
        # Gunakan format MM-DD HH:MM:SS untuk UI agar ringkas
        short_ts = datetime.datetime.now().strftime("%m-%d %H:%M:%S")
        state["logs"].append({"time": short_ts, "message": message, "level": level})
        if len(state["logs"]) > LOG_MAX_ENTRIES:
            state["logs"] = state["logs"][-LOG_MAX_ENTRIES:]

def mask_token(value, keep_start=None, keep_end=None):
    if keep_start is None:
        keep_start = MASK_KEEP_START
    if keep_end is None:
        keep_end = MASK_KEEP_END
    value = str(value or "")
    if len(value) <= keep_start + keep_end:
        return "***" if value else ""
    return f"{value[:keep_start]}...{value[-keep_end:]}"

def load_persisted_logs():
    log_file = os.path.join(BASE_DIR, "vpn_helper.log")
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-1000:]:
                import re
                m = re.match(r"^\[(.*?)\] \[(.*?)\] (.*)$", line.strip())
                if m:
                    full_ts, lvl, msg = m.groups()
                    if msg.startswith("[UI] "):
                        clean_msg = msg[5:]
                        # Potong tahun (YYYY-) agar muat satu baris di UI
                        short_ts = full_ts[5:] if len(full_ts) >= 19 else full_ts
                        state["logs"].append({"time": short_ts, "message": clean_msg, "level": lvl.lower()})
        except Exception:
            pass
