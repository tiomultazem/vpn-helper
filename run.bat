@echo off
cls
setlocal
set "BASE_DIR=%~dp0"
set "TARGET_EXE=%BASE_DIR%VPN Helper.exe"

REM Jika diminta build eksplisit (--publish, --build, /b) atau biner belum ada, panggil compile.bat
if "%~1"=="--publish" goto :COMPILE
if "%~1"=="--build" goto :COMPILE
if "%~1"=="/b" goto :COMPILE
if not exist "%TARGET_EXE%" goto :COMPILE
goto :RUN_EXE

:COMPILE
echo [%date% %time%] Biner belum siap atau parameter kompilasi terdeteksi. Memanggil compile.bat...
call "%BASE_DIR%compile.bat" %*
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Kompilasi gagal. Eksekusi dibatalkan.
    exit /b %ERRORLEVEL%
)

:RUN_EXE
REM Hentikan instans lama sebelum menjalankan biner
taskkill /F /IM "VPN Helper.exe" >nul 2>&1
taskkill /F /IM "openconnect.exe" >nul 2>&1

echo [OK] Menjalankan: "%TARGET_EXE%"
start "" "%TARGET_EXE%"
exit /b 0
