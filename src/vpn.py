import os
import time
import socket
import re
import threading
import subprocess
import requests
import xml.etree.ElementTree as ET

import webbrowser

from src.state import (
    state, add_log, format_user_error, mask_token, 
    InvalidCredentialsError, OTPRequiredError, update_tray_status, BASE_DIR
)

from src.config import (
    VPN_HOST, VPN_PORT, VERIFY_TLS, REQUEST_TIMEOUT,
    SESSION_COOKIE_NAME, OPENCONNECT_PROTOCOL, TUNNEL_INTERFACE,
    OPENCONNECT_BINARY_NAMES, OPENCONNECT_SEARCH_PATHS, OPENCONNECT_PATH,
    OPENCONNECT_EXTRA_ARGS, OPENCONNECT_AUTO_NO_DTLS,
    PING_TARGET, PING_INTERVAL, SSO_USERNAME, SSO_PASSWORD,
    DEV_MODE, HTTP_USER_AGENT, HTTP_ACCEPT, HTTP_PRAGMA,
    build_saml_start_url, build_saml_auth_id_url, build_vpn_config_url,
    update_env_variable, ensure_internal_config
)
from src import config
from src.bypass import bypass_manager

# Regex patterns
RE_JS_REDIRECT = re.compile(r"window\.location\s*=\s*['\"]([^'\"]+)['\"]")
RE_ACTION_URL = re.compile(r'action="([^"]+)"')
RE_SAML_RESPONSE = re.compile(r'name="SAMLResponse"\s+value="([^"]+)"')
RE_RELAY_STATE = re.compile(r'name="RelayState"\s+value="([^"]+)"')
RE_CALLBACK_ID = re.compile(r"[?&]id=([^'\"]+)")

# HTTP Session for VPN
vpn_session = requests.Session()
vpn_session.headers.update({
    "User-Agent": HTTP_USER_AGENT,
    "Accept": HTTP_ACCEPT,
    "Pragma": HTTP_PRAGMA,
})

# Subprocess & thread state
tunnel_process = None
tunnel_log_thread = None
ping_thread = None
ping_stop_event = threading.Event()
reconnect_thread = None
reconnect_lock = threading.Lock()
reconnect_stop_event = threading.Event()

def is_reconnecting():
    with reconnect_lock:
        return reconnect_thread is not None and reconnect_thread.is_alive() and not reconnect_stop_event.is_set()

def stop_auto_reconnect():
    reconnect_stop_event.set()
    add_log("Proses auto-reconnect dihentikan.", "info", user_visible=False)

def is_gateway_reachable():
    try:
        socket.setdefaulttimeout(3)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((VPN_HOST, int(VPN_PORT)))
        s.close()
        return True
    except Exception:
        return False

def trigger_auto_reconnect(reason):
    global reconnect_thread
    if not config.AUTOMATE or DEV_MODE != "pointcoffee":
        return
        
    with reconnect_lock:
        if reconnect_thread and reconnect_thread.is_alive():
            return
            
        reconnect_stop_event.clear()
        add_log(f"Auto-reconnect dipicu karena: {reason}", "warning", user_visible=False)
        
        def reconnect_worker():
            stop_tunnel_process()
            invalidate_gateway_session("Koneksi VPN terputus. Memulai proses penyambungan kembali...")
            last_state_reachable = None
            
            while not reconnect_stop_event.is_set() and config.AUTOMATE and not state["vpn_connected"]:
                reachable = is_gateway_reachable()
                if reachable:
                    if last_state_reachable is not True:
                        add_log("Jaringan terdeteksi aktif. Melakukan autentikasi otomatis...", "info", user_visible=True)
                        state["last_error"] = "Jaringan terdeteksi aktif. Melakukan autentikasi otomatis..."
                    last_state_reachable = True
                    try:
                        perform_headless_sso()
                        for _ in range(15):
                            if state["vpn_connected"] or not config.AUTOMATE or reconnect_stop_event.is_set():
                                return
                            time.sleep(1)
                    except InvalidCredentialsError:
                        return
                    except OTPRequiredError:
                        add_log("Autentikasi OTP/2FA terdeteksi. Membuka browser default untuk verifikasi...", "warning", user_visible=True)
                        state["last_error"] = "OTP terdeteksi. Silakan selesaikan login di browser..."
                        webbrowser.open(build_saml_start_url())
                        for _ in range(60):
                            if state["vpn_connected"] or not config.AUTOMATE or reconnect_stop_event.is_set():
                                return
                            time.sleep(1)
                    except Exception as e:
                        clean_err = format_user_error(e)
                        state["last_error"] = f"Upaya autentikasi ulang gagal ({clean_err}). Mencoba lagi..."
                        add_log(f"Upaya autentikasi ulang gagal ({clean_err}). Mencoba lagi dalam 10 detik...", "warning", user_visible=True)
                else:
                    if last_state_reachable is not False:
                        add_log("Gateway tidak terjangkau. Menunggu koneksi jaringan pulih...", "warning", user_visible=True)
                        state["last_error"] = "Gateway tidak terjangkau. Menunggu koneksi jaringan pulih..."
                    last_state_reachable = False
                if reconnect_stop_event.wait(timeout=10):
                    break
                
        reconnect_thread = threading.Thread(target=reconnect_worker, daemon=True)
        reconnect_thread.start()

def monitor_tunnel_loop():
    global tunnel_process
    while True:
        try:
            if state["vpn_connected"] or state["tunnel_pid"] is not None:
                if tunnel_process is not None:
                    code = tunnel_process.poll()
                    if code is not None:
                        add_log(f"Mendeteksi proses tunnel telah berhenti secara tidak terduga (exit code {code}).", "warning", user_visible=False)
                        stop_tunnel_process()
                        invalidate_gateway_session(f"Proses tunnel terhenti (exit code {code}).")
                        trigger_auto_reconnect(f"Proses tunnel terhenti (exit code {code})")
                else:
                    add_log("Mendeteksi proses tunnel hilang.", "warning", user_visible=False)
                    set_vpn_connected(False)
                    state["tunnel_pid"] = None
                    invalidate_gateway_session("Proses tunnel hilang.")
        except Exception:
            pass
        time.sleep(2)

def ping_loop():
    from src.gp import is_gp_mode
    if is_gp_mode():
        return
    if not PING_TARGET or PING_INTERVAL <= 0:
        return
    add_log(f"Background ping worker started targeting {PING_TARGET} every {PING_INTERVAL}s.", "info", user_visible=False)
    
    # Tunggu beberapa detik agar routing/interface benar-benar aktif setelah terkoneksi
    time.sleep(5)
    
    consecutive_failures = 0
    while not ping_stop_event.is_set():
        if state["vpn_connected"]:
            try:
                cmd = ["ping", "-n" if os.name == "nt" else "-c", "1", PING_TARGET]
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                if res.returncode == 0:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
            except Exception:
                consecutive_failures += 1
                
            if consecutive_failures >= 3:
                add_log(f"Ping ke {PING_TARGET} gagal 3 kali berturut-turut. Terowongan VPN diduga terputus.", "warning", user_visible=True)
                stop_tunnel_process()
                invalidate_gateway_session("Koneksi terputus (Ping timeout).")
                trigger_auto_reconnect("Ping timeout")
                break
        else:
            consecutive_failures = 0
            
        for _ in range(PING_INTERVAL):
            if ping_stop_event.is_set():
                break
            time.sleep(1)
    add_log("Background ping worker stopped.", "info", user_visible=False)

def set_vpn_connected(connected):
    global ping_thread
    state["vpn_connected"] = connected
    update_tray_status()
    if connected:
        from src.gp import is_gp_mode
        if not is_gp_mode() and PING_TARGET and PING_INTERVAL > 0:
            if ping_thread is None or not ping_thread.is_alive():
                ping_stop_event.clear()
                ping_thread = threading.Thread(target=ping_loop, daemon=True)
                ping_thread.start()
        try:
            bypass_manager.start_periodic_refresh()
        except Exception as e:
            add_log(f"Gagal memulai bypass manager: {e}", "warning", user_visible=False)
    else:
        ping_stop_event.set()
        try:
            bypass_manager.stop_periodic_refresh()
        except Exception as e:
            add_log(f"Gagal menghentikan bypass manager: {e}", "warning", user_visible=False)

def reset_vpn_artifacts(keep_connect_request=False):
    stop_tunnel_process()
    if not keep_connect_request:
        state["connect_requested"] = False
    state["svpn_cookie_found"] = False
    state["vpn_config_loaded"] = False
    state["vpn_config_summary"] = {}
    state["last_error"] = None
    vpn_session.cookies.clear()

def get_svpn_cookie():
    return vpn_session.cookies.get(SESSION_COOKIE_NAME)

def invalidate_gateway_session(reason):
    set_vpn_connected(False)
    state["connect_requested"] = False
    state["sso_completed"] = False
    state["svpn_cookie_found"] = False
    state["vpn_config_loaded"] = False
    state["vpn_config_summary"] = {}
    state["last_error"] = reason
    vpn_session.cookies.clear()

def clear_login_session(reason=None):
    state["connect_requested"] = False
    state["sso_in_progress"] = False
    state["sso_completed"] = False
    state["svpn_cookie_found"] = False
    state["vpn_config_loaded"] = False
    state["vpn_config_summary"] = {}
    state["last_callback"] = None
    state["last_callback_query"] = {}
    state["last_auth_id"] = None
    state["sso_url"] = None
    state["last_error"] = reason
    vpn_session.cookies.clear()

import shutil
def find_tunnel_engine():
    candidates = [
        OPENCONNECT_PATH,
    ]
    candidates.extend(shutil.which(name) for name in OPENCONNECT_BINARY_NAMES)
    candidates.extend(OPENCONNECT_SEARCH_PATHS)

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return ("openconnect", candidate)

    return (None, None)

def should_suppress_openconnect_line(line):
    return (
        "Unexpected pre-PPP packet header for encap 5." in line
        or "GFtype" in line and "heartbe" in line
        or line.startswith("< 0010:") and "61 74 00" in line
    )

def build_openconnect_extra_args():
    args = list(OPENCONNECT_EXTRA_ARGS)
    normalized = {arg.lower() for arg in args}
    if OPENCONNECT_AUTO_NO_DTLS and "--no-dtls" not in normalized:
        args.append("--no-dtls")
    return args

def configure_tunnel_interface(iface_name=None):
    if os.name != "nt":
        return
    if not iface_name:
        iface_name = TUNNEL_INTERFACE or "VPN Helper"
    try:
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        subprocess.run(
            ["netsh", "interface", "ipv4", "set", "interface", iface_name, "metric=1"],
            capture_output=True,
            timeout=5,
            creationflags=flags
        )
        ps_cmd = (
            f"Set-DnsClient -InterfaceAlias '{iface_name}' -ConnectionSpecificSuffix 'bps.go.id' -ErrorAction SilentlyContinue; "
            "Clear-DnsClientCache"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            timeout=10,
            creationflags=flags
        )
        add_log(f"Adapter '{iface_name}' dioptimalkan (Metric=1, Suffix=bps.go.id, DNS flushed).", "info", user_visible=True)
    except Exception as e:
        add_log(f"Gagal mengonfigurasi adapter tunnel: {e}", "warning", user_visible=True)


def enforce_dns_on_physical_adapter(dns_servers=None):
    """
    Set DNS server pada adapter fisik aktif ke DNS internal VPN (10.10.11.11, 10.10.11.12).
    Wintun adapter tidak muncul di DNS resolver Windows, sehingga kita perlu inject
    DNS server langsung ke adapter fisik sementara VPN aktif.
    DNS lama disimpan ke state agar bisa di-restore saat disconnect.
    """
    if os.name != "nt":
        return
    if dns_servers is None:
        dns_servers = ["10.10.11.11", "10.10.11.12"]
    try:
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        # Cari adapter fisik yang punya default gateway (adapter aktif)
        ps_find = (
            "Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
            "Sort-Object -Property RouteMetric | "
            "Select-Object -First 1 -ExpandProperty InterfaceAlias"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_find],
            capture_output=True, text=True, timeout=5, creationflags=flags
        )
        phys_iface = result.stdout.strip()
        if not phys_iface or phys_iface == (TUNNEL_INTERFACE or "VPN Helper"):
            # Fallback: ambil adapter Ethernet/Wi-Fi pertama yang Up
            ps_fallback = (
                "Get-NetAdapter | Where-Object { $_.Status -eq 'Up' -and $_.InterfaceDescription -notmatch 'Wintun|OpenConnect|Virtual|Loopback|Bluetooth' } | "
                "Select-Object -First 1 -ExpandProperty Name"
            )
            result2 = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_fallback],
                capture_output=True, text=True, timeout=5, creationflags=flags
            )
            phys_iface = result2.stdout.strip()
        if not phys_iface:
            add_log("Tidak dapat menemukan adapter fisik aktif untuk inject DNS.", "warning", user_visible=False)
            return
        # Simpan DNS lama
        ps_get_dns = f"(Get-DnsClientServerAddress -InterfaceAlias '{phys_iface}' -AddressFamily IPv4).ServerAddresses -join ','"
        result3 = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_get_dns],
            capture_output=True, text=True, timeout=5, creationflags=flags
        )
        old_dns = result3.stdout.strip()
        state["_gp_phys_iface"] = phys_iface
        state["_gp_old_dns"] = old_dns
        # Set DNS internal VPN
        ps_set = f"Set-DnsClientServerAddress -InterfaceAlias '{phys_iface}' -ServerAddresses {','.join(dns_servers)}"
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_set],
            capture_output=True, timeout=5, creationflags=flags
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Clear-DnsClientCache"],
            capture_output=True, timeout=5, creationflags=flags
        )
        add_log(f"DNS pada '{phys_iface}' dialihkan ke {dns_servers} (semula: {old_dns or 'DHCP'}).", "info", user_visible=True)
    except Exception as e:
        add_log(f"Gagal inject DNS ke adapter fisik: {e}", "warning", user_visible=False)


def restore_physical_adapter_dns():
    """
    Restore DNS adapter fisik ke konfigurasi semula setelah VPN GP disconnect.
    """
    if os.name != "nt":
        return
    phys_iface = state.get("_gp_phys_iface")
    old_dns = state.get("_gp_old_dns", "")
    if not phys_iface:
        return
    try:
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        if old_dns:
            dns_list = ",".join(f'"{d}"' for d in old_dns.split(",") if d.strip())
            ps_restore = f"Set-DnsClientServerAddress -InterfaceAlias '{phys_iface}' -ServerAddresses {dns_list}"
        else:
            # Tidak ada DNS lama → kembalikan ke DHCP
            ps_restore = f"Set-DnsClientServerAddress -InterfaceAlias '{phys_iface}' -ResetServerAddresses"
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_restore],
            capture_output=True, timeout=5, creationflags=flags
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Clear-DnsClientCache"],
            capture_output=True, timeout=5, creationflags=flags
        )
        add_log(f"DNS pada '{phys_iface}' dikembalikan ke semula ({old_dns or 'DHCP'}).", "info", user_visible=True)
    except Exception as e:
        add_log(f"Gagal restore DNS adapter fisik: {e}", "warning", user_visible=False)
    finally:
        state.pop("_gp_phys_iface", None)
        state.pop("_gp_old_dns", None)

def read_tunnel_output(proc):
    if not proc.stdout:
        return
    while True:
        try:
            raw_line = proc.stdout.readline()
            if not raw_line:
                break
            line = raw_line.strip()
            if not line:
                continue
            if should_suppress_openconnect_line(line):
                continue

            add_log(f"openconnect: {line}", "info", user_visible=False)

            # Intercept authcookie dari dump HTTP — dari body POST getconfig/hipreportcheck/tunnel
            if not state.get("gp_authcookie"):
                # Cek baris yang mengandung authcookie= dalam format URL-encoded body/querystring
                if "authcookie=" in line:
                    ac_match = re.search(r"authcookie=([a-f0-9]{16,})", line)
                    if ac_match:
                        ac = ac_match.group(1).strip()
                        state["gp_authcookie"] = ac
                        add_log(f"authcookie GP diintercept dari dump openconnect.", "info", user_visible=False)
                # Format XML: <authcookie>xxx</authcookie>
                elif "<authcookie>" in line:
                    ac_match = re.search(r"<authcookie>(.*?)</authcookie>", line)
                    if ac_match:
                        ac = ac_match.group(1).strip()
                        if ac and ac.lower() not in ("", "empty"):
                            state["gp_authcookie"] = ac
                            add_log(f"authcookie GP diintercept dari XML openconnect.", "info", user_visible=False)

            # Intercept portal name dari getconfig response: <portal>GP-Gateway-60-N</portal>
            if "<portal>" in line and not state.get("gp_portal"):
                p_match = re.search(r"<portal>(.*?)</portal>", line)
                if p_match:
                    p = p_match.group(1).strip()
                    if p and p.lower() not in ("", "empty"):
                        state["gp_portal"] = p

            # Intercept md5sum dari hipreportcheck response (format: "md5sum HEXVALUE")
            if "md5sum" in line and not state.get("gp_hip_md5"):
                md5_match = re.search(r"md5sum\s+([a-f0-9]{16,})", line)
                if md5_match:
                    state["gp_hip_md5"] = md5_match.group(1).strip()
                    add_log(f"md5sum HIP diintercept dari openconnect.", "info", user_visible=False)

            if "Set up tun device failed" in line or "Failed to create Wintun session" in line:
                invalidate_gateway_session("Adapter Wintun gagal dibuat. Jalankan SSO ulang sebelum retry.")
                add_log("Gagal membuat adapter Wintun. Silakan jalankan ulang aplikasi sebagai Administrator.", "error", user_visible=True)
            elif "Got HTTP response: HTTP/1.1 302 Found" in line:
                invalidate_gateway_session("Sesi gateway sudah dialihkan ke login. Jalankan SSO ulang.")
            elif "Configured as " in line:
                ip_match = re.search(r"Configured as ([0-9.]+)", line)
                assigned_ip = ip_match.group(1) if ip_match else ""
                state["assigned_ip"] = assigned_ip
                state["vpn_config_loaded"] = True
                state["svpn_cookie_found"] = True
                summary = state.get("vpn_config_summary") or {}
                summary["assigned_addr"] = assigned_ip
                state["vpn_config_summary"] = summary
            elif "route configuration done" in line:
                configure_tunnel_interface(TUNNEL_INTERFACE)
                from src.gp import is_gp_mode, submit_gp_hip_report
                if is_gp_mode():
                    assigned_ip = state.get("assigned_ip", "")
                    threading.Thread(target=submit_gp_hip_report, args=(assigned_ip,), daemon=True).start()
                if not state["vpn_connected"]:
                    set_vpn_connected(True)
                    state["last_error"] = None
                    state["vpn_config_loaded"] = True
                    state["svpn_cookie_found"] = True
                    ip_addr = state.get("assigned_ip", "")
                    add_log(f"VPN Berhasil Terhubung! IP Address: {ip_addr}", "success", user_visible=True)
            elif "Configured 2 DNS servers:" in line:
                dns = line.split("servers:")[-1].strip()
                add_log(f"Server DNS dikonfigurasi ke: {dns}", "info", user_visible=True)
            elif "Cookie was rejected by server" in line:
                from src.gp import clear_gp_session, is_gp_mode, perform_gp_sso
                clear_gp_session()
                resumed = state.get("gp_resumed_session", False)
                state["gp_resumed_session"] = False
                invalidate_gateway_session("Cookie ditolak server. Jalankan SSO ulang.")
                if is_gp_mode() and resumed:
                    add_log("Sesi tersimpan tidak valid lagi. Membuka SSO otomatis...", "warning", user_visible=True)
                    threading.Thread(target=perform_gp_sso, daemon=True).start()
                else:
                    trigger_auto_reconnect("Cookie ditolak server")
            elif "Detected dead peer!" in line:
                invalidate_gateway_session("Koneksi terputus (Detected dead peer). Mencoba menyambung kembali...")
                trigger_auto_reconnect("Detected dead peer")
        except Exception as e:
            add_log(f"Gagal memproses baris output tunnel: {e}", "warning", user_visible=False)
            continue

    try:
        code = proc.wait(timeout=2)
    except Exception:
        code = proc.poll()

    state["tunnel_pid"] = None
    if state["vpn_connected"] or state["connect_requested"]:
        invalidate_gateway_session(f"Koneksi VPN terhenti (exit code {code}).")
    add_log(f"Tunnel process selesai dengan exit code {code}.", "warning" if code else "info", user_visible=False)
    add_log("Koneksi VPN terhenti.", "warning", user_visible=True)
    trigger_auto_reconnect(f"Proses tunnel selesai dengan exit code {code}")

def stop_tunnel_process():
    global tunnel_process

    if tunnel_process and tunnel_process.poll() is None:
        try:
            tunnel_process.terminate()
            try:
                tunnel_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel_process.kill()
            add_log("Tunnel process dihentikan.", "warning", user_visible=False)
            add_log("Koneksi VPN dihentikan.", "warning", user_visible=True)
        except Exception as e:
            add_log(f"Gagal menghentikan tunnel process: {e}", "error", user_visible=False)

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "openconnect.exe", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception as e:
            add_log(f"Gagal terminate openconnect.exe: {e}", "warning", user_visible=False)

    tunnel_process = None
    set_vpn_connected(False)
    state["tunnel_pid"] = None
    restore_physical_adapter_dns()

def start_openconnect_tunnel():
    global tunnel_process, tunnel_log_thread

    if tunnel_process and tunnel_process.poll() is None:
        return True

    state["assigned_ip"] = ""

    engine_name, engine_path = find_tunnel_engine()
    state["tunnel_engine"] = engine_name
    if engine_name != "openconnect":
        raise RuntimeError("openconnect belum ditemukan. Set openconnect_path di .env jika lokasinya custom.")

    cookie = get_svpn_cookie()
    if not cookie:
        raise RuntimeError(f"{SESSION_COOKIE_NAME} belum ada. Jalankan SSO dulu sampai Gateway siap.")

    if not state["vpn_config_loaded"]:
        raise RuntimeError("Config gateway belum siap atau sesi sudah invalid. Jalankan SSO ulang.")

    add_log("Memulai koneksi VPN...", "info", user_visible=True)

    target = f"{VPN_HOST}:{VPN_PORT}"
    args = [
        engine_path,
        f"--protocol={OPENCONNECT_PROTOCOL}",
        "--cookie-on-stdin",
        "--no-passwd",
        "--non-inter",
        f"--interface={TUNNEL_INTERFACE}",
        *build_openconnect_extra_args(),
        target,
    ]

    add_log(f"Menjalankan openconnect protocol {OPENCONNECT_PROTOCOL} ke {target}.", "info", user_visible=False)
    add_log("Jika gagal karena permission, jalankan app ini dari terminal Administrator.", "warning", user_visible=False)

    tunnel_process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1
    )

    if tunnel_process.stdin:
        tunnel_process.stdin.write(f"{SESSION_COOKIE_NAME}={cookie}\n")
        tunnel_process.stdin.close()

    tunnel_log_thread = threading.Thread(
        target=read_tunnel_output,
        args=(tunnel_process,),
        daemon=True
    )
    tunnel_log_thread.start()

    set_vpn_connected(False)
    state["tunnel_pid"] = tunnel_process.pid
    add_log(f"Tunnel process dimulai dengan PID {tunnel_process.pid}. Menunggu adapter aktif.", "success", user_visible=False)
    return True

def summarize_vpn_config(xml_text):
    summary = {
        "assigned_addr": None,
        "dns_servers": [],
        "dns_suffix": None,
        "route_count": 0,
        "raw_size": len(xml_text or "")
    }

    root = ET.fromstring(xml_text)

    for node in root.iter():
        tag = node.tag.split("}", 1)[-1]
        text = (node.text or "").strip()

        if tag == "assigned-addr" and text:
            summary["assigned_addr"] = text
        elif tag == "dns":
            ip = node.attrib.get("ip")
            domain = node.attrib.get("domain")
            if ip:
                summary["dns_servers"].append(ip)
            if domain:
                summary["dns_suffix"] = domain
        elif tag == "split-tunnel-info":
            summary["route_count"] += 1

    return summary

def exchange_auth_id_for_cookie(auth_id):
    auth_url = build_saml_auth_id_url(auth_id)
    add_log("Menukar Authentication ID ke sesi SSL-VPN gateway.", "info", user_visible=False)

    response = vpn_session.get(
        auth_url,
        timeout=REQUEST_TIMEOUT,
        verify=VERIFY_TLS,
        allow_redirects=False
    )

    cookie = vpn_session.cookies.get(SESSION_COOKIE_NAME) or response.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        set_cookie = response.headers.get("Set-Cookie", "")
        cookie_prefix = f"{SESSION_COOKIE_NAME}="
        if cookie_prefix in set_cookie:
            cookie = set_cookie.split(cookie_prefix, 1)[1].split(";", 1)[0]
            vpn_session.cookies.set(SESSION_COOKIE_NAME, cookie, domain=VPN_HOST, path="/")

    if not cookie:
        raise RuntimeError(f"{SESSION_COOKIE_NAME} tidak ditemukan dari auth_id endpoint (HTTP {response.status_code}).")

    state["svpn_cookie_found"] = True
    add_log("Cookie sesi gateway diterima. Sesi SSL-VPN siap dipakai oleh engine tunnel.", "success", user_visible=False)
    return cookie

def fetch_vpn_config():
    config_url = build_vpn_config_url()
    add_log("Meminta konfigurasi SSL-VPN dari FortiGate.", "info", user_visible=False)

    response = vpn_session.get(
        config_url,
        timeout=REQUEST_TIMEOUT,
        verify=VERIFY_TLS,
        allow_redirects=False
    )

    if response.status_code != 200:
        raise RuntimeError(f"Gagal mengambil konfigurasi SSL-VPN (HTTP {response.status_code}).")

    summary = summarize_vpn_config(response.text)
    state["vpn_config_loaded"] = True
    state["vpn_config_summary"] = summary
    add_log(
        f"Config SSL-VPN diterima: assigned={summary['assigned_addr'] or '-'}, "
        f"dns={len(summary['dns_servers'])}, routes={summary['route_count']}.",
        "success",
        user_visible=False
    )
    return summary

def perform_headless_sso():
    if not SSO_USERNAME or not SSO_PASSWORD:
        raise RuntimeError("Kredensial SSO (sso_username & sso_password) belum diatur di .env")

    state["sso_in_progress"] = True
    state["connect_requested"] = True
    state["sso_completed"] = False
    state["last_error"] = None
    reset_vpn_artifacts(keep_connect_request=True)

    try:
        start_url = build_saml_start_url()
        add_log("Memulai autentikasi SSO (Headless)...", "info", user_visible=True)
        add_log(f"Memulai SSO Headless ke {start_url}...", "info", user_visible=False)
        
        # Langkah 1: Hubungi Gateway VPN
        res1 = vpn_session.get(start_url, allow_redirects=True, timeout=REQUEST_TIMEOUT, verify=VERIFY_TLS)
        
        # Deteksi JS redirect
        js_match = RE_JS_REDIRECT.search(res1.text)
        if js_match:
            redirect_url = js_match.group(1)
            add_log(f"Mengikuti JS redirect Gateway ke halaman SSO...", "info", user_visible=False)
            res1 = vpn_session.get(redirect_url, allow_redirects=True, timeout=REQUEST_TIMEOUT, verify=VERIFY_TLS)
        
        # Langkah 2: Cari URL POST Form Login di HTML Keycloak
        action_match = RE_ACTION_URL.search(res1.text)
        if not action_match:
            raise RuntimeError("Gagal menemukan form login SSO di halaman HTML (mungkin diblokir/CAPTCHA)")
            
        action_url = action_match.group(1).replace('&amp;', '&')
        
        # Langkah 3: POST Kredensial SSO
        add_log("Mengirim kredensial autentikasi ke server SSO...", "info", user_visible=True)
        payload = {
            'username': SSO_USERNAME,
            'password': SSO_PASSWORD,
            'credentialId': ''
        }
        res2 = vpn_session.post(action_url, data=payload, allow_redirects=False, timeout=REQUEST_TIMEOUT, verify=VERIFY_TLS)
        
        html_content = res2.text
        if res2.status_code == 302:
            redirect_url = res2.headers.get("Location")
            res2 = vpn_session.get(redirect_url, allow_redirects=False, timeout=REQUEST_TIMEOUT, verify=VERIFY_TLS)
            html_content = res2.text
            
        # Langkah 4: Ekstrak SAMLResponse dari HTML
        saml_response_match = RE_SAML_RESPONSE.search(html_content)
        acs_url_match = RE_ACTION_URL.search(html_content)
        relay_state_match = RE_RELAY_STATE.search(html_content)
        
        if not saml_response_match or not acs_url_match:
            html_lower = html_content.lower()
            error_keywords = [
                "invalid username or password",
                "kredensial tidak valid",
                "username atau password salah",
                "kredensial anda salah",
                "username or password is_invalid",
                "invalid_credentials",
                "login/password salah",
                "login atau password salah",
                "incorrect username or password"
            ]
            if any(kw in html_lower for kw in error_keywords):
                raise InvalidCredentialsError("Kredensial anda mungkin salah. Harap perbaiki kredensial anda terlebih dahulu")
            
            otp_keywords = [
                "otp", "totp", "authenticator", "2fa", "two-factor",
                "kode verifikasi", "one-time password", "passcode"
            ]
            if any(kw in html_lower for kw in otp_keywords):
                raise OTPRequiredError("Autentikasi OTP/2FA terdeteksi. Silakan selesaikan login di browser.")

            raise RuntimeError("Gagal mengekstrak SAMLResponse dari Keycloak (cek kredensial atau ada Captcha/OTP)")

            
        saml_response = saml_response_match.group(1)
        relay_state = relay_state_match.group(1) if relay_state_match else ""
        acs_url = acs_url_match.group(1).replace('&amp;', '&')
        
        # Langkah 5: POST SAMLResponse kembali ke Gateway ACS
        add_log("Verifikasi respon SAML dengan gateway...", "info", user_visible=True)
        post_data = {
            'SAMLResponse': saml_response,
            'RelayState': relay_state
        }
        res3 = vpn_session.post(acs_url, data=post_data, allow_redirects=True, timeout=REQUEST_TIMEOUT, verify=VERIFY_TLS)
        
        # Langkah 6: Ekstrak callback ID dan tukar dengan Cookie
        id_match = RE_CALLBACK_ID.search(res3.text)
        if not id_match:
            raise RuntimeError("Tidak menemukan ID callback dari respon ACS Gateway")
            
        auth_id = id_match.group(1)
        state["last_auth_id"] = auth_id
        state["sso_completed"] = True
        state["sso_in_progress"] = False
        
        exchange_auth_id_for_cookie(auth_id)
        
        # Optimasi: Set config_loaded ke True dan langsung start openconnect
        state["vpn_config_loaded"] = True
        
        add_log("Autentikasi SSO berhasil! Menyiapkan koneksi...", "success", user_visible=True)
        start_openconnect_tunnel()
        
        # Ambil konfigurasi XML di background thread untuk memperbarui info UI
        def fetch_config_bg():
            try:
                fetch_vpn_config()
            except Exception:
                pass
        threading.Thread(target=fetch_config_bg, daemon=True).start()
        
        return True
        
    except Exception as e:
        state["sso_in_progress"] = False
        state["connect_requested"] = False
        clean_err = format_user_error(e)
        state["last_error"] = clean_err
        if isinstance(e, InvalidCredentialsError):
            config.AUTOMATE = False
            update_env_variable("automate", False)
            add_log(clean_err, "error", user_visible=True)
            add_log("Mode otomatis dinonaktifkan.", "info", user_visible=True)
        elif isinstance(e, OTPRequiredError):
            add_log(clean_err, "warning", user_visible=True)
        else:
            add_log(f"Autentikasi gagal: {clean_err}", "error", user_visible=True)

        raise e

def auto_connect_on_start():
    time.sleep(3.0)
    if not config.AUTOMATE or DEV_MODE != "pointcoffee":
        return
    add_log("Mencoba auto-connect VPN pada startup secara headless...", "info")
    trigger_auto_reconnect("Startup aplikasi")
