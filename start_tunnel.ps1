<#
.SYNOPSIS
  CloudStream Zero-Signup Instant Public HTTPS Tunnel
  Downloads official cloudflared binary and gives an instant public HTTPS WebDAV link.
  Zero account required, zero login, zero credit card.
#>

$port = 7860
$binDir = "$PSScriptRoot\.bin"
if (-not (Test-Path $binDir)) { New-Item -ItemType Directory -Path $binDir -Force | Out-Null }
$cloudflaredPath = "$binDir\cloudflared.exe"

if (-not (Test-Path $cloudflaredPath)) {
    Write-Host "[*] Downloading official Cloudflare Tunnel runner (one-time setup, zero signup)..." -ForegroundColor Cyan
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    Invoke-WebRequest -Uri $url -OutFile $cloudflaredPath -UseBasicParsing
    Write-Host "[✓] cloudflared ready!" -ForegroundColor Green
}

Write-Host "`n========================================================" -ForegroundColor Cyan
Write-Host " 🎬 STARTING INSTANT PUBLIC WEBDAV TUNNEL (PORT $port)" -ForegroundColor Cyan
Write-Host " Zero login, zero account, zero hassle." -ForegroundColor Yellow
Write-Host "========================================================`n" -ForegroundColor Cyan

& $cloudflaredPath tunnel --url "http://localhost:$port"
