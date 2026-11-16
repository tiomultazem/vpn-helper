import os
import atexit
import datetime
import threading
import requests
from flask import Flask, render_template, request, jsonify, send_from_directory

# Import modular components
from src import state
from src import config
from src import vpn
from src import tray
from src import updater
from src.bypass import bypass_manager
from src import gp as gp_mod

app = Flask(
    __name__,
    template_folder=os.path.join(state.BASE_DIR, "templates"),
    static_folder=os.path.join(state.BASE_DIR, "static")
)
callback_app = Flask("callback_listener")

ASSETS_DIR = os.path.join(state.BASE_DIR, "assets")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/favicon.ico", endpoint="favicon")
def favicon():
    return send_from_directory(ASSETS_DIR, "favicon.ico", mimetype="image/vnd.microsoft.icon")

@app.route("/assets/<path:filename>", endpoint="assets")
def assets(filename):
    return send_from_directory(ASSETS_DIR, filename)

@app.route("/api/status")
def status():
    return jsonify({
        "vpn_connected": state.state["vpn_connected"],
        "sso_completed": state.state["sso_completed"],
        "svpn_cookie_found": state.state["svpn_cookie_found"],
        "vpn_config_loaded": state.state["vpn_config_loaded"],
        "vpn_config_summary": state.state["vpn_config_summary"],
        "logs": state.state["logs"],
        "vpn_host": config.VPN_HOST,
        "vpn_port": config.VPN_PORT,
        "app_url": config.build_app_url(),
        "callback_url": config.build_callback_url(),
        "sso_in_progress": state.state["sso_in_progress"],
        "last_callback": state.state["last_callback"],
        "last_callback_query": state.state["last_callback_query"],
        "last_auth_id": state.mask_token(state.state["last_auth_id"]),
        "callback_port": config.CALLBACK_PORT,
        "verify_tls": config.VERIFY_TLS,
        "last_error": state.state["last_error"],
        "tunnel_engine": state.state["tunnel_engine"],
        "tunnel_engine_available": bool(vpn.find_tunnel_engine()[0]),
        "tunnel_pid": state.state["tunnel_pid"],
        "dev_mode": config.DEV_MODE,
        "automate_mode": config.AUTOMATE,
        "has_credentials": bool(config.SSO_USERNAME and config.SSO_PASSWORD),
        "bypass_enabled": bypass_manager.enabled,
        "bypass_apps": bypass_manager.apps,
        "bypass_domains": bypass_manager.domains,
        "bypass_active_routes": {
            ip: {"domain": meta.get("domain"), "gateway": meta.get("gateway")}
            for ip, meta in bypass_manager.active_routes.items()
        },
        "bypass_physical_gateway": bypass_manager.get_physical_gateway_info()[0]
    })

@app.route("/api/automate/toggle", methods=["POST"])
def toggle_automate():
    if config.DEV_MODE != "pointcoffee":
        return jsonify({"success": False, "message": "Fitur otomatisasi dinonaktifkan di environment ini."}), 403
        
    data = request.json or {}
    val = bool(data.get("automate", False))
    config.AUTOMATE = val
    config.update_env_variable("automate", val)
    
    msg = "Mode otomatis diaktifkan." if val else "Mode otomatis dinonaktifkan."
    state.add_log(msg, "info")
    
    if val and not state.state["vpn_connected"] and not state.state["sso_in_progress"]:
        vpn.trigger_auto_reconnect("Otomatisasi diaktifkan")
        
    return jsonify({"success": True, "message": msg})

@app.route("/api/connect/sso", methods=["POST"])
def connect_sso():
    if state.state["sso_in_progress"]:
        return jsonify({"success": False, "message": "SSO sedang berjalan."})

    if vpn.tunnel_process and vpn.tunnel_process.poll() is None:
        return jsonify({"success": True, "message": "Tunnel sudah aktif."})

    if state.state["vpn_config_loaded"] and vpn.get_svpn_cookie():
        try:
            vpn.start_openconnect_tunnel()
            return jsonify({"success": True, "message": "VPN tunnel dimulai."})
        except Exception as e:
            state.state["last_error"] = str(e)
            state.add_log(f"Gagal memulai VPN: {e}", "error")
            return jsonify({"success": False, "message": str(e)}), 400

    # Pastikan mode GP dinonaktifkan saat menggunakan mode Fortinet SSL
    gp_mod.set_gp_mode(False)

    state.state["sso_in_progress"] = True
    state.state["connect_requested"] = True
    state.state["sso_completed"] = False
    state.state["last_callback"] = None
    state.state["last_callback_query"] = {}
    state.state["last_auth_id"] = None
    vpn.reset_vpn_artifacts(keep_connect_request=True)

    if config.SSO_USERNAME and config.SSO_PASSWORD and config.AUTOMATE:
        state.add_log("Kredensial SSO ditemukan. Menjalankan headless login flow...", "info", user_visible=True)
        threading.Thread(target=vpn.perform_headless_sso, daemon=True).start()
        return jsonify({"success": True, "message": "Headless SSO login dimulai di latar belakang."})

    sso_url = config.build_saml_start_url()
    state.state["sso_url"] = sso_url

    state.add_log(f"Memulai SSO browser flow ke {sso_url}", "info", user_visible=True)
    state.add_log(f"Menunggu callback di {config.build_callback_url()}", "info", user_visible=True)

    try:
        tray.open_app_in_browser(url=sso_url)
        state.add_log("Browser default dibuka untuk login SSO.", "success", user_visible=True)
        return jsonify({"success": True, "message": "Browser dibuka untuk SSO login."})
    except Exception as e:
        state.state["sso_in_progress"] = False
        state.add_log(f"Gagal membuka browser: {e}", "error")
        return jsonify({"success": False, "message": f"Gagal membuka browser: {e}"})

@app.route("/api/connect/vpn", methods=["POST"])
def connect_vpn_alias():
    if gp_mod.is_gp_mode():
        return jsonify({"success": True, "message": "Mode GP aktif."})
    return connect_sso()

@app.route("/api/disconnect/vpn", methods=["POST"])
def disconnect_vpn():
    if config.AUTOMATE:
        config.AUTOMATE = False
        config.update_env_variable("automate", "false")
        state.add_log("Interaksi user terdeteksi (Disconnect). Mode otomatis dinonaktifkan.", "info")

    vpn.stop_tunnel_process()
    vpn.invalidate_gateway_session("Disconnected. Jalankan Connect untuk login ulang.")
    state.state["connect_requested"] = False
    state.state["sso_in_progress"] = False
    state.state["sso_completed"] = False
    state.state["last_callback"] = None
    state.state["last_callback_query"] = {}
    state.state["last_auth_id"] = None
    state.state["sso_url"] = None
    state.state["gp_token"] = None

    state.add_log("VPN disconnected dan state SSO dibersihkan.", "warning")
    return jsonify({
        "success": True,
        "message": "VPN disconnected."
    })

@app.route("/api/logout", methods=["POST"])
def logout():
    if config.AUTOMATE:
        config.AUTOMATE = False
        config.update_env_variable("automate", "false")
        state.add_log("Interaksi user terdeteksi (Logout). Mode otomatis dinonaktifkan.", "info")

    was_connected = vpn.tunnel_process and vpn.tunnel_process.poll() is None or state.state["vpn_connected"]
    vpn.stop_tunnel_process()
    vpn.clear_login_session("Logout. Sesi SSO/Gateway dibersihkan.")
    state.state["sso_in_progress"] = False
    state.state["connect_requested"] = False
    state.state["sso_completed"] = False
    state.state["last_callback"] = None
    state.state["last_callback_query"] = {}
    state.state["last_auth_id"] = None
    state.state["sso_url"] = None
    state.state["gp_token"] = None
    state.add_log("Logout selesai. Sesi SSO/Gateway dibersihkan.", "warning")
    return jsonify({
        "success": True,
        "message": "Logout selesai. VPN disconnected dan sesi login dibersihkan." if was_connected else "Logout selesai. Sesi login dibersihkan."
    })

@app.route("/api/gp/connect", methods=["POST"])
def gp_connect():
    """Endpoint koneksi GlobalProtect: terima portal, mulai SAML auth."""
    data = request.json or {}
    portal = data.get("portal", config.GP_HOST).strip()

    if state.state["sso_in_progress"]:
        return jsonify({"success": False, "message": "Proses login sedang berjalan."})

    if vpn.tunnel_process and vpn.tunnel_process.poll() is None:
        return jsonify({"success": True, "message": "VPN sudah terhubung."})

    # Set mode GP aktif
    gp_mod.set_gp_mode(True)

    # Coba resume dari sesi tersimpan dulu, fallback ke SSO jika gagal
    def _resume_or_sso():
        if not gp_mod.try_resume_gp_session():
            gp_mod.set_gp_mode(True)
            gp_mod.perform_gp_sso(portal)

    threading.Thread(target=_resume_or_sso, daemon=True).start()
    return jsonify({"success": True, "message": "Mencoba resume sesi GP atau SSO..."})


@app.route("/gp/saml-form")
def gp_saml_form():
    """Sajikan HTML form SAML GP ke browser agar user bisa auto-submit ke IdP."""
    html = state.state.get("_gp_saml_html", "")
    if not html:
        return "SAML form belum tersedia. Klik Connect terlebih dahulu.", 404
    # Inject auto-submit script agar form langsung di-POST ke IdP
    auto_submit = "<script>document.getElementById('myform').submit();</script>"
    return html.replace("</body>", f"{auto_submit}</body>")


@app.route("/gp/acs-callback", methods=["GET", "POST"])
def gp_acs_callback():
    """
    Endpoint callback setelah SAML ACS di vpn.bps.go.id/SAML20/SP/ACS.
    GlobalProtect mengirimkan cookie SESSID lewat redirect ke sini.
    """
    # Ambil cookie dari request atau parameter
    sessid = request.cookies.get("SESSID") or request.args.get("token") or request.form.get("token")
    if sessid:
        gp_mod.gp_session.cookies.set("SESSID", sessid)
        state.state["sso_in_progress"] = False
        state.state["sso_completed"] = True
        state.state["last_error"] = None
        add_log("Cookie GP (SESSID) diterima. Memulai tunnel...", "success", user_visible=True)
        threading.Thread(target=gp_mod.start_gp_tunnel, daemon=True).start()
        return "<html><body><script>window.close();</script><p>Login berhasil. Silakan tutup jendela ini.</p></body></html>"

    state.state["sso_in_progress"] = False
    add_log("ACS callback diterima tapi SESSID tidak ditemukan.", "warning", user_visible=True)
    return "Login selesai tapi sesi tidak terdeteksi.", 400


@app.route("/api/gp/callback", methods=["POST"])
def api_gp_callback():
    """Menerima callback login GlobalProtect dari gp_auth.py."""
    token = request.form.get("token", "").strip()
    username = request.form.get("username", "").strip() or config.SSO_USERNAME
    portal_cookie = request.form.get("portal_cookie", "").strip()
    prelogin_cookie = request.form.get("prelogin_cookie", "").strip()
    sessid = request.form.get("sessid", "").strip()

    active_token = portal_cookie or prelogin_cookie or sessid or token
    if not active_token:
        return jsonify({"success": False, "message": "Token tidak ditemukan."}), 400

    if active_token == state.state.get("gp_token"):
        state.add_log("Token GP ini sudah pernah dipakai sebelumnya. Silakan login SSO ulang di browser.", "warning", user_visible=False)
        return jsonify({"success": False, "message": "Token sudah pernah dipakai."}), 400

    if (vpn.tunnel_process and vpn.tunnel_process.poll() is None) or state.state.get("vpn_connected"):
        state.add_log("Callback GP diterima tetapi VPN tunnel sudah terhubung. Mengabaikan token duplikat.", "info", user_visible=False)
        return jsonify({"success": True, "message": "Tunnel sudah terhubung."})

    state.state["sso_in_progress"] = False
    state.state["sso_completed"] = True
    state.state["svpn_cookie_found"] = True
    state.state["vpn_config_loaded"] = True
    state.state["vpn_config_summary"] = {
        "assigned_addr": None,
        "dns_servers": ["10.10.11.11", "10.10.11.12"],
        "route_count": 6
    }

    state.state["gp_token"] = active_token
    state.state["gp_username"] = username
    state.add_log(f"Token GP diterima: {state.mask_token(active_token)}", "success", user_visible=True)

    usergroup = "gateway:prelogin-cookie"
    if portal_cookie:
        usergroup = "portal:portal-userauthcookie"

    state.add_log(f"Memulai tunnel OpenConnect GP (user={username}, group={usergroup})...", "info", user_visible=True)

    threading.Thread(
        target=gp_mod.start_gp_tunnel,
        kwargs={"token": active_token, "username": username, "usergroup": usergroup},
        daemon=True
    ).start()

    return jsonify({"success": True})


@app.route("/api/tunnel/stop", methods=["POST"])
def stop_tunnel():
    if config.AUTOMATE:
        config.AUTOMATE = False
        config.update_env_variable("automate", "false")
        state.add_log("Interaksi user terdeteksi (Stop Tunnel). Mode otomatis dinonaktifkan.", "info")
    vpn.stop_tunnel_process()
    vpn.invalidate_gateway_session("Tunnel dihentikan. Jalankan SSO ulang sebelum start lagi.")
    state.state["sso_completed"] = False
    return jsonify({
        "success": True,
        "message": "Tunnel process dihentikan. Silakan SSO ulang untuk start lagi."
    })

@app.route("/api/logs/clear", methods=["POST"])
def clear_logs():
    state.state["logs"] = []
    log_file = os.path.join(state.BASE_DIR, "vpn_helper.log")
    with state.log_file_lock:
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                f.write("")
        except Exception:
            pass
    state.add_log("Log dibersihkan.", "info")
    return jsonify({"success": True})

# ─── Bypass / Split-Tunnel API ────────────────────────────────────────────────

@app.route("/api/bypass/toggle", methods=["POST"])
def bypass_toggle():
    data = request.json or {}
    enabled = bool(data.get("enabled", not bypass_manager.enabled))
    bypass_manager.set_enabled(enabled)
    msg = "Bypass aktif." if enabled else "Bypass dinonaktifkan. Semua route bypass dihapus."
    state.add_log(msg, "info")
    return jsonify({"success": True, "enabled": bypass_manager.enabled, "message": msg})

@app.route("/api/bypass/apps", methods=["POST"])
def bypass_add_app():
    data = request.json or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"success": False, "message": "Path tidak boleh kosong."}), 400
    added = bypass_manager.add_app(path)
    msg = f"Aplikasi ditambahkan: {path}" if added else "Aplikasi sudah ada di daftar."
    state.add_log(msg, "info")
    return jsonify({"success": True, "apps": bypass_manager.apps, "message": msg})

@app.route("/api/bypass/apps/delete", methods=["POST"])
def bypass_remove_app():
    data = request.json or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"success": False, "message": "Path tidak boleh kosong."}), 400
    removed = bypass_manager.remove_app(path)
    msg = f"Aplikasi dihapus: {path}" if removed else "Aplikasi tidak ditemukan di daftar."
    state.add_log(msg, "info")
    return jsonify({"success": removed, "apps": bypass_manager.apps, "message": msg})

@app.route("/api/bypass/domains", methods=["POST"])
def bypass_add_domain():
    data = request.json or {}
    domain = data.get("domain", "").strip()
    if not domain:
        return jsonify({"success": False, "message": "Domain tidak boleh kosong."}), 400
    added = bypass_manager.add_domain(domain)
    msg = f"Domain ditambahkan: {domain}" if added else "Domain sudah ada di daftar."
    state.add_log(msg, "info")
    return jsonify({"success": True, "domains": bypass_manager.domains, "message": msg})

@app.route("/api/bypass/domains/delete", methods=["POST"])
def bypass_remove_domain():
    data = request.json or {}
    domain = data.get("domain", "").strip()
    if not domain:
        return jsonify({"success": False, "message": "Domain tidak boleh kosong."}), 400
    removed = bypass_manager.remove_domain(domain)
    msg = f"Domain dihapus: {domain}" if removed else "Domain tidak ditemukan."
    state.add_log(msg, "info")
    return jsonify({"success": removed, "domains": bypass_manager.domains, "message": msg})

@app.route("/api/bypass/refresh", methods=["POST"])
def bypass_refresh():
    if not state.state.get("vpn_connected", False):
        return jsonify({"success": False, "message": "VPN belum terhubung. Refresh bypass hanya bisa saat VPN aktif."}), 400
    threading.Thread(target=bypass_manager.apply_bypass_routes, daemon=True).start()
    return jsonify({"success": True, "message": "Refresh route bypass dimulai di background."})

@callback_app.route("/", methods=["GET"])
def callback_root():
    q = request.args.to_dict(flat=True)
    auth_id = q.get("id")
    if not auth_id:
        state.add_log(f"Callback SSO diterima tanpa ID (ping) di port {config.CALLBACK_PORT}", "info", user_visible=False)
        return "Ping diterima"

    state.state["last_callback"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state.state["last_auth_id"] = auth_id
    state.state["last_callback_query"] = {
        key: state.mask_token(value) if key.lower() in {"id", "token", "code"} else value
        for key, value in q.items()
    }
    state.state["sso_in_progress"] = False
    state.state["sso_completed"] = True
    if not state.state["vpn_connected"]:
        vpn.reset_vpn_artifacts(keep_connect_request=True)
        state.state["connect_requested"] = True

    state.add_log(f"Callback SSO diterima di port {config.CALLBACK_PORT}", "success", user_visible=False)
    state.add_log(f"Query callback keys={list(q.keys())}", "info", user_visible=False)

    if state.state["last_auth_id"]:
        state.add_log(f"Authentication ID diterima: {state.mask_token(state.state['last_auth_id'])}", "success", user_visible=False)
        state.add_log("SSO browser flow selesai.", "success", user_visible=True)
        try:
            vpn.exchange_auth_id_for_cookie(state.state["last_auth_id"])
            state.state["vpn_config_loaded"] = True
            if not state.state["vpn_connected"]:
                state.add_log("Connect flow lanjut otomatis ke tunnel.", "info", user_visible=False)
                vpn.start_openconnect_tunnel()
            else:
                state.add_log("VPN sudah terhubung. Sesi baru disimpan untuk penyambungan berikutnya.", "info", user_visible=True)
            
            # Fetch config in background
            def fetch_config_bg():
                try:
                    vpn.fetch_vpn_config()
                except Exception:
                    pass
            threading.Thread(target=fetch_config_bg, daemon=True).start()
        except requests.exceptions.SSLError as e:
            state.state["connect_requested"] = False
            clean_err = state.format_user_error(e)
            state.state["last_error"] = clean_err
            state.add_log(f"TLS certificate gateway ditolak: {clean_err}", "error", user_visible=True)
            state.add_log("Jika gateway memakai sertifikat internal, set verify_tls=false di .env untuk testing.", "warning", user_visible=True)
        except Exception as e:
            state.state["connect_requested"] = False
            clean_err = state.format_user_error(e)
            state.state["last_error"] = clean_err
            state.add_log(f"Gagal menyiapkan sesi SSL-VPN: {clean_err}", "error", user_visible=True)

    return f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>SSO Callback Diterima</title>
      <style>
        body {{
          font-family: Arial, sans-serif;
          background: #111;
          color: #eee;
          display: flex;
          align-items: center;
          justify-content: center;
          min-height: 100vh;
          margin: 0;
        }}
        .box {{
          max-width: 760px;
          padding: 24px;
          border: 1px solid #333;
          border-radius: 12px;
          background: #1a1a1a;
        }}
        code {{
          display: block;
          white-space: pre-wrap;
          word-break: break-word;
          margin-top: 12px;
          color: #9fe3ff;
        }}
      </style>
    </head>
    <body>
      <div class="box">
        <h2>SSO callback diterima</h2>
        <p>Authentication ID sudah diterima oleh listener lokal.</p>
        <code>{state.state["last_callback_query"]}</code>
        <p>Silakan kembali ke app utama di {config.build_app_url()}</p>
      </div>
    </body>
    </html>
    """

def run_callback_listener():
    state.add_log(f"Callback listener aktif di {config.build_callback_url()}", "info")
    callback_app.run(host=config.CALLBACK_HOST, port=config.CALLBACK_PORT, debug=config.CALLBACK_DEBUG, use_reloader=config.USE_RELOADER)

def run_app_server():
    app.run(host=config.APP_HOST, port=config.APP_PORT, debug=config.APP_DEBUG, use_reloader=config.USE_RELOADER)

def run_console_mode():
    threading.Thread(target=run_callback_listener, daemon=True).start()
    if config.SSO_USERNAME and config.SSO_PASSWORD:
        threading.Thread(target=vpn.auto_connect_on_start, daemon=True).start()
    else:
        state.add_log(f"Buka browser ke {config.build_app_url()}", "info")
        tray.open_app_in_browser()
    run_app_server()

atexit.register(vpn.stop_tunnel_process)
updater.register_updater_routes(app, state.BASE_DIR, state.add_log)

if __name__ == "__main__":
    state.load_persisted_logs()

    state.add_log(f"Host VPN: {config.VPN_HOST}:{config.VPN_PORT}", "info")
    state.add_log(f"Port callback lokal SAML: {config.CALLBACK_PORT}", "info")
    state.add_log("VPN Helper dimulai.", "info")

    threading.Thread(target=vpn.monitor_tunnel_loop, daemon=True).start()

    if not config.ENABLE_TRAY or not tray.run_tray_mode(
        on_start_callback_listener=run_callback_listener,
        on_start_app_server=run_app_server,
        on_start_auto_connect=vpn.auto_connect_on_start,
        on_stop_tunnel=vpn.stop_tunnel_process
    ):
        run_console_mode()
