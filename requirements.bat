@echo off
setlocal EnableExtensions

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Python is not installed or not available in PATH.
    echo Install Python first, then run this installer again.
    exit /b 1
)

python -c "import importlib.util, sys; mods=('flask','dotenv','requests','cryptography'); missing=[m for m in mods if importlib.util.find_spec(m) is None]; print('Missing packages: ' + ', '.join(missing) if missing else 'All Python dependencies are already installed.'); sys.exit(1 if missing else 0)"
if errorlevel 1 (
    python -m pip --version >nul 2>&1
    if errorlevel 1 (
        python -m ensurepip --upgrade
        if errorlevel 1 exit /b 1
    )

    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency installation failed.
        pause
        exit /b 1
    )
)

echo.
echo Dependency check complete. This installer will self-destruct in 3 seconds.
start "" cmd /c "timeout /t 3 /nobreak >nul & del /f /q ""%~f0"""
exit /b 0
