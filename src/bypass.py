import os
import re
import json
import socket
import subprocess
import threading
import time
import urllib.request
from src.state import add_log, state
from src.config import update_env_variable, BYPASS_ENABLED, BYPASS_APPS, BYPASS_DOMAINS, TUNNEL_INTERFACE

# ──────────────────────────────────────────────────────────────────────────────
# Pemetaan wildcard ke URL daftar CIDR publik milik provider.
# Catatan: tidak ada modifikasi DNS adapter sama sekali.
# ──────────────────────────────────────────────────────────────────────────────
WILDCARD_CIDR_SOURCES = {
    "*.google.com": [
        "https://www.gstatic.com/ipranges/goog.json",    # Google infra umum
        "https://www.gstatic.com/ipranges/cloud.json",   # Google Cloud
    ],
    "*.googleapis.com": [
        "https://www.gstatic.com/ipranges/goog.json",
    ],
    "*.googleusercontent.com": [
        "https://www.gstatic.com/ipranges/goog.json",
    ],
    "*.youtube.com": [
        "https://www.gstatic.com/ipranges/goog.json",
    ],
    "*.cloudflare.com": [
        "https://www.cloudflare.com/ips-v4",
    ],
    "*.github.com": [
        "https://api.github.com/meta",
    ],
}

# Cache CIDR per pola wildcard {pattern -> (timestamp, [cidr_str, ...])}
_cidr_cache: dict = {}
_cidr_cache_lock = threading.Lock()
CIDR_CACHE_TTL = 3600  # 1 jam


class BypassManager:

    def __init__(self):
        self.enabled = BYPASS_ENABLED
        self.apps = list(BYPASS_APPS)
        self.domains = list(BYPASS_DOMAINS)
        self.active_routes = {}   # ip/cidr -> {"gateway": gw, "ifindex": idx, "domain": dom}
        self._last_resolved_ips = {}
        self._lock = threading.Lock()
        self._periodic_thread = None
        self._stop_event = threading.Event()

    # ── Utilitas path ──────────────────────────────────────────────────────────

    def normalize_path(self, path: str) -> str:
        return os.path.normpath(path.strip()).lower()

    # ── Enabled ────────────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool):
        with self._lock:
            self.enabled = enabled
            update_env_variable("bypass_enabled", enabled)
        if not enabled:
            self.cleanup_routes()
        else:
            if state.get("vpn_connected", False):
                threading.Thread(target=self.apply_bypass_routes, daemon=True).start()

    # ── Aplikasi ───────────────────────────────────────────────────────────────

    def add_app(self, app_path: str) -> bool:
        normalized = self.normalize_path(app_path)
        with self._lock:
            for a in self.apps:
                if self.normalize_path(a) == normalized:
                    return False
            self.apps.append(app_path.strip())
            self._save_apps()
            return True

    def remove_app(self, app_path: str) -> bool:
        normalized = self.normalize_path(app_path)
        with self._lock:
            initial_len = len(self.apps)
            self.apps = [a for a in self.apps if self.normalize_path(a) != normalized]
            if len(self.apps) < initial_len:
                self._save_apps()
                return True
            return False

    def _save_apps(self):
        update_env_variable("bypass_apps", ";".join(self.apps))

    # ── Domain ────────────────────────────────────────────────────────────────

    def add_domain(self, domain: str) -> bool:
        dom = domain.strip().lower()
        if not dom:
            return False
        with self._lock:
            if dom in [d.lower() for d in self.domains]:
                return False
            self.domains.append(dom)
            update_env_variable("bypass_domains", ";".join(self.domains))
        if self.enabled and state.get("vpn_connected", False):
            threading.Thread(target=self.apply_bypass_routes, daemon=True).start()
        return True

    def remove_domain(self, domain: str) -> bool:
        dom = domain.strip().lower()
        with self._lock:
            initial_len = len(self.domains)
            self.domains = [d for d in self.domains if d.lower() != dom]
            if len(self.domains) < initial_len:
                update_env_variable("bypass_domains", ";".join(self.domains))
                changed = True
            else:
                changed = False
        if changed and self.enabled and state.get("vpn_connected", False):
            threading.Thread(target=self.apply_bypass_routes, daemon=True).start()
        return changed

    # ── Wildcard CIDR ─────────────────────────────────────────────────────────

    def is_wildcard(self, domain: str) -> bool:
        return domain.startswith("*.")

    def _fetch_cidr_list(self, pattern: str) -> list:
        """
        Unduh dan kembalikan daftar CIDR IPv4 untuk pola wildcard.
        Menggunakan cache 1 jam. Tidak menyentuh DNS adapter sama sekali.
        """
        now = time.time()
        with _cidr_cache_lock:
            cached = _cidr_cache.get(pattern)
            if cached:
                ts, cidrs = cached
                if now - ts < CIDR_CACHE_TTL:
                    return cidrs

        urls = WILDCARD_CIDR_SOURCES.get(pattern, [])
        all_cidrs = []

        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    raw = resp.read().decode("utf-8")

                # Format JSON Google: {"prefixes": [{"ipv4Prefix": "..."}]}
                if url.endswith(".json") and "gstatic" in url:
                    data = json.loads(raw)
                    for entry in data.get("prefixes", []):
                        cidr = entry.get("ipv4Prefix")
                        if cidr:
                            all_cidrs.append(cidr)

                # Format teks Cloudflare: satu IP/CIDR per baris
                elif "cloudflare" in url and "ips-v4" in url:
                    for line in raw.splitlines():
                        line = line.strip()
                        if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(/\d+)?$", line):
                            all_cidrs.append(line)

                # Format JSON GitHub meta: {"web": ["cidr", ...]}
                elif "api.github.com" in url:
                    data = json.loads(raw)
                    for key in ("web", "api", "git", "packages", "pages", "actions"):
                        for cidr in data.get(key, []):
                            if ":" not in cidr:  # IPv4 only
                                all_cidrs.append(cidr)

            except Exception as e:
                add_log(f"Gagal mengunduh CIDR untuk {pattern} dari {url}: {e}", "warning", user_visible=False)

        # Deduplikasi
        all_cidrs = list(dict.fromkeys(all_cidrs))

        if all_cidrs:
            with _cidr_cache_lock:
                _cidr_cache[pattern] = (now, all_cidrs)
            add_log(f"Bypass CIDR {pattern}: {len(all_cidrs)} blok IP dimuat.", "info", user_visible=True)
        else:
            add_log(f"Bypass CIDR {pattern}: tidak ada blok IP yang berhasil dimuat.", "warning", user_visible=True)

        return all_cidrs

    def _cidr_to_mask(self, cidr: str):
        """Konversi CIDR ke (network_address, subnet_mask) string."""
        try:
            net, prefix = cidr.split("/")
            prefix = int(prefix)
            mask_int = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
            mask = ".".join(str((mask_int >> (8 * i)) & 0xFF) for i in [3, 2, 1, 0])
            return net, mask
        except Exception:
            return None, None

    # ── IP Utilities ──────────────────────────────────────────────────────────

    def is_private_ip(self, ip: str) -> bool:
        try:
            parts = [int(p) for p in ip.split(".")]
            if len(parts) != 4:
                return True
            if parts[0] == 10:
                return True
            if parts[0] == 172 and 16 <= parts[1] <= 31:
                return True
            if parts[0] == 192 and parts[1] == 168:
                return True
            if parts[0] == 127:
                return True
            return False
        except Exception:
            return True

    # ── Gateway Detection ─────────────────────────────────────────────────────

    def get_physical_gateway_info(self):
        if os.name != "nt":
            return None, None, None

        try:
            cmd = (
                "powershell -NoProfile -Command "
                "\"Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
                "Select-Object NextHop, InterfaceIndex, InterfaceAlias, RouteMetric | ConvertTo-Json\""
            )
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, startupinfo=si)
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                if isinstance(data, dict):
                    data = [data]
                vpn_alias = (TUNNEL_INTERFACE or "VPN Helper").lower()
                for route in data:
                    alias = str(route.get("InterfaceAlias", "")).lower()
                    nexthop = str(route.get("NextHop", "")).strip()
                    if vpn_alias in alias or "openconnect" in alias:
                        continue
                    if nexthop and nexthop != "0.0.0.0" and not nexthop.startswith("127."):
                        return nexthop, route.get("InterfaceIndex"), route.get("InterfaceAlias")
        except Exception as e:
            add_log(f"Error mendeteksi gateway fisik via PowerShell: {e}", "warning", user_visible=False)

        # Fallback via route print
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            res = subprocess.run("route print 0.0.0.0", shell=True, capture_output=True, text=True, startupinfo=si)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                        gw = parts[2]
                        if gw != "0.0.0.0" and gw != "On-link":
                            return gw, None, "Physical Interface"
        except Exception as e:
            add_log(f"Error mendeteksi gateway fisik via route print: {e}", "warning", user_visible=False)

        return None, None, None

    # ── DNS Resolution (domain biasa saja) ────────────────────────────────────

    def resolve_domain_public(self, domain: str) -> list:
        ips = []
        if os.name == "nt":
            for dns_server in ["1.1.1.1", "8.8.8.8"]:
                try:
                    cmd = f"powershell -NoProfile -Command \"Resolve-DnsName -Name '{domain}' -Server '{dns_server}' -Type A -ErrorAction SilentlyContinue | Select-Object -ExpandProperty IPAddress\""
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, startupinfo=si)
                    if res.returncode == 0 and res.stdout.strip():
                        for line in res.stdout.splitlines():
                            ip = line.strip()
                            if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                                if not self.is_private_ip(ip) and ip not in ips:
                                    ips.append(ip)
                    if ips:
                        break
                except Exception:
                    pass

        # Fallback nslookup
        if not ips:
            try:
                cmd = f"nslookup {domain} 1.1.1.1"
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True, startupinfo=si)
                if res.returncode == 0:
                    lines = res.stdout.splitlines()
                    in_answers = False
                    for line in lines:
                        if "Non-authoritative answer" in line or "Name:" in line:
                            in_answers = True
                        if in_answers and "Address" in line and ":" in line:
                            parts = line.split(":", 1)
                            ip = parts[1].strip()
                            if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                                if not self.is_private_ip(ip) and ip not in ips:
                                    ips.append(ip)
            except Exception:
                pass

        return ips

    # ── Hosts file (dipertahankan untuk domain biasa) ─────────────────────────

    def _update_hosts_file(self, domain_ip_map: dict):
        if os.name != "nt":
            return
        hosts_path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), r"System32\drivers\etc\hosts")
        try:
            lines = []
            if os.path.exists(hosts_path):
                with open(hosts_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
            new_lines = [l for l in lines if "# VPN Helper Bypass" not in l]
            for ip, dom in domain_ip_map.items():
                new_lines.append(f"{ip}\t{dom}\t# VPN Helper Bypass\n")
            with open(hosts_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            self._flush_dns()
        except Exception as e:
            add_log(f"Gagal memperbarui file hosts untuk bypass DNS: {e}", "warning", user_visible=True)

    def _cleanup_hosts_file(self):
        if os.name != "nt":
            return
        hosts_path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), r"System32\drivers\etc\hosts")
        try:
            if not os.path.exists(hosts_path):
                return
            with open(hosts_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            new_lines = [l for l in lines if "# VPN Helper Bypass" not in l]
            if len(new_lines) != len(lines):
                with open(hosts_path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                self._flush_dns()
        except Exception as e:
            add_log(f"Gagal membersihkan file hosts: {e}", "warning", user_visible=False)

    def _flush_dns(self):
        if os.name != "nt":
            return
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            subprocess.run("ipconfig /flushdns", shell=True, capture_output=True, text=True, startupinfo=si)
        except Exception:
            pass

    # ── Apply Routes ──────────────────────────────────────────────────────────

    def apply_bypass_routes(self):
        with self._lock:
            if not self.enabled:
                self._cleanup_routes_unlocked()
                return

            gw, ifidx, alias = self.get_physical_gateway_info()
            if not gw:
                add_log("Bypass warning: Gateway/interface fisik tidak ditemukan. Rute bypass tidak dipasang.", "warning", user_visible=True)
                return

            resolved_target = {}  # key (ip_or_cidr) -> domain_label

            for dom in self.domains:
                if self.is_wildcard(dom):
                    # ── Wildcard: gunakan CIDR publik provider ──
                    cidrs = self._fetch_cidr_list(dom)
                    if cidrs:
                        for cidr in cidrs:
                            resolved_target[cidr] = dom
                    else:
                        add_log(f"Bypass wildcard {dom}: tidak ada CIDR tersedia.", "warning", user_visible=True)
                else:
                    # ── Domain biasa: resolusi DNS ke IP spesifik ──
                    public_ips = self.resolve_domain_public(dom)
                    if public_ips:
                        ips_sorted = sorted(public_ips)
                        if self._last_resolved_ips.get(dom) != ips_sorted:
                            self._last_resolved_ips[dom] = ips_sorted
                            add_log(f"Bypass DNS {dom} -> {', '.join(public_ips)} (via public DNS)", "info", user_visible=True)
                        else:
                            add_log(f"Bypass DNS {dom} -> {', '.join(public_ips)} (via public DNS)", "info", user_visible=False)
                        for ip in public_ips:
                            resolved_target[ip] = dom
                    else:
                        if self._last_resolved_ips.get(dom) != "FAILED":
                            self._last_resolved_ips[dom] = "FAILED"
                            add_log(f"Bypass DNS: Gagal resolve IP publik untuk {dom}", "warning", user_visible=True)
                        else:
                            add_log(f"Bypass DNS: Gagal resolve IP publik untuk {dom}", "warning", user_visible=False)

            # Hapus rute yang tidak lagi diperlukan
            for key in list(self.active_routes.keys()):
                if key not in resolved_target:
                    self._delete_route(key)

            # Tambahkan rute baru
            for key, dom in resolved_target.items():
                if key not in self.active_routes:
                    if "/" in key:
                        # CIDR route
                        net, mask = self._cidr_to_mask(key)
                        if net and mask and self._add_cidr_route(net, mask, gw, ifidx):
                            self.active_routes[key] = {"gateway": gw, "ifindex": ifidx, "domain": dom}
                            add_log(f"Bypass CIDR route dipasang: {key} via {gw} ({alias or 'Fisik'})", "info", user_visible=False)
                    else:
                        # IP/32 route
                        if self._add_route(key, gw, ifidx):
                            self.active_routes[key] = {"gateway": gw, "ifindex": ifidx, "domain": dom}
                            add_log(f"Bypass route dipasang: {key}/32 via {gw} ({alias or 'Fisik'})", "info", user_visible=True)

            # Sync hosts file untuk domain biasa saja
            normal_ip_map = {
                k: v for k, v in resolved_target.items()
                if "/" not in k
            }
            self._update_hosts_file(normal_ip_map)

    # ── Route Management ──────────────────────────────────────────────────────

    def _add_route(self, ip: str, gateway: str, ifindex=None) -> bool:
        if os.name != "nt":
            return False
        try:
            if ifindex:
                cmd = f"route add {ip} mask 255.255.255.255 {gateway} metric 5 if {ifindex}"
            else:
                cmd = f"route add {ip} mask 255.255.255.255 {gateway} metric 5"
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, startupinfo=si)
            return res.returncode == 0
        except Exception as e:
            add_log(f"Gagal memasang route bypass {ip}: {e}", "error", user_visible=False)
            return False

    def _add_cidr_route(self, network: str, mask: str, gateway: str, ifindex=None) -> bool:
        if os.name != "nt":
            return False
        try:
            if ifindex:
                cmd = f"route add {network} mask {mask} {gateway} metric 5 if {ifindex}"
            else:
                cmd = f"route add {network} mask {mask} {gateway} metric 5"
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, startupinfo=si)
            return res.returncode == 0
        except Exception as e:
            add_log(f"Gagal memasang CIDR route bypass {network}/{mask}: {e}", "error", user_visible=False)
            return False

    def _delete_route(self, key: str):
        if os.name != "nt":
            return
        try:
            if "/" in key:
                net, mask = self._cidr_to_mask(key)
                if net and mask:
                    cmd = f"route delete {net} mask {mask}"
                else:
                    return
            else:
                cmd = f"route delete {key}"
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            subprocess.run(cmd, shell=True, capture_output=True, text=True, startupinfo=si)
            self.active_routes.pop(key, None)
        except Exception as e:
            add_log(f"Gagal menghapus route bypass {key}: {e}", "warning", user_visible=False)

    def _cleanup_routes_unlocked(self):
        for key in list(self.active_routes.keys()):
            self._delete_route(key)
        self.active_routes.clear()
        self._last_resolved_ips.clear()
        self._cleanup_hosts_file()

    def cleanup_routes(self):
        with self._lock:
            self._cleanup_routes_unlocked()
            add_log("Semua route bypass berhasil dibersihkan.", "info", user_visible=False)

    # ── Periodic Refresh ──────────────────────────────────────────────────────

    def start_periodic_refresh(self):
        self.stop_periodic_refresh()
        self._stop_event.clear()

        def refresh_worker():
            add_log("Bypass manager: Memulai worker refresh route bypass...", "info", user_visible=False)
            self.apply_bypass_routes()
            while not self._stop_event.is_set():
                if self._stop_event.wait(timeout=600):
                    break
                if state.get("vpn_connected", False) and self.enabled:
                    self.apply_bypass_routes()

        self._periodic_thread = threading.Thread(target=refresh_worker, daemon=True)
        self._periodic_thread.start()

    def stop_periodic_refresh(self):
        self._stop_event.set()
        if self._periodic_thread and self._periodic_thread.is_alive():
            self._periodic_thread.join(timeout=2)
        self.cleanup_routes()


bypass_manager = BypassManager()
