# Archive: Google Cloud Shell & Render Hub Architecture

This directory archives the legacy infrastructure components used during the **Google Cloud Shell (GCS)** and **Render Central Hub** deployment era of CloudStream WebDAV Bridge.

---

## 1. Overview of Archived Files

| Archived Path | Original Path | Purpose / Description |
| :--- | :--- | :--- |
| `cloud_shell_runner.py` | `cloud_shell_runner.py` | GCS daemon managing WebDAV service, quick/named Cloudflare tunnels, SIGHUP masking, and Central Hub heartbeat loop. |
| `cloud_shell_init.sh` | `cloud_shell_init.sh` | Shell bootstrap script orchestrating detached `tmux` sessions, pip wheel caching, and environment provisioning. |
| `scripts/gcs_anti_idle_bookmarklet.js` | `scripts/gcs_anti_idle_bookmarklet.js` | Browser bookmarklet defeating GCS 20-minute inactivity timer via silent Web Audio API and synthetic xterm.js keystroke pulses. |
| `scripts/gcs-anti-idle.user.js` | `scripts/gcs-anti-idle.user.js` | Tampermonkey userscript variant of the GCS anti-idle engine with high-contrast OSD status pill. |
| `tests/test_cloud_shell_runner.py` | `tests/test_cloud_shell_runner.py` | Pytest test suite validating Cloud Shell runner lifecycle, named tunnels, heartbeat payloads, and process management. |

---

## 2. Why We Transitioned Away from Cloud Shell & Render

While Google Cloud Shell and Render provided a zero-cost compute foundation during early development, they introduced operational friction and failure modes:

1. **Aggressive Inactivity Watchdogs**:
   - Google Cloud Shell strictly enforces a **20-minute client WebSocket inactivity timer**. If no keyboard or mouse events are registered within the active browser session, the VM is stopped.
   - Defeating this required running browser tabs with synthetic xterm.js DOM pulse injectors and silent Web Audio oscillators.
2. **Ephemeral Containers & Hard Resets**:
   - Google Cloud Shell enforces a **12-hour maximum continuous runtime**. After 12 hours, the container is forcibly recycled, terminating running tunnels and resetting ephemeral files.
3. **Dynamic / Fragile Tunnel Hostnames**:
   - Ephemeral Cloudflare Quick Tunnels generated random hostnames (`*.trycloudflare.com`) on every container boot, requiring dynamic discovery mechanisms and registration loops.
4. **Render Free Tier Cold Starts**:
   - Central Hub deployed on Render free tier suffered from 50+ second spin-down delays, leading to timeout errors when players probed WebDAV roots.
5. **Architectural Overhead**:
   - Maintaining heartbeats, active liveness probes, tunnel self-healing monitors, and client companion pairing created unnecessary systemic complexity.

---

## 3. The Modern Replacement: Permanent Serverless Edge Architecture

The entire Cloud Shell + Render backend has been superseded by a permanent, serverless edge deployment on **Cloudflare Workers**:

- **Edge Domain**: `https://cloudstream-dav-bridge.sahil-cloudstream.workers.dev`
- **100% Serverless & Zero Idle Timeouts**:
  - Runs natively on Cloudflare's global Anycast edge network.
  - Zero virtual machines to maintain, zero browser anti-idle scripts required, zero 12-hour session resets, and zero cold-start latency.
- **Cloudflare KV Persistence (`MOUNTS_KV`, `USER_REGISTRY`)**:
  - WebDAV mount configurations and device profiles are persisted globally in sub-millisecond Cloudflare KV.
- **Direct HTTP 302 Found Stream Redirection**:
  - `GET` and `HEAD` video stream requests bypass the edge worker entirely via direct `302 Found` redirects to upstream CDN storage.
  - Video bytes stream at wire speed (100–300+ Mbps) directly between the client (CX File Explorer / Android TV) and origin CDNs with millisecond timeline seeking (`206 Partial Content`).
  - Zero video bandwidth traverses the worker, complying with free edge quotas.
- **Two-Tier Stream Probing Shield**:
  - Tier 1: Client residential OkHttp Range Prober (`Range: bytes=0-8191`) for container metadata inspection without datacenter IP blocks.
  - Tier 2: Edge synthetic size fallback (100 GiB) preventing media player 0-byte demuxer traps.
- **Permanent "Set Once & Forget" WebDAV Host**:
  - CX File Explorer and media clients configure `cloudstream-dav-bridge.sahil-cloudstream.workers.dev` once. The URL is permanent and never rotates.

---

## 4. Current Repository Status

The active production codebase resides in:
- `cloudflare-worker/`: Serverless edge worker (`src/index.ts`, `wrangler.jsonc`, `deploy_worker.bat`).
- `cloud-stream-bridge-android`: Native Android companion app with direct Cloudflare KV synchronization.
- `central_hub.py`, `webdav_engine.py`: Retained for optional local LAN bridge hosting.
