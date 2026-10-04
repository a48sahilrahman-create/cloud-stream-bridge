#!/usr/bin/env python3
"""
CloudStream WebDAV Bridge - Step 2: Google Cloud Shell Launcher (Persistent Runner & Anti-Idle Daemon)
Optimized for Google Cloud Shell (5GB Persistent Disk at $HOME).
Routes streams using unique hardware-anchored device username (e.g. rmx3031-4f9a2e81c0d5) with zero Google Sign-In dependencies.

Updated 3-Step Suite Workflow:
  - Step 1: Device Identity & Pointer Hub (CloudStream Android App)
  - Step 2: Google Cloud Shell Launcher (Persistent Cloud Runner & Telemetry Pulse)
  - Step 3: CX File Explorer Setup (Permanent WebDAV Streaming on Android TV & Phone)

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
import urllib.error
import subprocess
import json
import argparse
from datetime import datetime

# ANSI Color codes
BOLD = "\033[1m"
GREEN = "\033[1;32m"
CYAN = "\033[1;36m"
YELLOW = "\033[1;33m"
RED = "\033[1;31m"
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


def parse_args(argv=None):
    """Parse command line arguments with environment variable fallbacks."""
    parser = argparse.ArgumentParser(
        description="CloudStream WebDAV Bridge - Step 2: Google Cloud Shell Launcher (Routes via unique hardware-anchored device username, e.g. rmx3031-4f9a2e81c0d5, zero Google Sign-In dependencies)"
    )
    default_user = (
        os.environ.get("CLOUDSTREAM_USER_ID")
        or os.environ.get("USER_ID")
        or "default"
    )
    default_hub = (
        os.environ.get("CLOUDSTREAM_HUB_URL")
        or os.environ.get("HUB_URL")
        or "https://cloud-stream-bridge.onrender.com"
    )
    default_port = int(os.environ.get("PORT", "7860"))

    parser.add_argument(
        "--user",
        default=default_user,
        help="Unique hardware-anchored device username (e.g. rmx3031-4f9a2e81c0d5, zero Google Sign-In) for multi-user routing (default: CLOUDSTREAM_USER_ID or 'default')"
    )
    parser.add_argument(
        "--hub",
        default=default_hub,
        help="Central Pointer Hub URL (default: CLOUDSTREAM_HUB_URL or 'https://cloud-stream-bridge.onrender.com')"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help="Port for local FastAPI server (default: 7860)"
    )
    return parser.parse_args(argv)


def is_valid_tunnel_url(tunnel_url: str) -> bool:
    """
    Validate that tunnel_url is a genuine public Cloudflare tunnel.
    Guards strictly against registering localhost or 127.0.0.1 fallback URLs.
    """
    if not tunnel_url or not isinstance(tunnel_url, str):
        return False
    lower = tunnel_url.lower()
    if "localhost" in lower or "127.0.0.1" in lower:
        return False
    return "trycloudflare.com" in lower


def register_with_hub(
    hub_url: str,
    user_id: str,
    tunnel_url: str,
    timeout: float = 15.0,
    verbose: bool = True,
    log_errors: bool = True
) -> bool:
    """
    Sends HTTP POST to {hub_url}/api/register with {"user_id": user_id, "tunnel_url": tunnel_url}.
    Logs clean success or warning message. Timeout increased to 15s for Render cold boots.
    """
    if not hub_url or not user_id or not tunnel_url:
        if verbose or log_errors:
            print(f"{YELLOW}[!] Registration skipped: missing hub_url, user_id, or tunnel_url.{RESET}")
        return False

    # Guard: Never register localhost or non-trycloudflare URLs as a public tunnel URL
    if not is_valid_tunnel_url(tunnel_url):
        if verbose or log_errors:
            print(f"{RED}[!] Error: Cloudflare tunnel failed to establish. Refusing to register fallback URL '{tunnel_url}' with Central Hub.{RESET}")
        return False

    clean_hub = hub_url.rstrip("/")
    endpoint = f"{clean_hub}/api/register"
    payload = json.dumps({"user_id": user_id, "tunnel_url": tunnel_url}).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "CloudStreamRunner/1.0"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", getattr(resp, "code", 200))
            if 200 <= status < 300:
                if verbose:
                    print(f"{GREEN}[✓] Successfully registered with Hub ({clean_hub}) for device username '{user_id}'.{RESET}")
                return True
            else:
                if verbose or log_errors:
                    print(f"{YELLOW}[!] Hub registration returned HTTP status {status}: {endpoint}{RESET}")
                return False
    except urllib.error.HTTPError as e:
        if verbose or log_errors:
            err_detail = ""
            try:
                if hasattr(e, "read"):
                    err_body = e.read().decode("utf-8", errors="ignore").strip()
                    try:
                        err_json = json.loads(err_body)
                        err_detail = err_json.get("detail") or err_json.get("error") or err_body
                    except Exception:
                        err_detail = err_body
            except Exception:
                pass
            if not err_detail:
                err_detail = str(getattr(e, "reason", e))
            print(f"{YELLOW}[!] Hub registration HTTP error {e.code} ({e.reason}): {err_detail} ({endpoint}){RESET}")
        return False
    except (TimeoutError, urllib.error.URLError) as e:
        is_timeout = isinstance(e, TimeoutError) or ("timed out" in str(getattr(e, "reason", e)).lower())
        if verbose or log_errors:
            if is_timeout:
                print(f"{YELLOW}[!] Hub registration timed out after {timeout}s (Render cold boot may be in progress): {endpoint}{RESET}")
            else:
                print(f"{YELLOW}[!] Hub registration network error ({endpoint}): {getattr(e, 'reason', e)}{RESET}")
        return False
    except Exception as e:
        if verbose or log_errors:
            print(f"{YELLOW}[!] Hub registration warning ({endpoint}): {e}{RESET}")
        return False


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


def launch_server(port: int = 7860) -> subprocess.Popen:
    """Launch FastAPI server with stdout/stderr directed to server.log."""
    global server_proc
    env = os.environ.copy()
    env["MOUNTS_DB_PATH"] = MOUNTS_DB_PATH
    env["PYTHONUNBUFFERED"] = "1"

    log_file = open(SERVER_LOG, "w", encoding="utf-8")
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", str(port)],
        cwd=REPO_DIR,
        stdout=log_file,
        stderr=log_file,
        env=env
    )
    print(f"{CYAN}[*] CloudStream FastAPI server launched on port {port} (PID: {server_proc.pid}){RESET}")
    print(f"{DIM}    Server logs: {SERVER_LOG}{RESET}")

    # Wait for server to become responsive
    healthy = False
    for _ in range(20):
        time.sleep(0.5)
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=1.5) as r:
                if r.status == 200:
                    healthy = True
                    break
        except Exception:
            pass

    if healthy:
        print(f"{GREEN}[✓] Local FastAPI server online & healthy on port {port}.{RESET}")
    else:
        print(f"{YELLOW}[!] Server launched, waiting for initial requests...{RESET}")

    return server_proc


def launch_tunnel(cloudflared_path: str, port: int = 7860) -> subprocess.Popen:
    """Launch cloudflared tunnel pointing to http://localhost:{port}."""
    global tunnel_proc
    if os.path.exists(TUNNEL_LOG):
        try:
            os.remove(TUNNEL_LOG)
        except Exception:
            pass

    log_file = open(TUNNEL_LOG, "w", encoding="utf-8")
    tunnel_cmd = [cloudflared_path, "tunnel", "--url", f"http://localhost:{port}"]
    tunnel_proc = subprocess.Popen(
        tunnel_cmd,
        stdout=log_file,
        stderr=log_file
    )
    print(f"{CYAN}[*] Cloudflare tunnel launched (PID: {tunnel_proc.pid}){RESET}")
    print(f"{DIM}    Tunnel logs: {TUNNEL_LOG}{RESET}")
    return tunnel_proc


ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
CLOUDFLARE_URL_PATTERN = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


def extract_tunnel_url(timeout_secs: int = 45) -> str:
    """
    Extract public trycloudflare URL from tunnel.log within timeout period.
    Strips ANSI escape sequences from stdout/stderr lines before applying regex.
    Logs success or failure clearly.
    """
    global tunnel_proc
    print(f"{CYAN}[*] Extracting high-speed public tunnel URL (up to {timeout_secs}s)...{RESET}")
    start = time.time()

    while time.time() - start < timeout_secs:
        # Check if cloudflared process died prematurely
        if tunnel_proc is not None and tunnel_proc.poll() is not None:
            ret = tunnel_proc.poll()
            print(f"{YELLOW}[!] Cloudflared failed: process exited prematurely with exit code {ret}.{RESET}")
            if os.path.exists(TUNNEL_LOG):
                try:
                    with open(TUNNEL_LOG, "r", encoding="utf-8", errors="ignore") as f:
                        log_lines = f.readlines()
                    tail_lines = [ANSI_ESCAPE_PATTERN.sub("", line).strip() for line in log_lines[-8:] if line.strip()]
                    if tail_lines:
                        print(f"{DIM}    cloudflared output:{RESET}")
                        for l in tail_lines:
                            print(f"{DIM}      {l}{RESET}")
                except Exception:
                    pass
            return ""

        time.sleep(1)
        if os.path.exists(TUNNEL_LOG):
            try:
                matched_urls = []
                with open(TUNNEL_LOG, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        clean_line = ANSI_ESCAPE_PATTERN.sub("", line)
                        matches = CLOUDFLARE_URL_PATTERN.findall(clean_line)
                        if matches:
                            matched_urls.extend(matches)
                if matched_urls:
                    tunnel_url = matched_urls[-1]
                    print(f"{GREEN}[✓] Successfully extracted Cloudflare tunnel URL: {tunnel_url}{RESET}")
                    return tunnel_url
            except Exception:
                pass

    print(f"{YELLOW}[!] Failed to extract trycloudflare URL within {timeout_secs}s timeout.{RESET}")
    if os.path.exists(TUNNEL_LOG):
        try:
            with open(TUNNEL_LOG, "r", encoding="utf-8", errors="ignore") as f:
                log_lines = f.readlines()
            tail_lines = [ANSI_ESCAPE_PATTERN.sub("", line).strip() for line in log_lines[-8:] if line.strip()]
            if tail_lines:
                print(f"{DIM}    Recent cloudflared log output:{RESET}")
                for l in tail_lines:
                    print(f"{DIM}      {l}{RESET}")
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


ANSI_PATTERN = ANSI_ESCAPE_PATTERN


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes for accurate string length measurement."""
    return ANSI_PATTERN.sub("", text)


def _box_row(content: str, border_len: int = 76) -> str:
    """Format a single box row ensuring exact outer border alignment."""
    plain_len = len(_strip_ansi(content))
    pad = max(0, border_len - 1 - plain_len)
    return f"{CYAN}║{RESET} {content}" + (" " * pad) + f"{CYAN}║{RESET}"


def print_banner(
    tunnel_url: str,
    mount_count: int,
    user_id: str = "default",
    hub_url: str = "https://cloud-stream-bridge.onrender.com"
):
    """Print visually stunning ANSI box with Step 2 status and Step 3 CX File Explorer setup."""
    clean_hub = hub_url.rstrip("/")
    perm_dav = f"{clean_hub}/dav/{user_id}/"
    direct_dav = f"{tunnel_url.rstrip('/')}/dav/"
    hub_host = clean_hub.replace("https://", "").replace("http://", "").split("/")[0]

    border_len = 76
    line_sep = "═" * border_len

    print(f"\n{CYAN}╔{line_sep}╗{RESET}")
    print(_box_row(f"{GREEN}{BOLD}🎬 STEP 2: GOOGLE CLOUD SHELL LAUNCHER ONLINE{RESET}", border_len))
    print(f"{CYAN}╠{line_sep}╣{RESET}")
    print(_box_row(f"{WHITE}{BOLD}User ID:{RESET}          {MAGENTA}{BOLD}{user_id}{RESET}", border_len))
    print(_box_row(f"  {DIM}└─ Unique hardware device ID (zero Google Sign-In){RESET}", border_len))
    print(_box_row(f"{WHITE}{BOLD}Permanent WebDAV:{RESET} {GREEN}{BOLD}{perm_dav}{RESET}", border_len))
    print(_box_row(f"{WHITE}{BOLD}Direct Tunnel:{RESET}    {CYAN}{direct_dav}{RESET}", border_len))
    print(_box_row(f"{WHITE}{BOLD}Web UI:{RESET}           {CYAN}{tunnel_url}{RESET}", border_len))
    print(_box_row("", border_len))
    print(_box_row(f"{YELLOW}{BOLD}📱 Step 3: CX File Explorer Setup (Android TV & Phone):{RESET}", border_len))
    print(_box_row(f"    • Protocol:    {WHITE}WebDAV{RESET}", border_len))
    print(_box_row(f"    • Host:        {CYAN}{hub_host}{RESET} {DIM}(Permanent){RESET}", border_len))
    print(_box_row(f"    • Port:        {WHITE}443{RESET}", border_len))
    print(_box_row(f"    • Path:        {WHITE}/dav/{user_id}/{RESET}", border_len))
    print(_box_row(f"    • HTTPS:       {GREEN}ON (Checked){RESET}", border_len))
    print(_box_row(f"    • Username:    {WHITE}admin (or check Anonymous){RESET}", border_len))
    print(_box_row("", border_len))
    print(_box_row(f"{MAGENTA}{BOLD}📺 Android APK Endpoint:{RESET} {CYAN}{clean_hub}{RESET}", border_len))
    print(_box_row(f"{GREEN}{BOLD}💾 Persistent Library:{RESET}   {WHITE}{mount_count} mounted streams loaded from disk{RESET}", border_len))
    print(f"{CYAN}╚{line_sep}╝{RESET}\n")


def get_live_status(port: int = 7860):
    """Query local /api/status for stream metrics."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return None


def run_heartbeat_loop(
    hub_url: str = "https://cloud-stream-bridge.onrender.com",
    user_id: str = "default",
    tunnel_url: str = "",
    port: int = 7860
):
    """
    12-Hour Anti-Idle Heartbeat Keep-Alive Loop.
    Google Cloud Shell disconnects after 20 minutes without terminal activity.
    Emits a clean 1-line status pulse every 50 seconds to keep session active,
    and sends a keep-alive pulse to {hub_url}/api/register to refresh central hub 12h TTL.
    """
    global tunnel_proc
    start_time = time.time()
    pulse_count = 0
    clean_hub = hub_url.rstrip("/") if hub_url else ""
    print(f"{GREEN}[*] Step 2 anti-idle heartbeat active. Keep this Cloud Shell tab open while streaming.{RESET}")
    print(f"{DIM}[*] Device: {user_id} (Zero Google Sign-In) | Hub: {clean_hub or 'None'} | Interval: 50s | Ctrl+C to stop.{RESET}\n")

    while True:
        try:
            if tunnel_proc is not None and tunnel_proc.poll() is not None:
                ret = tunnel_proc.poll()
                print(f"\n{YELLOW}[!] Cloudflared tunnel died: process exited with exit code {ret}. Stopping heartbeat loop.{RESET}")
                break

            pulse_count += 1
            now_str = datetime.now().strftime("%H:%M:%S")
            uptime_min = int((time.time() - start_time) / 60)

            # Query local status
            status = get_live_status(port=port) or {}
            active_streams = status.get("active_streams", 0)
            library_gb = status.get("total_virtual_library_gb", 0.0)
            streamed_mb = status.get("streamed_mb", 0.0)
            streamed_gb = status.get("streamed_gb", 0.0)
            mounted_count = status.get("mounted_count", 0)
            streamed_str = f"{streamed_gb} GB" if streamed_gb >= 1.0 else f"{streamed_mb} MB"

            # Hub keep-alive pulse (refresh 12-hour TTL on central hub)
            hub_synced = False
            if clean_hub and is_valid_tunnel_url(tunnel_url):
                hub_synced = register_with_hub(clean_hub, user_id, tunnel_url, timeout=15.0, verbose=False, log_errors=True)
                if not hub_synced:
                    print(f"{YELLOW}[!] Hub heartbeat desync: Registration refresh failed for {clean_hub} (device: {user_id}){RESET}")
                hub_status_str = f"{GREEN}SYNCED [✓]{RESET}" if hub_synced else f"{YELLOW}DESYNC [!]{RESET}"
            else:
                hub_status_str = f"{DIM}N/A{RESET}"

            heartbeat_msg = (
                f"{DIM}[{now_str}]{RESET} 💓 "
                f"{GREEN}Pulse #{pulse_count}{RESET} | "
                f"{CYAN}{user_id}{RESET} | "
                f"Hub: {hub_status_str} | "
                f"Streams: {YELLOW}{active_streams}{RESET} | "
                f"Library: {WHITE}{library_gb}GB ({mounted_count}){RESET} | "
                f"Data: {CYAN}{streamed_str}{RESET} | "
                f"Uptime: {MAGENTA}{uptime_min}m{RESET}"
            )
            print(heartbeat_msg)
            sys.stdout.flush()

            # Heartbeat pulse interval (50 seconds: prevents 20m timeout)
            for _ in range(50):
                if tunnel_proc is not None and tunnel_proc.poll() is not None:
                    break
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception:
            if tunnel_proc is not None and tunnel_proc.poll() is not None:
                break
            time.sleep(5)


def main():
    # 0. Parse CLI arguments
    args = parse_args()
    user_id = args.user
    hub_url = args.hub
    port = args.port

    # Register OS signal traps
    signal.signal(signal.SIGINT, cleanup_and_exit)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, cleanup_and_exit)

    print(f"\n{BOLD}{CYAN}=== Step 2: Google Cloud Shell Launcher (CloudStream Bridge) ==={RESET}")
    print(f"{CYAN}[*] Unique Device Username: {BOLD}{user_id}{RESET} (Zero Google Sign-In)")
    print(f"{CYAN}[*] Central Hub URL:         {BOLD}{hub_url}{RESET}")

    # 1. Setup persistent storage
    mount_count = setup_persistent_storage()

    # 2. Check and install dependencies
    ensure_python_dependencies()

    # 3. Locate or download cloudflared
    cloudflared_bin = ensure_cloudflared_binary()

    # 4. Kill old processes
    kill_old_processes()

    # 5. Launch FastAPI server
    launch_server(port=port)

    # 6. Launch Cloudflare tunnel
    launch_tunnel(cloudflared_bin, port=port)

    # 7. Extract public tunnel URL
    tunnel_url = extract_tunnel_url(timeout_secs=45)

    if not tunnel_url:
        print(f"\n{YELLOW}[!] Warning: Could not automatically parse trycloudflare URL within 45s.{RESET}")
        print(f"{DIM}    Check {TUNNEL_LOG} for details.{RESET}")
        tunnel_url = f"http://localhost:{port}"

    # Guard: Ensure it NEVER registers http://localhost or http://127.0.0.1 as a public tunnel URL.
    # If the tunnel URL is localhost or does not contain trycloudflare.com, it must NOT register
    # and must log an error that the tunnel failed to establish.
    if is_valid_tunnel_url(tunnel_url):
        register_with_hub(hub_url, user_id, tunnel_url, timeout=15.0, verbose=True)
    else:
        print(f"\n{RED}[!] Error: Cloudflare tunnel failed to establish. Local fallback URL '{tunnel_url}' will NOT be registered with Central Hub!{RESET}")

    # 8. Display ASCII QR Code & ANSI Banner
    if is_valid_tunnel_url(tunnel_url):
        print_ascii_qr(tunnel_url)
    print_banner(tunnel_url, mount_count, user_id=user_id, hub_url=hub_url)

    # 9. Enter anti-idle heartbeat loop
    run_heartbeat_loop(hub_url=hub_url, user_id=user_id, tunnel_url=tunnel_url, port=port)

    cleanup_and_exit()


if __name__ == "__main__":
    main()
