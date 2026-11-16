@echo off
setlocal EnableExtensions

cd /d "%~dp0"

where openconnect >nul 2>&1
if errorlevel 1 (
    if not exist "C:\Program Files\OpenConnect-GUI\openconnect.exe" (
        if not exist "C:\Program Files\OpenConnect\openconnect.exe" (
            echo OpenConnect tidak ditemukan. Mendownload installer...
            powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.infradead.org/openconnect-gui/download/openconnect-gui-1.6.2-win64.exe' -OutFile '%TEMP%\openconnect-installer.exe'"
            if errorlevel 1 (
                echo [ERROR] Gagal mendownload OpenConnect.
                pause
                exit /b 1
            )
            echo Menjalankan installer OpenConnect...
            echo Pastikan mencentang opsi 'console' pada saat instalasi.
            start /wait "" "%TEMP%\openconnect-installer.exe" /ALLUSERS
            del /f /q "%TEMP%\openconnect-installer.exe"
        )
    )
)

echo.
echo [OK] Pengecekan dependensi selesai.

REM Pastikan OpenConnect terdaftar di System PATH untuk semua user
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path 'C:\Program Files\OpenConnect-GUI\openconnect.exe') { $p = [Environment]::GetEnvironmentVariable('Path', 'Machine'); if ($p -notlike '*OpenConnect-GUI*') { [Environment]::SetEnvironmentVariable('Path', $p.TrimEnd(';') + ';C:\Program Files\OpenConnect-GUI', 'Machine') } }" >nul 2>&1

if exist "%~dp0VPN Helper.exe" (
    echo Menjalankan VPN Helper...
    start "" "%~dp0VPN Helper.exe"
) else (
    echo [INFO] VPN Helper.exe siap dijalankan.
)
exit /b 0
