---
title: CloudStream WebDAV Bridge
emoji: 🎬
colorFrom: indigo
colorTo: cyan
sdk: docker
app_port: 7860
pinned: false
---

# CloudStream WebDAV Bridge (High-Velocity Cloud-to-Cloud Data Bridge)
Bypass home internet bottlenecks and stream massive 50 GB to 100 GB movie files (4K UHD Remuxes / HDR) with 0 bytes local storage.

### Features
- **Datacenter Speed**: Pulls upstream links at multi-gigabit speeds directly from the cloud.
- **On-Demand Range Proxy (RFC 7233)**: Fetches only the exact 10 MB - 20 MB block requested when scrubbing.
- **RFC 4918 WebDAV Server**: Native mounting in CX File Explorer, Nova Player, VLC, and Kodi.
- **Instant Magic Bytes Probe (<100ms)**: Validates MKV/MP4 container headers and HTTP Range capabilities.

---

### Google Cloud Shell Deployment (1-Click Persistent Runner)

Run the following command directly in [Google Cloud Shell](https://shell.cloud.google.com/):

```bash
curl -sSL https://raw.githubusercontent.com/a48sahilrahman-create/cloud-stream-bridge/main/cloud_shell_init.sh | bash
```

#### What this does:
1. Clones/updates the repository into `$HOME/cloud-stream-bridge`.
2. Persists all mounted streams in `$HOME/.cloudstream-bridge/mounts.json` across container restarts.
3. Automatically sets up dependencies and downloads the high-speed `cloudflared` tunnel binary.
4. Generates an ASCII QR Code and an ANSI connection dashboard for CX File Explorer & Android TV.
5. Keeps the Google Cloud Shell session active with a 12-hour anti-idle telemetry heartbeat loop.

