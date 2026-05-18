#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Python is not installed or not available in PATH."
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Requesting Administrator/root permission..."
  exec sudo "$0"
fi

exec "$PYTHON_BIN" app.py
