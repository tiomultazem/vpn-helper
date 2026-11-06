"""
GlobalProtect (PaloAlto) VPN backend module.
Menangani prelogin.esp, SAML flow, dan tunnel via OpenConnect --protocol=gp.
Terpisah dari logika Fortinet di vpn.py.
"""
import os
import sys
import json
import time as _time
import base64
import threading
import requests
import xml.etree.ElementTree as ET

from src.state import state, add_log, update_tray_status, BASE_DIR
from src import config
from src.vpn import (
    find_tunnel_engine, start_openconnect_tunnel,
    stop_tunnel_process, set_vpn_connected,
    build_openconnect_extra_args
)
from src import tray

import shutil
_logo_src = os.path.join(BASE_DIR, "assets", "logo.png")
_fav_src = os.path.join(BASE_DIR, "assets", "favicon.ico")
_ext_dir = os.path.join(BASE_DIR, "extension")
try:
    os.makedirs(_ext_dir, exist_ok=True)
    if os.path.isfile(_logo_src):
        shutil.copy(_logo_src, os.path.join(_ext_dir, "logo.png"))
    if os.path.isfile(_fav_src):
        shutil.copy(_fav_src, os.path.join(_ext_dir, "favicon.ico"))
except Exception:
    pass

GP_PORT = config.GP_PORT
GP_PROTOCOL = config.GP_PROTOCOL
GP_GATEWAY_PRELOGIN_PATH = config.GP_GATEWAY_PRELOGIN_PATH
GP_PORTAL_PRELOGIN_PATH = config.GP_PORTAL_PRELOGIN_PATH
GP_SESSION_COOKIE = config.GP_SESSION_COOKIE
GP_SESSION_FILE = os.path.join(BASE_DIR, ".gp_session.json")

# Requests session khusus GlobalProtect
gp_session = requests.Session()
gp_session.headers.update({
    "User-Agent": "PAN GlobalProtect/6.0.7-14 (Windows 10)",
    "Accept": "*/*",
})

# State flag mode aktif
_gp_mode_active = False


def is_gp_mode():
    return _gp_mode_active


def set_gp_mode(val: bool):
    global _gp_mode_active
    _gp_mode_active = val


# ─── Session Persistence ────────────────────────────────────────────────────

def save_gp_session():
    """
    Simpan authcookie GP ke file lokal agar bisa resume tanpa SSO ulang.
    authcookie valid ±72 jam (sesuai respons server).
    """
    authcookie = state.get("gp_authcookie")
    if not authcookie:
        return
    username = state.get("gp_username") or config.SSO_USERNAME or ""
    data = {
        "authcookie": authcookie,
        "username": username,
        "gateway": config.GP_GATEWAY,          # hostname untuk koneksi: vpn-gw01.bps.go.id
        "portal_name": state.get("gp_portal") or config.GP_HOST,  # nama portal dari server
        "expires_at": _time.time() + 72 * 3600,
    }
    try:
        with open(GP_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        add_log("Sesi GP disimpan (berlaku 72 jam). Reconnect berikutnya tidak perlu SSO ulang.", "info", user_visible=False)
    except Exception as e:
        add_log(f"Gagal simpan sesi GP: {e}", "warning", user_visible=False)


def load_gp_session() -> dict | None:
    """Muat sesi GP tersimpan jika masih valid. Return dict atau None."""
    if not os.path.exists(GP_SESSION_FILE):
        return None
    try:
        with open(GP_SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if _time.time() > data.get("expires_at", 0):
            os.remove(GP_SESSION_FILE)
            add_log("Sesi GP tersimpan sudah expired, dihapus.", "info", user_visible=False)
            return None
        return data
    except Exception:
        return None


def clear_gp_session():
    """Hapus file sesi GP tersimpan (dipanggil saat session ditolak server)."""
    try:
        if os.path.exists(GP_SESSION_FILE):
            os.remove(GP_SESSION_FILE)
            add_log("Sesi GP tersimpan dihapus.", "info", user_visible=False)
    except Exception:
        pass


def try_resume_gp_session() -> bool:
    """
    Coba resume sesi GP via Python HTTP langsung ke login.esp dengan portal-userauthcookie tersimpan.
    Jika server return authcookie baru → start tunnel normal.
    Return True jika tunnel berhasil dimulai.
    """
    session = load_gp_session()
    if not session:
        return False

    saved_authcookie = session.get("authcookie", "")
    username = session.get("username", "") or config.SSO_USERNAME or ""
    gateway = session.get("gateway") or session.get("portal") or config.GP_GATEWAY
    if not gateway or "." not in gateway:
        gateway = config.GP_GATEWAY

    if not saved_authcookie or not username:
        return False

    remaining_h = max(0, int((session.get("expires_at", 0) - _time.time()) / 3600))
    add_log(
        f"Sesi GP tersimpan ditemukan (user={username}, sisa ±{remaining_h} jam). Mencoba resume...",
        "info", user_visible=True
    )

    # Coba login.esp dengan portal-userauthcookie (authcookie dari sesi sebelumnya)
    url = f"https://{gateway}:{GP_PORT}/ssl-vpn/login.esp"
    payload = {
        "user": username,
        "passwd": "",
        "inputStr": "",
        "clientVer": "4100",
        "clientos": "Windows",
        "clientgpversion": "6.3.3-1121",
        "portal-userauthcookie": saved_authcookie,
        "portal-prelogonuserauthcookie": "",
        "prelogin-cookie": "",
        "computer": "localhost",
        "server": gateway,
    }
    try:
        r = gp_session.post(url, data=payload, verify=config.VERIFY_TLS, timeout=15)
        root = ET.fromstring(r.text)
        new_authcookie = root.findtext("authcookie", "")
        if new_authcookie and new_authcookie.lower() not in ("", "empty"):
            add_log("Resume sesi GP berhasil — authcookie baru diperoleh.", "success", user_visible=True)
            state["gp_resumed_session"] = True
            # Update sesi tersimpan dengan authcookie baru
            state["gp_username"] = username
            start_gp_tunnel(
                token=new_authcookie,
                username=username,
                usergroup="gateway:prelogin-cookie"
            )
            return True
        else:
            status = root.findtext("status", "unknown")
            add_log(f"Resume sesi GP ditolak server (status={status}). Perlu SSO ulang.", "warning", user_visible=True)
            clear_gp_session()
            return False
    except Exception as e:
        add_log(f"Resume sesi GP error: {e}. Fallback ke SSO.", "warning", user_visible=True)
        clear_gp_session()
        return False



def get_prelogin_saml_request(host: str = None) -> str:
    """
    POST ke ssl-vpn/prelogin.esp pada gateway GP, parse XML, kembalikan SAML HTML form (base64-decoded).
    """
    if not host:
        host = config.GP_GATEWAY
    url = f"https://{host}:{GP_PORT}{GP_GATEWAY_PRELOGIN_PATH}?tmp=tmp&clientVer=4100&clientos=Windows"
    try:
        r = gp_session.post(url, verify=config.VERIFY_TLS, timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
    except requests.exceptions.SSLError as e:
        raise RuntimeError(f"TLS error saat prelogin: {e}")
    except Exception as e:
        raise RuntimeError(f"Gagal menghubungi gateway {host}: {e}")

    root = ET.fromstring(r.text)
    status = root.findtext("status", "")
    if status.lower() != "success":
        raise RuntimeError(f"prelogin.esp mengembalikan status: {status}")

    saml_request_b64 = root.findtext("saml-request", "")
    if not saml_request_b64:
        raise RuntimeError("Tidak ada saml-request di respons prelogin.esp.")

    try:
        html = base64.b64decode(saml_request_b64).decode("utf-8", errors="replace")
    except Exception as e:
        raise RuntimeError(f"Gagal decode saml-request: {e}")

    return html


def get_gp_cookie() -> str | None:
    return gp_session.cookies.get(GP_SESSION_COOKIE)


def fetch_gp_authcookie(prelogin_cookie: str, username: str, gateway: str = None) -> str | None:
    """
    POST ke login.esp gateway GP secara langsung menggunakan prelogin-cookie SAML.
    Kembalikan authcookie (session token) dari XML response.
    authcookie ini yang dibutuhkan oleh hipreport.esp, bukan prelogin-cookie.
    """
    if not gateway:
        gateway = config.GP_GATEWAY
    portal = config.GP_HOST
    computer = os.environ.get("COMPUTERNAME", "DESKTOP-1NF095D")

    url = f"https://{gateway}:{GP_PORT}/ssl-vpn/login.esp"
    payload = {
        "user": username,
        "passwd": "",
        "inputStr": "",
        "clientVer": "4100",
        "clientos": "Windows",
        "clientgpversion": "6.3.3-1121",
        "portal-prelogonuserauthcookie": "",
        "portal-userauthcookie": "",
        "prelogin-cookie": prelogin_cookie,
        "computer": computer,
        "server": gateway,
    }

    try:
        r = gp_session.post(url, data=payload, verify=config.VERIFY_TLS, timeout=15)
        root = ET.fromstring(r.text)
        authcookie = root.findtext("authcookie", "")
        if authcookie and authcookie.lower() not in ("", "empty"):
            add_log(f"authcookie GP berhasil diperoleh dari login.esp.", "info", user_visible=False)
            return authcookie
        else:
            status = root.findtext("status", "unknown")
            add_log(f"login.esp tidak mengembalikan authcookie (status={status}). HIP report akan gagal.", "warning", user_visible=False)
            return None
    except Exception as e:
        add_log(f"Gagal fetch authcookie dari login.esp: {e}", "warning", user_visible=False)
        return None


def start_gp_tunnel(portal: str = config.GP_HOST, token: str = None, username: str = None, usergroup: str = None):
    """
    Mulai OpenConnect tunnel dengan protocol=gp menggunakan token autentikasi.
    """
    engine_name, engine_path = find_tunnel_engine()
    if engine_name != "openconnect":
        raise RuntimeError("openconnect tidak ditemukan.")

    if not token:
        token = get_gp_cookie()
    if not token:
        raise RuntimeError(f"Token sesi belum ada. Selesaikan login SSO terlebih dahulu.")

    if not username:
        username = config.SSO_USERNAME

    state["gp_authcookie"] = None  # akan diisi setelah tunnel terbentuk via intercept
    state["gp_hip_md5"] = None     # akan diisi saat md5sum diintercept dari hipreportcheck
    # gp_resumed_session diset oleh try_resume_gp_session() sebelum memanggil fungsi ini
    # Jangan reset di sini — biarkan vpn.py membacanya untuk fallback ke SSO jika sesi ditolak

    gateway = config.GP_GATEWAY
    target = f"{gateway}:{GP_PORT}"
    add_log(f"Memulai tunnel GlobalProtect ke gateway {target}...", "info", user_visible=True)

    import subprocess
    args = [
        engine_path,
        f"--protocol={GP_PROTOCOL}",
        "--os=win",
        "--passwd-on-stdin",
        "--non-inter",
        "--dump-http-traffic",  # agar response login.esp (authcookie) tercetak di stdout
        f"--interface={config.TUNNEL_INTERFACE}",
    ]
    if username:
        args.append(f"--user={username}")
    if usergroup:
        args.append(f"--usergroup={usergroup}")

    computer = os.environ.get("COMPUTERNAME", "DESKTOP-1NF095D")
    args.append(f"--local-hostname={computer}")

    gp_extra_args = [arg for arg in build_openconnect_extra_args() if arg.lower() != "--no-dtls"]
    args.extend(gp_extra_args)
    args.append(target)

    import src.vpn as vpn_mod
    vpn_mod.tunnel_process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1
    )

    if vpn_mod.tunnel_process.stdin:
        vpn_mod.tunnel_process.stdin.write(f"{token}\n")
        vpn_mod.tunnel_process.stdin.close()

    vpn_mod.tunnel_log_thread = threading.Thread(
        target=vpn_mod.read_tunnel_output,
        args=(vpn_mod.tunnel_process,),
        daemon=True
    )
    vpn_mod.tunnel_log_thread.start()

    if token:
        state["gp_token"] = token
    if username:
        state["gp_username"] = username

    set_vpn_connected(False)
    state["tunnel_pid"] = vpn_mod.tunnel_process.pid
    add_log(f"GP tunnel PID {vpn_mod.tunnel_process.pid} dimulai.", "success", user_visible=False)


def submit_gp_hip_report(assigned_ip: str):
    """
    Kirim laporan Host Information Profile (HIP) ke Palo Alto gateway.
    Tanpa laporan ini, firewall GlobalProtect memblokir seluruh akses data tunnel.
    """
    if not assigned_ip:
        return

    # Prioritas: authcookie yang diintercept dari openconnect stdout
    authcookie = state.get("gp_authcookie") or state.get("gp_token") or get_gp_cookie()
    username = state.get("gp_username") or config.SSO_USERNAME or "gilangprasetyo"
    gateway = config.GP_GATEWAY
    # Portal harus sesuai yang dikembalikan getconfig.esp (misal: GP-Gateway-60-N), bukan config.GP_HOST
    portal = state.get("gp_portal") or config.GP_HOST
    # openconnect mengirim "localhost" ke hipreportcheck — harus konsisten agar server tidak reject
    computer = os.environ.get("COMPUTERNAME", "DESKTOP-1NF095D")
    hip_md5 = state.get("gp_hip_md5") or ""

    if not authcookie or authcookie == state.get("gp_token"):
        add_log("Peringatan: authcookie belum diintercept dari openconnect — HIP mungkin ditolak server.", "warning", user_visible=False)

    # Simpan sesi untuk reconnect berikutnya (authcookie valid ±72 jam dari server)
    save_gp_session()


    hrpt_path = r"C:\Program Files\Palo Alto Networks\GlobalProtect\pan_gp_hrpt.xml"
    report_xml = ""
    if os.path.isfile(hrpt_path):
        try:
            with open(hrpt_path, "r", encoding="utf-8") as f:
                report_xml = f.read()
            import re
            import datetime
            now_str = datetime.datetime.now().strftime("%m/%d/%Y %H:%M:%S")
            report_xml = re.sub(r"<generate-time>.*?</generate-time>", f"<generate-time>{now_str}</generate-time>", report_xml)
            report_xml = re.sub(r"<ip-address>.*?</ip-address>", f"<ip-address>{assigned_ip}</ip-address>", report_xml)
            # Update IP di entri virtual adapter jika ada IP lama
            report_xml = re.sub(r'(<entry name=")\d+\.\d+\.\d+\.\d+("/>)', rf'\g<1>{assigned_ip}\2', report_xml)
            if username:
                report_xml = re.sub(r"<user-name>.*?</user-name>", f"<user-name>{username}</user-name>", report_xml)
            if computer:
                report_xml = re.sub(r"<host-name>.*?</host-name>", f"<host-name>{computer}</host-name>", report_xml)
            if hip_md5:
                report_xml = re.sub(r"<md5-sum>.*?</md5-sum>", f"<md5-sum>{hip_md5}</md5-sum>", report_xml)
            report_xml = re.sub(r"<real-time-protection>no</real-time-protection>", "<real-time-protection>yes</real-time-protection>", report_xml)
        except Exception as e:
            add_log(f"Gagal membaca pan_gp_hrpt.xml: {e}", "warning", user_visible=False)

    if not report_xml:
        import datetime
        now_str = datetime.datetime.now().strftime("%m/%d/%Y %H:%M:%S")
        report_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<hip-report>
\t<md5-sum>{hip_md5 or "6ec16d97df2ab20bc2f32eb117ffce"}</md5-sum>
\t<user-name>{username}</user-name>
\t<domain></domain>
\t<host-name>{computer}</host-name>
\t<host-id>c880a7d0-a9e5-4a2f-8559-062e63600fba</host-id>
\t<ip-address>{assigned_ip}</ip-address>
\t<generate-time>{now_str}</generate-time>
\t<hip-report-version>4</hip-report-version>
\t<categories>
\t\t<entry name="host-info">
\t\t\t<client-version>6.3.3-1121</client-version>
\t\t\t<os>Microsoft Windows 11 Home Single Language , 64-bit</os>
\t\t\t<os-vendor>Microsoft</os-vendor>
\t\t\t<domain></domain>
\t\t\t<host-name>{computer}</host-name>
\t\t\t<host-id>c880a7d0-a9e5-4a2f-8559-062e63600fba</host-id>
\t\t</entry>
\t</categories>
</hip-report>"""

    url = f"https://{gateway}:{GP_PORT}/ssl-vpn/hipreport.esp"
    payload = {
        "user": username,
        "domain": "",
        "portal": portal,
        "authcookie": authcookie or "",
        "client-ip": assigned_ip,
        "client-ipv6": "",
        "computer": computer,
        "md5": hip_md5,
        "client-role": "global-protect-full",
        "report": report_xml,
    }

    try:
        r = gp_session.post(url, data=payload, verify=config.VERIFY_TLS, timeout=10)
        add_log(f"HIP report response: {r.text[:300]}", "info", user_visible=False)
        if "status=\"success\"" in r.text or "<response status='success'>" in r.text or "status=\"Success\"" in r.text:
            add_log("Laporan HIP berhasil dikirim ke gateway. Akses internal dibuka.", "success", user_visible=True)
            # Tidak menginjeksi DNS ke adapter fisik agar tidak membajak internet lokal
            # Verifikasi DNS setelah HIP sukses
            threading.Thread(target=_verify_dns_post_hip, daemon=True).start()
        else:
            add_log(f"Laporan HIP terkirim tapi belum diterima (status={r.status_code}). Response: {r.text[:200]}", "warning", user_visible=False)
    except Exception as e:
        add_log(f"Gagal mengirim laporan HIP ke gateway: {e}", "warning", user_visible=False)


def _verify_dns_post_hip():
    """
    Cek DNS internal (10.10.11.11) setelah HIP berhasil.
    Gateway GP membuka akses data setelah menerima HIP report yang valid.
    """
    import time
    import subprocess
    time.sleep(3)  # beri gateway waktu apply policy
    target_domain = "fasih-sm.bps.go.id"
    dns_server = "10.10.11.11"
    add_log(f"Verifikasi DNS: resolve {target_domain} via {dns_server}...", "info", user_visible=True)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"Resolve-DnsName {target_domain} -Server {dns_server} -Type A -ErrorAction Stop | Format-Table -AutoSize | Out-String"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            add_log(f"DNS OK: {target_domain} resolved via {dns_server}:\n{result.stdout.strip()}", "success", user_visible=True)
        else:
            err = (result.stderr or result.stdout or "").strip()
            add_log(f"DNS gagal resolve {target_domain} via {dns_server}: {err or 'timeout/NXDOMAIN'}", "warning", user_visible=True)
    except subprocess.TimeoutExpired:
        add_log(f"DNS timeout: {dns_server} tidak merespons — HIP mungkin belum diterima gateway.", "warning", user_visible=True)
    except Exception as e:
        add_log(f"DNS check error: {e}", "warning", user_visible=False)


def perform_gp_sso(portal: str = None):
    """
    Jalankan flow SSO GlobalProtect langsung ke gateway:
    1. Prelogin gateway -> dapat HTML form SAML
    2. Submit form di browser/WebEngine -> user login
    3. Server redirect ke SAML20/SP/ACS -> set cookie gateway
    """
    gateway = config.GP_GATEWAY
    add_log(f"Memulai alur SSO GlobalProtect ke gateway {gateway}...", "info", user_visible=True)
    state["sso_in_progress"] = True
    state["connect_requested"] = True

    try:
        saml_html = get_prelogin_saml_request(gateway)
    except Exception as e:
        state["sso_in_progress"] = False
        state["last_error"] = str(e)
        add_log(str(e), "error", user_visible=True)
        return

    # Tulis form HTML ke endpoint lokal agar bisa dibuka di browser
    state["sso_url"] = f"http://127.0.0.1:{config.APP_PORT}/gp/saml-form"
    state["_gp_saml_html"] = saml_html

    add_log("Membuka browser utama untuk login SSO GlobalProtect...", "info", user_visible=True)
    try:
        from src import tray
        tray.open_app_in_browser(url=state["sso_url"])
        add_log("Browser utama dibuka. Pastikan Extension/Userscript VPN Helper aktif.", "success", user_visible=True)
    except Exception as e:
        state["sso_in_progress"] = False
        add_log(f"Gagal membuka browser: {e}", "error", user_visible=True)

