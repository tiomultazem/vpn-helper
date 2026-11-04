"""
Modul jendela login SSO mandiri untuk GlobalProtect BPS menggunakan PyQt5 WebEngine.
Mencegat skema globalprotectcallback:// langsung di memori tanpa menyentuh registri Windows.
"""
import sys
import os
import re
import json
import base64
import urllib.request
import urllib.parse

from PyQt5.QtCore import QUrl, QTimer
from PyQt5.QtWidgets import QApplication
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage, QWebEngineProfile
from PyQt5.QtNetwork import QNetworkCookie


_SUCCESS_HTML = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Login Berhasil</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    background: #0f172a;
    color: #e2e8f0;
    font-family: 'Segoe UI', Arial, sans-serif;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
  }
  .card {
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 16px;
    padding: 48px 40px;
    text-align: center;
    width: 380px;
    box-shadow: 0 24px 64px rgba(0,0,0,.6);
    animation: fadeIn .35s ease;
  }
  @keyframes fadeIn { from { opacity:0; transform:translateY(16px); } to { opacity:1; transform:none; } }
  .icon {
    width: 72px; height: 72px;
    background: linear-gradient(135deg,#10b981,#059669);
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    margin: 0 auto 24px;
    font-size: 36px; color: white;
    animation: pop .4s cubic-bezier(.34,1.56,.64,1);
  }
  @keyframes pop { from { transform:scale(0); opacity:0; } to { transform:scale(1); opacity:1; } }
  h2 { font-size: 22px; font-weight: 700; color: #f8fafc; margin-bottom: 8px; }
  .sub { color: #94a3b8; font-size: 13px; line-height: 1.7; margin-bottom: 28px; }
  .countdown {
    background: #0f172a; border-radius: 8px;
    padding: 10px 16px; font-size: 13px;
    color: #64748b; margin-bottom: 20px;
    border: 1px solid #1e293b;
  }
  .countdown b { color: #10b981; }
  .btn {
    display: inline-block;
    border: 1px solid #475569; color: #94a3b8;
    border-radius: 8px; padding: 9px 22px;
    font-size: 13px; cursor: pointer; text-decoration: none;
    transition: border-color .2s, color .2s;
  }
  .btn:hover { border-color: #94a3b8; color: #e2e8f0; }
</style>
</head>
<body>
<div class="card">
  <div class="icon">&#10003;</div>
  <h2>Login Berhasil!</h2>
  <p class="sub">Tunnel VPN GlobalProtect sedang dibentuk.<br>Silakan kembali ke aplikasi utama.</p>
  <div class="countdown">Akan tertutup otomatis dalam <b id="s">3</b> detik.</div>
  <a class="btn" href="vpnhelper://close">&#10005; &nbsp;Tutup Sekarang</a>
</div>
<script>
  var n = 3, el = document.getElementById('s');
  var timer = setInterval(function() {
    n--;
    if (el) el.textContent = Math.max(0, n);
    if (n <= 0) {
      clearInterval(timer);
      window.location.href = 'vpnhelper://close';
    }
  }, 1000);
</script>
</body>
</html>"""


def parse_callback_payload(raw_str: str) -> dict:
    if not raw_str:
        return {}

    b64_part = raw_str
    if "globalprotectcallback:" in b64_part:
        b64_part = b64_part.split("globalprotectcallback:", 1)[1]
    b64_part = b64_part.lstrip("/").split('"')[0].split("'")[0].split(">")[0].split("&")[0].strip()

    try:
        decoded = base64.b64decode(b64_part).decode("utf-8", errors="ignore")
    except Exception:
        decoded = raw_str

    user_match = re.search(r"<saml-username>([^<]+)</saml-username>", decoded)
    prelogin_match = re.search(r"<prelogin-cookie>([^<]+)</prelogin-cookie>", decoded)
    portal_match = re.search(r"<portal-userauthcookie>([^<]+)</portal-userauthcookie>", decoded)
    status_match = re.search(r"<saml-auth-status>([^<]+)</saml-auth-status>", decoded)

    token = ""
    if portal_match:
        token = portal_match.group(1)
    elif prelogin_match:
        token = prelogin_match.group(1)

    return {
        "token": token,
        "username": user_match.group(1) if user_match else "",
        "prelogin_cookie": prelogin_match.group(1) if prelogin_match else "",
        "portal_cookie": portal_match.group(1) if portal_match else "",
        "status": status_match.group(1) if status_match else "",
        "decoded_snippet": decoded[:300]
    }


class GPCustomWebEnginePage(QWebEnginePage):
    def __init__(self, parent=None, on_callback=None, on_close=None):
        super().__init__(parent)
        self.on_callback = on_callback
        self.on_close = on_close

    def certificateError(self, certificateError):
        certificateError.ignoreCertificateError()
        return True

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        url_str = url.toString()
        if "globalprotectcallback:" in url_str:
            if self.on_callback:
                self.on_callback(url_str)
            return False
        if url_str.startswith("vpnhelper://close"):
            if self.on_close:
                self.on_close()
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


class GPAuthWindow(QWebEngineView):
    def __init__(self, initial_url):
        super().__init__()
        self.setWindowTitle("Login SSO BPS — GlobalProtect")
        self.resize(560, 760)

        self.captured_cookies = {}
        self.done = False
        self._sso_username = self._get_saved_username()

        self.custom_page = GPCustomWebEnginePage(
            self,
            on_callback=self.handle_callback_uri,
            on_close=self._close_window
        )
        self.setPage(self.custom_page)

        profile = self.custom_page.profile()
        profile.setUserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

        self.store = profile.cookieStore()
        self.store.cookieAdded.connect(self.on_cookie_added)

        self.urlChanged.connect(self.on_url_changed)
        self.loadFinished.connect(self.on_load_finished)

        self.load(QUrl(initial_url))

    def _get_saved_username(self) -> str:
        try:
            from src import config as cfg
            if cfg.SSO_USERNAME:
                return cfg.SSO_USERNAME
        except Exception:
            pass
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            session_file = os.path.join(base_dir, ".gp_session.json")
            if os.path.exists(session_file):
                import time as _t
                with open(session_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if _t.time() < data.get("expires_at", 0):
                    return data.get("username", "")
        except Exception:
            pass
        return ""

    def _close_window(self):
        self.close()
        QTimer.singleShot(50, QApplication.instance().quit)

    def on_cookie_added(self, cookie: QNetworkCookie):
        name = cookie.name().data().decode("utf-8", errors="ignore")
        value = cookie.value().data().decode("utf-8", errors="ignore")
        domain = cookie.domain()

        if "bps.go.id" in domain or name in ("SESSID", "portal-userauthcookie", "prelogin-cookie"):
            self.captured_cookies[name] = value

    def handle_callback_uri(self, uri: str):
        if self.done:
            return

        parsed = parse_callback_payload(uri)
        token = parsed.get("portal_cookie") or parsed.get("prelogin_cookie") or parsed.get("token")
        username = parsed.get("username") or ""

        if token:
            self.submit_payload(token, username, parsed, f"Callback intercepted: {uri}")

    def on_url_changed(self, qurl: QUrl):
        url_str = qurl.toString()
        if "globalprotectcallback:" in url_str:
            self.handle_callback_uri(url_str)
        elif "/SAML20/SP/ACS" in url_str and not self.done:
            QTimer.singleShot(500, self.extract_from_html)

    def on_load_finished(self, ok):
        url_str = self.url().toString()
        if "/SAML20/SP/ACS" in url_str and not self.done:
            QTimer.singleShot(300, self.extract_from_html)
        if self._sso_username and ok and not self.done:
            QTimer.singleShot(600, self._autofill_username)

    def _autofill_username(self):
        if self.done:
            return
        username = self._sso_username.replace("'", "\\'").replace("\\", "\\\\")
        js = f"""
(function() {{
    var selectors = [
        'input[name="username"]',
        'input[id="username"]',
        'input[type="email"]',
        'input[autocomplete="username"]',
        'input[name="user"]',
        'input[id="user"]'
    ];
    for (var i = 0; i < selectors.length; i++) {{
        var el = document.querySelector(selectors[i]);
        if (el && !el.value) {{
            var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            if (setter) {{
                setter.call(el, '{username}');
            }} else {{
                el.value = '{username}';
            }}
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            el.focus();
            break;
        }}
    }}
}})();
"""
        self.page().runJavaScript(js)

    def extract_from_html(self):
        if self.done:
            return
        self.page().toHtml(self.on_html_received)

    def on_html_received(self, html):
        if self.done:
            return

        cb_match = re.search(r"globalprotectcallback:([A-Za-z0-9+/=]+)", html)
        if cb_match:
            parsed = parse_callback_payload(cb_match.group(0))
            token = parsed.get("portal_cookie") or parsed.get("prelogin_cookie")
            if token:
                self.submit_payload(token, parsed.get("username", ""), parsed, "Found callback in HTML source")
                return

        user_match = re.search(r"<saml-username>([^<]+)</saml-username>", html)
        prelogin_match = re.search(r"<prelogin-cookie>([^<]+)</prelogin-cookie>", html)
        portal_match = re.search(r"<portal-userauthcookie>([^<]+)</portal-userauthcookie>", html)

        username = user_match.group(1) if user_match else ""
        prelogin_cookie = prelogin_match.group(1) if prelogin_match else self.captured_cookies.get("prelogin-cookie", "")
        portal_cookie = portal_match.group(1) if portal_match else self.captured_cookies.get("portal-userauthcookie", "")
        sessid = self.captured_cookies.get("SESSID", "")

        token = portal_cookie or prelogin_cookie or sessid
        if token:
            payload_dict = {
                "token": token,
                "username": username,
                "prelogin_cookie": prelogin_cookie,
                "portal_cookie": portal_cookie,
                "sessid": sessid
            }
            self.submit_payload(token, username, payload_dict, "Extracted from HTML tags & cookies")

    def submit_payload(self, token: str, username: str, extra: dict, source_desc: str):
        if self.done:
            return
        self.done = True

        prelogin_cookie = extra.get("prelogin_cookie", self.captured_cookies.get("prelogin-cookie", ""))
        portal_cookie = extra.get("portal_cookie", self.captured_cookies.get("portal-userauthcookie", ""))
        sessid = self.captured_cookies.get("SESSID", "")

        payload = {
            "token": token,
            "username": username,
            "prelogin_cookie": prelogin_cookie,
            "portal_cookie": portal_cookie,
            "sessid": sessid,
            "all_cookies": json.dumps(self.captured_cookies),
            "source": source_desc
        }

        try:
            from src import config
            callback_url = f"http://{config.APP_HOST}:{config.APP_PORT}/api/gp/callback"
        except Exception:
            callback_url = "http://127.0.0.1:8765/api/gp/callback"

        try:
            data = urllib.parse.urlencode(payload).encode("utf-8")
            req = urllib.request.Request(callback_url, data=data, method="POST")
            urllib.request.urlopen(req, timeout=3)
        except Exception as e:
            print(f"[GP_AUTH] Gagal mengirim callback: {e}")

        # Render halaman sukses HTML
        self.setHtml(_SUCCESS_HTML)

        # Jadwalkan penutupan window secara pasti dalam 3 detik di Python
        QTimer.singleShot(3000, self._close_window)


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/gp/saml-form"
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("QWebEngineView { background-color: #0f172a; }")
    win = GPAuthWindow(url)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
