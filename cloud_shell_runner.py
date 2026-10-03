#!/usr/bin/env python3
"""
CloudStream WebDAV Bridge - Google Cloud Shell Persistent Runner & Anti-Idle Daemon
Optimized for Google Cloud Shell (5GB Persistent Disk at $HOME).

Features:
  - Persistent Library: Stored at $HOME/.cloudstream-bridge/mounts.json across reboots/restarts.
  - Automatic Dependency Bootstrap: Installs FastAPI, Uvicorn, HTTPX, QRcode, etc.
  - Standalone Cloudflared Binary Management: Downloads & executes cloudflared tunnel.
  - Clean Process Management: Recycles prior server & tunnel instances.
  - Tunnel URL Extraction & Terminal QR Code Display.
  - 12-Hour Anti-Idle Pulse: 50s terminal heartbeat querying live telemetry to prevent 20m timeout.
  - Graceful Signal Trapping: Handles SIGINT/SIGTERM cleanly.
"""

import os
import sys
import time
import re
import signal
import shutil
import urllib.request
import subprocess
import json
from datetime import datetime

# ANSI Color codes
BOLD = "\033[1m"
GREEN = "\033[1;32m"
CYAN = "\033[1;36m"
YELLOW = "\033[1;33m"
MAGENTA = "\033[1;35m"
WHITE = "\033[1;37m"
DIM = "\033[2m"
RESET = "\033[0m"

HOME = os.path.expanduser("~")
CS_DIR = os.path.join(HOME, ".cloudstream-bridge")
BIN_DIR = os.path.join(CS_DIR, "bin")
MOUNTS_DB_PATH = os.path.join(CS_DIR, "mounts.json")
SERVER_LOG = os.path.join(CS_DIR, "server.log")
TUNNEL_LOG = os.path.join(CS_DIR, "tunnel.log")

REPO_DIR = os.path.abspath(os.path.dirname(__file__))

# Global process holders for signal handling
server_proc = None
tunnel_proc = None


def cleanup_and_exit(signum=None, frame=None):
    """Gracefully terminate background child processes."""
    global server_proc, tunnel_proc
    print(f"\n{YELLOW}[!] Shutting down CloudStream Bridge processes...{RESET}")
    if tunnel_proc and tunnel_proc.poll() is None:
        try:
            tunnel_proc.terminate()
            tunnel_proc.wait(timeout=3)
        except Exception:
            try:
                tunnel_proc.kill()
            except Exception:
                pass

    if server_proc and server_proc.poll() is None:
        try:
            server_proc.terminate()
            server_proc.wait(timeout=3)
        except Exception:
            try:
                server_proc.kill()
            except Exception:
                pass

    print(f"{GREEN}[✓] CloudStream Bridge successfully stopped.{RESET}")
    sys.exit(0)


def setup_persistent_storage():
    """Ensure ~/.cloudstream-bridge directories exist and seed mounts.json if empty."""
    os.makedirs(CS_DIR, exist_ok=True)
    os.makedirs(BIN_DIR, exist_ok=True)

    # Export MOUNTS_DB_PATH environment variable
    os.environ["MOUNTS_DB_PATH"] = MOUNTS_DB_PATH

    # If persistent mounts.json does not exist yet, seed it from local repo if available
    if not os.path.exists(MOUNTS_DB_PATH):
        local_mounts = os.path.join(REPO_DIR, "mounts.json")
        if os.path.exists(local_mounts):
            try:
                shutil.copy2(local_mounts, MOUNTS_DB_PATH)
                print(f"{GREEN}[*] Seeded persistent mounts from repo to {MOUNTS_DB_PATH}{RESET}")
            except Exception as e:
                with open(MOUNTS_DB_PATH, "w", encoding="utf-8") as f:
                    f.write("{}")
        else:
            with open(MOUNTS_DB_PATH, "w", encoding="utf-8") as f:
                f.write("{}")

    mount_count = 0
    try:
        with open(MOUNTS_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            mount_count = len(data)
    except Exception:
        mount_count = 0

    print(f"{CYAN}[*] Persistent directory: {CS_DIR}{RESET}")
    print(f"{CYAN}[*] Persistent mounts DB: {MOUNTS_DB_PATH} ({mount_count} streams indexed){RESET}")
    return mount_count


def ensure_python_dependencies():
    """Verify and install required Python packages automatically."""
    required = ["fastapi", "uvicorn", "httpx", "aiofiles", "pydantic", "jinja2", "qrcode"]
    missing = []
    for pkg in required:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if missing:
        print(f"{YELLOW}[*] Installing required packages: {', '.join(missing)}...{RESET}")
        cmd = [
            sys.executable, "-m", "pip", "install", "-q",
            "fastapi", "uvicorn[standard]", "httpx", "aiofiles", "pydantic", "jinja2", "qrcode"
        ]
        res = subprocess.run(cmd)
        if res.returncode != 0:
            print(f"{YELLOW}[!] Notice: pip install returned non-zero code {res.returncode}. Continuing...{RESET}")
        else:
            print(f"{GREEN}[✓] Python dependencies installed successfully.{RESET}")
    else:
        print(f"{GREEN}[✓] All Python dependencies satisfied.{RESET}")


def ensure_cloudflared_binary() -> str:
    """Locate or download the cloudflared binary."""
    # Check system PATH first
    system_bin = shutil.which("cloudflared")
    if system_bin and os.path.isfile(system_bin) and os.access(system_bin, os.X_OK):
        return system_bin

    # Check known standard paths
    for candidate in ["/usr/local/bin/cloudflared", "/usr/bin/cloudflared"]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    # Check user persistent bin directory
    local_bin = os.path.join(BIN_DIR, "cloudflared" + (".exe" if os.name == "nt" else ""))
    if os.path.isfile(local_bin) and (os.access(local_bin, os.X_OK) or os.name == "nt"):
        return local_bin

    # If on Linux amd64 (standard Google Cloud Shell), download cloudflared binary
    print(f"{YELLOW}[*] Downloading cloudflared binary to {local_bin}...{RESET}")
    download_url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    if os.name == "nt":
        download_url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

    try:
        req = urllib.request.Request(download_url, headers={"User-Agent": "CloudStreamRunner/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(local_bin, "wb") as out_file:
            shutil.copyfileobj(resp, out_file)
        if os.name != "nt":
            os.chmod(local_bin, 0o755)
        print(f"{GREEN}[✓] cloudflared successfully downloaded and configured: {local_bin}{RESET}")
        return local_bin
    except Exception as e:
        print(f"{YELLOW}[!] Failed to auto-download cloudflared ({e}). Trying system binary fallback...{RESET}")
        return "cloudflared"


def kill_old_processes():
    """Kill lingering uvicorn or cloudflared processes on Linux/Cloud Shell."""
    if os.name != "nt":
        try:
            subprocess.run(["pkill", "-9", "-f", "uvicorn.*main:app"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        try:
            subprocess.run(["pkill", "-9", "-f", "cloudflared.*tunnel"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        time.sleep(1)


def launch_server() -> subprocess.Popen:
    """Launch FastAPI server with stdout/stderr directed to server.log."""
    global server_proc
    env = os.environ.copy()
    env["MOUNTS_DB_PATH"] = MOUNTS_DB_PATH
    env["PYTHONUNBUFFERED"] = "1"

    log_file = open(SERVER_LOG, "w", encoding="utf-8")
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"],
        cwd=REPO_DIR,
        stdout=log_file,
        stderr=log_file,
        env=env
    )
    print(f"{CYAN}[*] CloudStream FastAPI server launched (PID: {server_proc.pid}){RESET}")
    print(f"{DIM}    Server logs: {SERVER_LOG}{RESET}")

    # Wait for server to become responsive
    healthy = False
    for _ in range(20):
        time.sleep(0.5)
        try:
            req = urllib.request.Request("http://127.0.0.1:7860/health")
            with urllib.request.urlopen(req, timeout=1.5) as r:
                if r.status == 200:
                    healthy = True
                    break
        except Exception:
            pass

    if healthy:
        print(f"{GREEN}[✓] Local FastAPI server online & healthy on port 7860.{RESET}")
    else:
        print(f"{YELLOW}[!] Server launched, waiting for initial requests...{RESET}")

    return server_proc


def launch_tunnel(cloudflared_path: str) -> subprocess.Popen:
    """Launch cloudflared tunnel pointing to http://localhost:7860."""
    global tunnel_proc
    if os.path.exists(TUNNEL_LOG):
        try:
            os.remove(TUNNEL_LOG)
        except Exception:
            pass

    log_file = open(TUNNEL_LOG, "w", encoding="utf-8")
    tunnel_cmd = [cloudflared_path, "tunnel", "--url", "http://localhost:7860"]
    tunnel_proc = subprocess.Popen(
        tunnel_cmd,
        stdout=log_file,
        stderr=log_file
    )
    print(f"{CYAN}[*] Cloudflare tunnel launched (PID: {tunnel_proc.pid}){RESET}")
    print(f"{DIM}    Tunnel logs: {TUNNEL_LOG}{RESET}")
    return tunnel_proc


def extract_tunnel_url(timeout_secs: int = 45) -> str:
    """Extract public trycloudflare URL from tunnel.log within timeout period."""
    print(f"{CYAN}[*] Extracting high-speed public tunnel URL (up to {timeout_secs}s)...{RESET}")
    start = time.time()
    url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

    while time.time() - start < timeout_secs:
        time.sleep(1)
        if os.path.exists(TUNNEL_LOG):
            try:
                with open(TUNNEL_LOG, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                matches = url_pattern.findall(content)
                if matches:
                    # Return the latest matched URL
                    return matches[-1]
            except Exception:
                pass

    return ""


def print_ascii_qr(url: str):
    """Print ASCII QR code of the tunnel URL for instant camera/TV scanning."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        print(f"\n{WHITE}{BOLD}   [ SCAN QR CODE WITH PHONE / TV CAMERA ]{RESET}")
        qr.print_ascii(invert=True)
        print()
    except Exception:
        # Graceful text fallback if qrcode ascii rendering is unavailable
        pass


def print_banner(tunnel_url: str, mount_count: int):
    """Print visually stunning ANSI box with connection endpoints."""
    host = tunnel_url.replace("https://", "").replace("http://", "").rstrip("/")
    dav_url = f"{tunnel_url}/dav/"
    border_len = 76
    line_sep = "═" * border_len

    print(f"\n{CYAN}╔{line_sep}╗{RESET}")
    print(f"{CYAN}║{RESET}  {GREEN}{BOLD}🎬 CLOUDSTREAM GOOGLE CLOUD SHELL BRIDGE ONLINE{RESET}" + " " * (border_len - 49) + f"{CYAN}║{RESET}")
    print(f"{CYAN}╠{line_sep}╣{RESET}")
    print(f"{CYAN}║{RESET}  {WHITE}{BOLD}🌐 Web UI:{RESET}            {CYAN}{tunnel_url:<54}{RESET} {CYAN}║{RESET}")
    print(f"{CYAN}║{RESET}  {WHITE}{BOLD}📁 WebDAV URL:{RESET}        {CYAN}{dav_url:<54}{RESET} {CYAN}║{RESET}")
    print(f"{CYAN}║{RESET}" + " " * border_len + f"{CYAN}║{RESET}")
    print(f"{CYAN}║{RESET}  {YELLOW}{BOLD}📱 CX File Explorer Settings (Android TV & Phone):{RESET}" + " " * (border_len - 52) + f"{CYAN}║{RESET}")
    print(f"{CYAN}║{RESET}     • Protocol:    {WHITE}WebDAV{RESET}" + " " * (border_len - 26) + f"{CYAN}║{RESET}")
    print(f"{CYAN}║{RESET}     • Host:        {CYAN}{host:<58}{RESET} {CYAN}║{RESET}")
    print(f"{CYAN}║{RESET}     • Port:        {WHITE}443{RESET}" + " " * (border_len - 23) + f"{CYAN}║{RESET}")
    print(f"{CYAN}║{RESET}     • Path:        {WHITE}/dav{RESET}" + " " * (border_len - 24) + f"{CYAN}║{RESET}")
    print(f"{CYAN}║{RESET}     • HTTPS:       {GREEN}ON (Checked){RESET}" + " " * (border_len - 32) + f"{CYAN}║{RESET}")
    print(f"{CYAN}║{RESET}     • Username:    {WHITE}admin (or check Anonymous){RESET}" + " " * (border_len - 46) + f"{CYAN}║{RESET}")
    print(f"{CYAN}║{RESET}" + " " * border_len + f"{CYAN}║{RESET}")
    print(f"{CYAN}║{RESET}  {MAGENTA}{BOLD}📺 Android APK Cloud Endpoint:{RESET} {CYAN}{tunnel_url:<44}{RESET} {CYAN}║{RESET}")
    print(f"{CYAN}║{RESET}  {GREEN}{BOLD}💾 Persistent Library:{RESET}         {WHITE}{mount_count} mounted streams loaded from disk{RESET}" + " " * max(0, border_len - 41 - len(str(mount_count)) - len(" mounted streams loaded from disk")) + f"{CYAN}║{RESET}")
    print(f"{CYAN}╚{line_sep}╝{RESET}\n")


def get_live_status():
    """Query local /api/status for stream metrics."""
    try:
        req = urllib.request.Request("http://127.0.0.1:7860/api/status")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return None


def run_heartbeat_loop():
    """
    12-Hour Anti-Idle Heartbeat Keep-Alive Loop.
    Google Cloud Shell disconnects after 20 minutes without terminal activity.
    Emits a clean 1-line status pulse every 50 seconds to keep session active.
    """
    start_time = time.time()
    pulse_count = 0
    print(f"{GREEN}[*] Anti-idle heartbeat active. Keep this Cloud Shell tab open while streaming.{RESET}")
    print(f"{DIM}[*] Press Ctrl+C at any time to cleanly stop.{RESET}\n")

    while True:
        try:
            pulse_count += 1
            now_str = datetime.now().strftime("%H:%M:%S")
            uptime_min = int((time.time() - start_time) / 60)

            status = get_live_status() or {}
            active_streams = status.get("active_streams", 0)
            library_gb = status.get("total_virtual_library_gb", 0.0)
            streamed_mb = status.get("streamed_mb", 0.0)
            streamed_gb = status.get("streamed_gb", 0.0)
            mounted_count = status.get("mounted_count", 0)

            streamed_str = f"{streamed_gb} GB" if streamed_gb >= 1.0 else f"{streamed_mb} MB"

            heartbeat_msg = (
                f"{DIM}[{now_str}]{RESET} 💓 "
                f"{GREEN}Heartbeat #{pulse_count}{RESET} | "
                f"{CYAN}CloudStream Active{RESET} | "
                f"Active Streams: {YELLOW}{active_streams}{RESET} | "
                f"Library: {WHITE}{library_gb} GB ({mounted_count} items){RESET} | "
                f"Consumed: {CYAN}{streamed_str}{RESET} | "
                f"Uptime: {MAGENTA}{uptime_min}m{RESET}"
            )
            print(heartbeat_msg)
            sys.stdout.flush()

            # Heartbeat pulse interval (50 seconds: prevents 20m timeout)
            time.sleep(50)
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception as e:
            time.sleep(50)


def main():
    # Register OS signal traps
    signal.signal(signal.SIGINT, cleanup_and_exit)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, cleanup_and_exit)

    print(f"\n{BOLD}{CYAN}=== CloudStream Bridge Google Cloud Shell Persistent Runner ==={RESET}")

    # 1. Setup persistent storage
    mount_count = setup_persistent_storage()

    # 2. Check and install dependencies
    ensure_python_dependencies()

    # 3. Locate or download cloudflared
    cloudflared_bin = ensure_cloudflared_binary()

    # 4. Kill old processes
    kill_old_processes()

    # 5. Launch FastAPI server
    launch_server()

    # 6. Launch Cloudflare tunnel
    launch_tunnel(cloudflared_bin)

    # 7. Extract public tunnel URL
    tunnel_url = extract_tunnel_url(timeout_secs=45)

    if not tunnel_url:
        print(f"\n{YELLOW}[!] Warning: Could not automatically parse trycloudflare URL within 45s.{RESET}")
        print(f"{DIM}    Check {TUNNEL_LOG} for details.{RESET}")
        tunnel_url = "http://localhost:7860"

    # 8. Display ASCII QR Code & ANSI Banner
    if "trycloudflare.com" in tunnel_url:
        print_ascii_qr(tunnel_url)
    print_banner(tunnel_url, mount_count)

    # 9. Enter anti-idle heartbeat loop
    run_heartbeat_loop()

    cleanup_and_exit()


if __name__ == "__main__":
    main()
