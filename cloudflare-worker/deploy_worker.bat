@echo off
setlocal enabledelayedexpansion

echo ================================================================
echo   CloudStream WebDAV Bridge - Cloudflare Worker Deployment
echo ================================================================
echo.

cd /d "%~dp0"

echo [1/3] Verifying Cloudflare Wrangler authentication...
call npx wrangler whoami > "%TEMP%\wrangler_auth_check.tmp" 2>&1
set WHOAMI_EXIT=%ERRORLEVEL%
findstr /i /c:"not authenticated" /c:"Please run" /c:"You are not logged in" "%TEMP%\wrangler_auth_check.tmp" >nul
if !ERRORLEVEL! equ 0 (
    set NEED_LOGIN=1
) else if %WHOAMI_EXIT% neq 0 (
    set NEED_LOGIN=1
) else (
    set NEED_LOGIN=0
)
if exist "%TEMP%\wrangler_auth_check.tmp" del "%TEMP%\wrangler_auth_check.tmp"

if !NEED_LOGIN! equ 1 (
    echo [!] Wrangler is not authenticated. Initiating Cloudflare login...
    call npx wrangler login
    if !ERRORLEVEL! neq 0 (
        echo [X] Authentication failed or was cancelled. Please log in and retry.
        pause
        exit /b 1
    )
) else (
    echo [OK] Wrangler is authenticated with Cloudflare.
)

echo.
echo [2/3] Deploying Worker to Cloudflare Global Edge Network...
call npx wrangler deploy
if !ERRORLEVEL! neq 0 (
    echo.
    echo [X] Worker deployment encountered an error. Please inspect the log above.
    pause
    exit /b !ERRORLEVEL!
)

echo.
echo ================================================================
echo   DEPLOYMENT SUCCESSFUL - PERMANENT WORKER ACTIVE
echo ================================================================
echo.
echo [3/3] Deployed Permanent Worker Endpoint:
echo   Worker Name : cloudstream-dav-bridge
echo   Worker URL  : https://cloudstream-dav-bridge.<subdomain>.workers.dev
echo                 (See exact workers.dev URL printed above)
echo.
echo ================================================================
echo   CX FILE EXPLORER CONFIGURATION (SET ONCE AND NEVER CHANGE)
echo ================================================================
echo.
echo   Configure CX File Explorer ONCE with these parameters:
echo     - Service Type : WebDAV
echo     - Host         : cloudstream-dav-bridge.<subdomain>.workers.dev
echo                      (Note: Do NOT prefix with "https://")
echo     - Path         : /dav/<your-device-id>/
echo                      (e.g., /dav/rmx3031-4f9a2e81c0d5/)
echo     - Port         : 443
echo     - SSL / HTTPS  : Checked / Enabled
echo     - Anonymous    : Checked / Enabled (or user: admin, pass: none)
echo.
echo   WHY THIS IS PERMANENT:
echo   - The Cloudflare Worker URL is your static entrypoint for life.
echo   - When Google Cloud Shell restarts or assigns dynamic URLs, the
echo     worker transparently resolves the active backend.
echo   - You NEVER need to re-enter host settings in CX File Explorer!
echo ================================================================
echo.
pause
