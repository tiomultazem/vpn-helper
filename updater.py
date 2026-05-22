import fnmatch
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import requests
from flask import jsonify


CONFIG_FILE = "config.json"
DEFAULT_OWNER = "tiomultazem"
DEFAULT_REPO = "vpn-helper"
DEFAULT_BRANCH = "main"
UPDATE_CHECK_TIMEOUT = 15
UPDATE_DOWNLOAD_TIMEOUT = (10, 120)

PRESERVE_NAMES = {
    ".env",
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    "VPN Helper.lnk",
    "server.out.log",
    "server.err.log",
    "update.log"
}
PRESERVE_PATTERNS = ("*.log", "*.pyc", "*.pyo")


def register_updater_routes(app, root_dir=None, logger=None):
    if getattr(app, "_vpn_helper_updater_registered", False):
        return

    app._vpn_helper_updater_registered = True
    root = Path(root_dir or Path(__file__).resolve().parent).resolve()

    def log(message, level="info"):
        if logger:
            try:
                logger(message, level)
                return
            except Exception:
                pass
        print(f"[{level.upper()}] {message}")

    @app.get("/api/update/check")
    def check_update():
        try:
            return jsonify(_build_update_payload(root))
        except Exception as exc:
            log(f"Update check gagal: {exc}", "warning")
            return jsonify({
                "success": False,
                "message": str(exc),
                "update_available": False
            })

    @app.post("/api/update/install")
    def install_update():
        try:
            payload = _prepare_update(root, log)
            _start_apply_process(payload, root, log)
            _exit_current_process_soon()
            return jsonify({
                "success": True,
                "message": "Update dimulai. App akan restart sebentar lagi.",
                "version": payload["remote_version"]
            })
        except Exception as exc:
            log(f"Update install gagal: {exc}", "error")
            return jsonify({"success": False, "message": str(exc)}), 500


def _build_update_payload(root):
    local_config = _read_config(root / CONFIG_FILE)
    remote_config = _fetch_remote_config(local_config)

    local_version = str(local_config.get("version", "0"))
    remote_version = str(remote_config.get("version", "0"))
    changelog = _normalize_changelog(remote_config.get("changelog", []))

    return {
        "success": True,
        "local_version": local_version,
        "remote_version": remote_version,
        "update_available": _version_key(remote_version) > _version_key(local_version),
        "changelog": changelog
    }


def _prepare_update(root, log):
    local_config = _read_config(root / CONFIG_FILE)
    remote_config = _fetch_remote_config(local_config)
    local_version = str(local_config.get("version", "0"))
    remote_version = str(remote_config.get("version", "0"))

    if _version_key(remote_version) <= _version_key(local_version):
        raise RuntimeError("Versi lokal sudah paling baru.")

    zip_url = _github_zip_url(remote_config, local_config)
    work_dir = Path(tempfile.mkdtemp(prefix="vpn-helper-update-"))
    zip_path = work_dir / "update.zip"
    extract_dir = work_dir / "extract"

    log(f"Download update {remote_version} dari GitHub...", "info")
    _download_file(zip_url, zip_path)

    log("Ekstrak paket update...", "info")
    source_root = _extract_zip(zip_path, extract_dir)

    return {
        "work_dir": str(work_dir),
        "source_root": str(source_root),
        "remote_version": remote_version
    }


def _read_config(path):
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _fetch_remote_config(local_config):
    url = _github_config_url(local_config)
    response = requests.get(
        url,
        timeout=UPDATE_CHECK_TIMEOUT,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "VPN-Helper-Updater"
        }
    )
    response.raise_for_status()
    return response.json()


def _github_config_url(config):
    github = config.get("github", {}) if isinstance(config, dict) else {}
    owner = github.get("owner", DEFAULT_OWNER)
    repo = github.get("repo", DEFAULT_REPO)
    branch = github.get("branch", DEFAULT_BRANCH)
    return github.get("config_url") or f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{CONFIG_FILE}"


def _github_zip_url(remote_config, local_config):
    github = {}
    if isinstance(local_config, dict):
        github.update(local_config.get("github", {}) or {})
    if isinstance(remote_config, dict):
        github.update(remote_config.get("github", {}) or {})

    owner = github.get("owner", DEFAULT_OWNER)
    repo = github.get("repo", DEFAULT_REPO)
    branch = github.get("branch", DEFAULT_BRANCH)
    return github.get("zip_url") or f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"


def _download_file(url, target):
    with requests.get(
        url,
        stream=True,
        timeout=UPDATE_DOWNLOAD_TIMEOUT,
        headers={"User-Agent": "VPN-Helper-Updater"}
    ) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if chunk:
                    handle.write(chunk)


def _extract_zip(zip_path, extract_dir):
    extract_dir.mkdir(parents=True, exist_ok=True)
    extract_root = extract_dir.resolve()

    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (extract_root / member.filename).resolve()
            if os.path.commonpath([str(extract_root), str(target)]) != str(extract_root):
                raise RuntimeError("Paket update tidak valid.")
        archive.extractall(extract_root)

    entries = [item for item in extract_root.iterdir() if item.name != "__MACOSX"]
    dirs = [item for item in entries if item.is_dir()]
    return dirs[0] if len(dirs) == 1 else extract_root


def _normalize_changelog(raw):
    entries = []

    if isinstance(raw, dict):
        raw = [{"version": version, "changes": changes} for version, changes in raw.items()]

    if not isinstance(raw, list):
        return entries

    for item in raw:
        if not isinstance(item, dict):
            continue
        version = str(item.get("version", "")).strip()
        if not version:
            continue

        changes = item.get("changes", item.get("items", item.get("change", [])))
        if isinstance(changes, str):
            changes = [changes]
        if not isinstance(changes, list):
            changes = []

        entries.append({
            "version": version,
            "date": str(item.get("date", "")).strip(),
            "changes": [str(change) for change in changes if str(change).strip()]
        })

    return sorted(entries, key=lambda entry: _version_key(entry["version"]), reverse=True)


def _version_key(version):
    parts = []
    for part in str(version).replace("-", ".").split("."):
        digits = "".join(char for char in part if char.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def _start_apply_process(payload, root, log):
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--apply",
        payload["source_root"],
        str(root),
        str(os.getpid()),
        sys.executable,
        payload["work_dir"]
    ]

    kwargs = {
        "cwd": str(root),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    subprocess.Popen(command, **kwargs)
    log("Updater apply process siap. App akan ditutup.", "info")


def _exit_current_process_soon():
    def delayed_exit():
        time.sleep(1.0)
        os._exit(0)

    threading.Thread(target=delayed_exit, daemon=True).start()


def _apply_update(source_root, target_root, parent_pid, python_exe, work_dir):
    target = Path(target_root).resolve()
    source = Path(source_root).resolve()
    work = Path(work_dir).resolve()

    _wait_for_parent(parent_pid)
    _write_update_log(target, "Apply update mulai.")

    for item in list(target.iterdir()):
        if _should_preserve(item):
            continue
        _remove_path(item)

    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)

    _write_update_log(target, "Apply update selesai. Restart app.")
    _restart_app(target, python_exe)

    try:
        shutil.rmtree(work, ignore_errors=True)
    except Exception:
        pass


def _should_preserve(path):
    name = path.name
    lower = name.lower()
    preserved = {item.lower() for item in PRESERVE_NAMES}
    if lower in preserved:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in PRESERVE_PATTERNS)


def _remove_path(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _wait_for_parent(pid):
    deadline = time.time() + 30
    while time.time() < deadline and _pid_exists(pid):
        time.sleep(0.5)

    if _pid_exists(pid):
        _stop_process(pid)
        time.sleep(1.0)


def _pid_exists(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False

    if pid <= 0:
        return False

    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False
        )
        return str(pid) in result.stdout

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _stop_process(pid):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], check=False)
        else:
            os.kill(int(pid), signal.SIGTERM)
    except Exception:
        pass


def _restart_app(target, python_exe):
    run_bat = target / "run.bat"
    app_py = target / "app.py"

    if os.name == "nt" and run_bat.exists():
        subprocess.Popen(
            ["cmd", "/c", "start", "", str(run_bat)],
            cwd=str(target),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL
        )
        return

    if app_py.exists():
        subprocess.Popen(
            [python_exe or sys.executable, str(app_py)],
            cwd=str(target),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL
        )


def _write_update_log(target, message):
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with (target / "update.log").open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


if __name__ == "__main__" and len(sys.argv) >= 7 and sys.argv[1] == "--apply":
    _apply_update(
        source_root=sys.argv[2],
        target_root=sys.argv[3],
        parent_pid=sys.argv[4],
        python_exe=sys.argv[5],
        work_dir=sys.argv[6]
    )
