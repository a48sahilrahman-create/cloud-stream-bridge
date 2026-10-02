# CloudStream WebDAV Bridge - Colab Runner
import os, sys, time, subprocess, re, json, urllib.request

print("="*70)
print("🚀 CLOUDSTREAM WEBDAV BRIDGE - GOOGLE CLUSTER INITIALIZATION")
print("="*70)

print("[1/5] Installing cloudflared & Python packages...")
os.system("pip install -q fastapi 'uvicorn[standard]' httpx pydantic aiofiles python-multipart")

if not os.path.exists("/usr/local/bin/cloudflared") and not os.path.exists("/usr/bin/cloudflared"):
    os.system("wget -q -nc https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb")
    os.system("dpkg -i cloudflared-linux-amd64.deb > /dev/null 2>&1")

print("[2/5] Cleaning background processes...")
os.system("pkill -9 -f uvicorn > /dev/null 2>&1")
os.system("pkill -9 -f cloudflared > /dev/null 2>&1")
time.sleep(1)

print("[3/5] Starting WebDAV streaming engine on port 7860...")
if os.path.exists("server.log"):
    try:
        os.remove("server.log")
    except Exception:
        pass

# Prefer main:app if templates exist, else standalone_server:app
app_module = "main:app" if os.path.exists("main.py") and os.path.exists("templates") else "standalone_server:app"
os.system(f"nohup python3 -m uvicorn {app_module} --host 0.0.0.0 --port 7860 > server.log 2>&1 &")

# Health check
server_ready = False
for _ in range(15):
    time.sleep(1)
    try:
        with urllib.request.urlopen("http://localhost:7860/api/status", timeout=2) as r:
            if r.status == 200:
                server_ready = True
                break
    except Exception:
        pass

if not server_ready:
    print("[ERROR] FastAPI server failed to start! Checking server.log:")
    if os.path.exists("server.log"):
        with open("server.log", "r", encoding="utf-8", errors="ignore") as f:
            print(f.read()[:1000])
    sys.exit(1)

print("[✓] Streaming engine online and listening on http://localhost:7860")

# Pre-mount default test movie (Bethlehem 4K MKV)
test_stream_url = "https://87bc29edda9ac0f5591377cf744d9a37.r2.cloudflarestorage.com/hub/b164351e985feafe3e21fb194fa8e058?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=c4a314ca3f1102bcd664fe54764a50f6%2F20261002%2Fauto%2Fs3%2Faws4_request&X-Amz-Date=20261002T151612Z&X-Amz-Expires=28800&X-Amz-SignedHeaders=host&response-content-disposition=attachment%3B%20filename%3D%22Bethlehem.Kudumba.Unit.2026.4K-2160p.SDR.JHS.WEB-DL.Hindi-Multi.DDP5.1.HEVC.x265-HDHub4u.Ms.mkv%22&X-Amz-Signature=8336cfda366979c15d0fd4ac24bec14d0ce635920fe51483a5a71d49a0e6ab73"
try:
    req = urllib.request.Request(
        "http://localhost:7860/api/mount",
        data=json.dumps({"url": test_stream_url}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as res:
        print("[✓] Pre-mounted: Bethlehem.Kudumba.Unit.2026.4K-2160p.SDR (14.16 GB)")
except Exception as e:
    print(f"[*] Pre-mount info: {e}")

print("[4/5] Launching high-speed Cloudflare Tunnel...")
if os.path.exists("tunnel.log"):
    try:
        os.remove("tunnel.log")
    except Exception:
        pass

os.system("nohup cloudflared tunnel --url http://localhost:7860 > tunnel.log 2>&1 &")

print("[5/5] Extracting secure public HTTPS URL...")
public_url = None
for _ in range(40):
    time.sleep(1)
    if os.path.exists("tunnel.log"):
        try:
            with open("tunnel.log", "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", content)
            if m:
                public_url = m.group(0)
                break
        except Exception:
            pass

if not public_url:
    print("[ERROR] Cloudflare tunnel did not provide a public URL. Tunnel logs:")
    if os.path.exists("tunnel.log"):
        with open("tunnel.log", "r", encoding="utf-8", errors="ignore") as f:
            print(f.read()[:1000])
    sys.exit(1)

host = public_url.replace("https://", "").replace("http://", "").strip("/")

print("\n" + "="*70)
print("🎬 CLOUDSTREAM WEBDAV BRIDGE IS ONLINE (10 Gbps Cloud Pipe)")
print("="*70)
print(f"🌐 Web UI:       {public_url}")
print(f"📁 WebDAV URL:   {public_url}/dav/")
print("\n📱 CX FILE EXPLORER CONFIGURATION:")
print(f"   Protocol:      WebDAV")
print(f"   Server / Host: {host}")
print(f"   Path:          /dav")
print(f"   Port:          443")
print(f"   HTTPS:         CHECKED (ON)")
print(f"   Username:      admin")
print(f"   Password:      (leave blank)")
print("="*70)
print("Ready to stream! Leave this cell running while watching.\n")

try:
    while True:
        time.sleep(60)
except KeyboardInterrupt:
    print("\n[*] Shutting down bridge gracefully...")
    os.system("pkill -9 -f uvicorn > /dev/null 2>&1")
    os.system("pkill -9 -f cloudflared > /dev/null 2>&1")
