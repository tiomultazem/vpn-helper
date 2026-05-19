#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Python is not installed or not available in PATH."
  echo "Install Python first, then run this installer again."
  exit 1
fi

if "$PYTHON_BIN" -c "import importlib.util, sys; mods=('flask','dotenv','requests','cryptography','pystray','PIL'); missing=[m for m in mods if importlib.util.find_spec(m) is None]; print('Missing packages: ' + ', '.join(missing) if missing else 'All Python dependencies are already installed.'); sys.exit(1 if missing else 0)"; then
  :
else
  if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
    "$PYTHON_BIN" -m ensurepip --upgrade
  fi

  "$PYTHON_BIN" -m pip install -r requirements.txt
fi

echo
echo "Dependency check complete. This installer will self-destruct in 3 seconds."
sleep 3
rm -- "$0"
