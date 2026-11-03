import os
from dotenv import load_dotenv
from urllib.parse import quote
from src.state import add_log

import base64
import hashlib
from cryptography.fernet import Fernet

DOTENV_PATH = os.getenv("dotenv_path", ".env")

if not os.path.exists(DOTENV_PATH) and os.path.exists(".env.enc"):
    print("\n--- Setup Konfigurasi VPN Helper ---")
    answer = input("Siapa sestama yang dilantik di 2026? ").strip().lower()
    try:
        key = base64.urlsafe_b64encode(hashlib.sha256(answer.encode()).digest())
        fernet = Fernet(key)
        with open(".env.enc", "rb") as ef:
            decrypted_data = fernet.decrypt(ef.read())
        with open(DOTENV_PATH, "wb") as df:
            df.write(decrypted_data)
        print("Kunci benar! File .env berhasil dibuat.\n")
    except Exception:
        print("Jawaban salah! Gagal mendekripsi .env.enc.\n")

load_dotenv(DOTENV_PATH)


def env_str(name, default=""):
    return os.getenv(name, default).strip()

def env_int(name, default):
    raw = env_str(name, str(default))
    try:
        return int(raw)
    except ValueError:
        return default

def env_bool(name, default=False):
    raw = env_str(name, "true" if default else "false").lower()
    return raw not in {"0", "false", "no", "off"}

def env_list(name, default, sep=";"):
    raw = env_str(name, default)
    return [item.strip() for item in raw.split(sep) if item.strip()]

# Load Configs
APP_HOST = env_str("app_host", "127.0.0.1")
APP_PORT = env_int("app_port", 5000)
APP_DEBUG = env_bool("app_debug", False)
CALLBACK_HOST = env_str("callback_host", "127.0.0.1")
CALLBACK_PORT = env_int("callback_port", 8020)
CALLBACK_DEBUG = env_bool("callback_debug", False)
USE_RELOADER = env_bool("use_reloader", False)

VPN_SCHEME = env_str("vpn_scheme", "https")
VPN_HOST = env_str("vpn_host")
GP_HOST = env_str("gp_host", "vpn.bps.go.id")
GP_GATEWAY = env_str("gp_gateway", "vpn-gw01.bps.go.id")
GP_PORT = env_str("gp_port", "443")
GP_PROTOCOL = env_str("gp_protocol", "gp")
GP_GATEWAY_PRELOGIN_PATH = env_str("gp_gateway_prelogin_path", "/ssl-vpn/prelogin.esp")
GP_PORTAL_PRELOGIN_PATH = env_str("gp_portal_prelogin_path", "/global-protect/prelogin.esp")
GP_SESSION_COOKIE = env_str("gp_session_cookie", "SESSID")
VPN_PORT = env_str("vpn_port")
VERIFY_TLS = env_bool("verify_tls", True)
TLS_CERT_PATH = env_str("tls_cert_path", "")
REQUEST_TIMEOUT = env_int("request_timeout", 25)

SAML_START_PATH = env_str("saml_start_path")
SAML_START_QUERY = env_str("saml_start_query")
SAML_AUTH_ID_PATH = env_str("saml_auth_id_path")
SAML_AUTH_ID_PARAM = env_str("saml_auth_id_param")
VPN_CONFIG_PATH = env_str("vpn_config_path")
VPN_CONFIG_QUERY = env_str("vpn_config_query")
SESSION_COOKIE_NAME = env_str("session_cookie_name")

HTTP_USER_AGENT = env_str("http_user_agent", "Mozilla/5.0 SV1")
HTTP_ACCEPT = env_str("http_accept", "*/*")
HTTP_PRAGMA = env_str("http_pragma", "no-cache")

OPENCONNECT_PATH = env_str("openconnect_path", "")
OPENCONNECT_BINARY_NAMES = env_list("openconnect_binary_names", "openconnect;openconnect.exe")
OPENCONNECT_SEARCH_PATHS = env_list(
    "openconnect_search_paths",
    r"C:\Program Files\OpenConnect-GUI\openconnect.exe;C:\Program Files (x86)\OpenConnect-GUI\openconnect.exe"
)
OPENCONNECT_PROTOCOL = env_str("openconnect_protocol")
TUNNEL_INTERFACE = env_str("tunnel_interface")
OPENCONNECT_EXTRA_ARGS = env_list("openconnect_extra_args", "")
OPENCONNECT_AUTO_NO_DTLS = env_bool("openconnect_auto_no_dtls", OPENCONNECT_PROTOCOL.lower() == "fortinet")
PING_TARGET = env_str("ping_target", "10.10.11.11")
PING_INTERVAL = env_int("ping_interval", 30)
SSO_USERNAME = env_str("sso_username")
SSO_PASSWORD = env_str("sso_password")
DEV_MODE = env_str("dev", "momoyo")
AUTOMATE = env_bool("automate", False) if DEV_MODE == "pointcoffee" else False

LOG_MAX_ENTRIES = env_int("log_max_entries", 500)
STATUS_LOG_LIMIT = env_int("status_log_limit", 80)
MASK_KEEP_START = env_int("mask_keep_start", 6)
MASK_KEEP_END = env_int("mask_keep_end", 4)
ENABLE_TRAY = env_bool("enable_tray", os.name == "nt")
AUTO_HIDE_CONSOLE = env_bool("auto_hide_console", os.name == "nt")

BYPASS_ENABLED = env_bool("bypass_enabled", True)
BYPASS_APPS = env_list("bypass_apps", r"C:\Program Files\RustDesk\rustdesk.exe")
BYPASS_DOMAINS = env_list("bypass_domains", "rs-ny.rustdesk.com")

# Sync state module fallbacks
from src.state import BASE_DIR
import src.state as state_mod
state_mod.LOG_MAX_ENTRIES = LOG_MAX_ENTRIES
state_mod.MASK_KEEP_START = MASK_KEEP_START
state_mod.MASK_KEEP_END = MASK_KEEP_END

REQUIRED_INTERNAL_CONFIG = {
    "vpn_host": VPN_HOST,
    "vpn_port": VPN_PORT,
    "saml_start_path": SAML_START_PATH,
    "saml_auth_id_path": SAML_AUTH_ID_PATH,
    "saml_auth_id_param": SAML_AUTH_ID_PARAM,
    "vpn_config_path": VPN_CONFIG_PATH,
    "session_cookie_name": SESSION_COOKIE_NAME,
    "openconnect_protocol": OPENCONNECT_PROTOCOL,
    "tunnel_interface": TUNNEL_INTERFACE,
}

def update_env_variable(key, value):
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env_path):
        return
        
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        key_upper = key.upper()
        key_lower = key.lower()
        found = False
        new_lines = []
        
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("#") and "=" in stripped:
                parts = stripped.split("=", 1)
                k = parts[0].strip()
                if k.upper() == key_upper:
                    new_lines.append(f"{k}={str(value).lower()}\n")
                    found = True
                    continue
            new_lines.append(line)
            
        if not found:
            new_lines.append(f"{key_lower}={str(value).lower()}\n")
            
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
            
    except Exception as e:
        add_log(f"Gagal menulis ke .env: {e}", "warning")

def ensure_internal_config():
    missing = [name for name, value in REQUIRED_INTERNAL_CONFIG.items() if not value]
    if missing:
        raise RuntimeError(
            "Konfigurasi internal belum lengkap. Ambil .env internal yang benar dulu."
        )

def build_gateway():
    ensure_internal_config()
    return f"{VPN_SCHEME}://{VPN_HOST}:{VPN_PORT}"

def build_saml_start_url():
    query = f"?{SAML_START_QUERY}" if SAML_START_QUERY else ""
    return f"{build_gateway()}{SAML_START_PATH}{query}"

def build_saml_auth_id_url(auth_id):
    return f"{build_gateway()}{SAML_AUTH_ID_PATH}?{SAML_AUTH_ID_PARAM}={quote(auth_id, safe='')}"

def build_vpn_config_url():
    query = f"?{VPN_CONFIG_QUERY}" if VPN_CONFIG_QUERY else ""
    return f"{build_gateway()}{VPN_CONFIG_PATH}{query}"

def build_app_url():
    return f"http://{APP_HOST}:{APP_PORT}"

def build_callback_url():
    return f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/"
