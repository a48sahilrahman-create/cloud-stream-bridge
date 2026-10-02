import base64

with open(r"C:\Users\sahil\workspaces\cloud-stream-bridge\standalone_server.py", "rb") as f:
    raw_bytes = f.read()

b64_str = base64.b64encode(raw_bytes).decode("ascii")

colab_code = f'''# CloudStream WebDAV Bridge - 10 Gbps Cloud Runner (Rock-Solid Persistent)
import os, sys, time, subprocess, base64, re

print("[1/5] Installing cloudflared & Python dependencies...")
os.system("pip install -q fastapi 'uvicorn[standard]' httpx pydantic aiofiles")
if not os.path.exists("/usr/local/bin/cloudflared"):
    os.system("wget -q -nc https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb")
    os.system("dpkg -i cloudflared-linux-amd64.deb > /dev/null 2>&1")

print("[2/5] Writing standalone server with embedded dark-mode Web UI...")
SERVER_B64 = "{b64_str}"
with open("standalone_server.py", "wb") as f:
    f.write(base64.b64decode(SERVER_B64))

print("[3/5] Starting WebDAV server on port 7860...")
os.system("pkill -9 -f uvicorn > /dev/null 2>&1")
os.system("pkill -9 -f cloudflared > /dev/null 2>&1")
time.sleep(1)

# Start uvicorn in background
os.system("nohup python3 -m uvicorn standalone_server:app --host 0.0.0.0 --port 7860 > server.log 2>&1 &")
time.sleep(3)

test_stream_url = "https://87bc29edda9ac0f5591377cf744d9a37.r2.cloudflarestorage.com/hub/b164351e985feafe3e21fb194fa8e058?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=c4a314ca3f1102bcd664fe54764a50f6%2F20261002%2Fauto%2Fs3%2Faws4_request&X-Amz-Date=20261002T151612Z&X-Amz-Expires=28800&X-Amz-SignedHeaders=host&response-content-disposition=attachment%3B%20filename%3D%22Bethlehem.Kudumba.Unit.2026.4K-2160p.SDR.JHS.WEB-DL.Hindi-Multi.DDP5.1.HEVC.x265-HDHub4u.Ms.mkv%22&X-Amz-Signature=8336cfda366979c15d0fd4ac24bec14d0ce635920fe51483a5a71d49a0e6ab73"
try:
    import urllib.request, json
    req = urllib.request.Request(
        "http://localhost:7860/api/mount",
        data=json.dumps({{"url": test_stream_url}}).encode("utf-8"),
        headers={{"Content-Type": "application/json"}}
    )
    with urllib.request.urlopen(req, timeout=15) as res:
        print("[*] Successfully pre-mounted Bethlehem 4K MKV into WebDAV library!")
except Exception as e:
    print(f"[*] Pre-mount notice: {{e}}")

print("[4/5] Launching Cloudflare 10 Gbps Tunnel in background...")
if os.path.exists("tunnel.log"):
    os.remove("tunnel.log")
os.system("nohup cloudflared tunnel --url http://localhost:7860 > tunnel.log 2>&1 &")

print("[5/5] Extracting public high-speed tunnel URL...")
public_url = None
for attempt in range(40):
    time.sleep(1)
    if os.path.exists("tunnel.log"):
        try:
            with open("tunnel.log", "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            m = re.search(r"https://[a-zA-Z0-9-]+\\.trycloudflare\\.com", content)
            if m:
                public_url = m.group(0)
                break
        except Exception:
            pass

if public_url:
    host = public_url.replace("https://", "").replace("http://", "").strip("/")
    print("\\n" + "="*70)
    print("🎬 CLOUDSTREAM WEBDAV BRIDGE IS ONLINE (10 Gbps Cloud Pipe)")
    print("="*70)
    print(f"🌐 Full Web UI: {{public_url}}")
    print(f"📁 WebDAV URL:   {{public_url}}/dav/")
    print("\\n📱 CX FILE EXPLORER CONFIGURATION:")
    print(f"   Protocol:      WebDAV")
    print(f"   Server / Host: {{host}}")
    print(f"   Path:          /dav")
    print(f"   Port:          443")
    print(f"   HTTPS:         CHECKED (ON)")
    print(f"   Username:      admin")
    print(f"   Password:      (leave blank)")
    print("="*70)
    print("Ready to stream! Leave this cell running while watching.\\n")
    # Keep cell active forever without crashing
    while True:
        time.sleep(60)
else:
    print("[ERROR] Could not acquire Cloudflare Tunnel URL. Tunnel log contents:")
    if os.path.exists("tunnel.log"):
        with open("tunnel.log", "r", encoding="utf-8", errors="ignore") as f:
            print(f.read()[:1000])
'''

with open(r"C:\Users\sahil\colab_cell_code.py", "w", encoding="utf-8") as f:
    f.write(colab_code)

print("Generated colab_cell_code.py successfully! Total chars:", len(colab_code))
