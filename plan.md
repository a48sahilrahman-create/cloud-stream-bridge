# CloudStream WebDAV Bridge: Probe Shield & Live Readiness Engine Plan

## Project Directory
**Path**: `C:\Users\sahil\workspaces\cloud-stream-bridge\`

---

## 1. System Architecture & Context

### Production Infrastructure Stack
* **Cloud Engine (Step 2)**: Ephemeral Linux container on **Google Cloud Shell** (`cloud_shell_runner.py`) running FastAPI/Uvicorn on port `7860`, managed by a 50-second anti-idle heartbeat loop and an unauthenticated `cloudflared` quick tunnel (`https://*.trycloudflare.com`).
* **Permanent Edge Router**: **Cloudflare Worker** at `https://cloudstream-dav-bridge.sahil-cloudstream.workers.dev` (`cloudflare-worker/src/index.ts`). It bridges CX File Explorer to the dynamic Cloud Shell quick tunnel with 0 video bytes proxied on the edge (reverse-proxies WebDAV XML metadata, returns `HTTP 302 Found` for `GET`/`HEAD`).
* **Client Streaming Mount**: **CX File Explorer** (Android / Android TV) mounted to `https://cloudstream-dav-bridge.sahil-cloudstream.workers.dev/dav/<device_id>/` with `Anonymous [X]` auth.

### Recently Resolved Defects
1. **4K UHD Remux Playback Stuttering & Buffering**:
   * *Root Cause*: High-bitrate 50–100 GB 4K video data (60–90+ Mbps) was being choked through the free Cloudflare Quick Tunnel (`*.trycloudflare.com`), which throttles TCP windows.
   * *Resolution*: Enabled direct `HTTP 302 Found` redirects to the upstream high-speed CDN in `webdav_engine.py`. Media players (ExoPlayer/VLC) now pull video directly from the CDN at full line speed.
2. **Background Data Drain on Stop/Scrub**:
   * *Root Cause*: `httpx.AsyncClient.stream()` kept draining upstream TCP packets into memory buffers after the client closed the connection.
   * *Resolution*: Hardened `range_proxy.py` with an explicit `await resp.aclose()` inside a strict `finally` block, cutting upstream connections in `<10ms` upon client disconnect.

---

## 2. The Problem: "Loading Time Difference & CX File Explorer Freeze"

### The Time Difference (Why It Takes 3 to 8 Seconds)
When a user pastes a stream link into the system, the server executes an asynchronous pre-flight inspection in `stream_probe.py`:
1. **Multi-Hop Redirect Traversal (1–3s)**: Follows 2 to 4 redirects from debrid/presigned storage layers to the origin edge.
2. **Cold CDN Handshake & Magic Byte Inspection (2–5s)**: Sends a `Range: bytes=0-8191` request to inspect container magic bytes (EBML header `\x1a\x45\xdf\xa3` for MKV, `ftyp`/`moov` for MP4) and extracts `Content-Range` or `Content-Length`.
3. **Virtual Mount Persistence (1–2s)**: Saves metadata to `$HOME/.cloudstream-bridge/mounts.json`.

### Why CX File Explorer Freezes / Struggles During This Window
If the user opens CX File Explorer before the probe completes:
1. **The 0-Byte Demuxer Trap**: The stream's `total_bytes` is temporarily `0`. When CX File Explorer starts playback (`Range: bytes=0-`), `parse_byte_range()` calculates `max(0, total_size - 1)` which evaluates to `0`. The proxy sends **only 1 byte** (`bytes=0-0`) and terminates the stream.
2. **EOF Suffix Seek Collapse**: Players seek the last 1 MB (`Range: bytes=-1048576`) to read MKV cues / MP4 `moov` atoms. With `total_size == 0`, the seek collapses to byte 0.
3. **Synchronous Player Hang**: The player demuxer hangs in an infinite retry loop attempting to detect audio/video codecs from a 1-byte stream, freezing the CX File Explorer UI.
4. **Why it's smooth once loaded**: Once the probe locks the actual size (e.g., 64.2 GB), CX File Explorer receives complete metadata and an immediate 302 redirect to the CDN.

---

## 3. Implementation Plan

### Phase 1: WebDAV "Probe Shield" (`webdav_engine.py`)
Prevent CX File Explorer from ever receiving 0-byte corrupt metadata while a stream is actively probing.
1. **In-Flight Probe Registry**: Maintain an active probe lookup `probing_tasks: Dict[str, asyncio.Event]` in `webdav_engine.py` or `MountManager`.
2. **Asynchronous Request Holding**:
   * When CX File Explorer issues `PROPFIND`, `HEAD`, or `GET` for a file that is currently probing:
   * Await the probe completion event with a 4-second timeout: `await asyncio.wait_for(probe_event.wait(), timeout=4.0)`.
   * If the probe completes within the window, serve the full, verified file metadata.
   * If the timeout expires or probe is still active, return `HTTP 503 Service Unavailable` with `Retry-After: 2` and a WebDAV XML status message ("Stream probing in progress, retrying in 2 seconds...").
3. **0-Byte Guard in `stream_range_proxy`**:
   * If `total_size <= 0`, reject the request with `HTTP 412 Precondition Failed` or `HTTP 503` rather than clamping to `0-0` (1 single byte).

### Phase 2: Multi-Stage Real-Time Readiness Tracker (`stream_probe.py` & `main.py`)
1. **Granular Probe Progress Lifecycle**:
   * `STAGE 1 (25%)`: Resolving upstream CDN redirects & SSL handshake.
   * `STAGE 2 (60%)`: Inspecting 4K Remux container & EBML/MP4 magic bytes.
   * `STAGE 3 (85%)`: Verifying byte-range seek capability & Content-Length.
   * `STAGE 4 (100%)`: Stream locked & verified ready for WebDAV playback.
2. **Telemetry API Updates**:
   * Include `probe_stage` and `probe_status` (`"probing" | "ready" | "failed"`) in `/api/mounts` and `/api/status`.

### Phase 3: Web UI Visual Readiness & Progress Dashboard (`templates/index.html`)
Replace the ambiguous "Probing <100ms..." label with a clear, multi-stage status experience:
1. **Animated Multi-Stage Progress Meter**:
   * Visual progress bar displaying percentage and active stage description.
2. **Prominent 100% Ready Success Card**:
   * Displays when probe hits 100%:
     `✓ READY FOR CX FILE EXPLORER: [Filename] (64.2 GB) locked in 2.4s.`
     `It is now safe to open CX File Explorer and stream smoothly.`
3. **Stream Card Status Pills**:
   * `🟡 PROBING` (warning banner: "Please wait 3-5s before opening in CX File Explorer")
   * `🟢 STREAM READY` (green badge: "Safe to play in CX File Explorer / VLC")

---

## 4. Verification & Testing Checklist

- [x] **Probe Shield Implementation**: Guarded `webdav_engine.py` with `asyncio.Event` and `wait_for(timeout=4.0)` to hold early incoming requests and emit HTTP 503 `Retry-After: 2` fallback.
- [x] **Range Proxy Guard**: Guarded `range_proxy.py` from 0-byte degenerate requests.
- [x] **Web UI Live Polling**: Multi-stage visual readiness tracker in `templates/index.html` with real-time `/api/probe/status/{probe_id}` polling.
- [x] **Stateless Cloud Persistence**: Created `library_vault.py` with GitHub Gist/JSON sync and multi-select batch-mounting.
- [x] **CX File Explorer Button Emoji Fix**: Patched UTF-16 surrogate bytes `📋` in smali bytecode, recompiled, aligned, and signed with Android SDK 36.
- [ ] **Physical On-Device Verification**: Connect physical Android phone via USB to verify 1-click clipboard paste and instant playback.

---

## 5. Copy-Paste Prompt for New Chat

Copy and paste the block below into your new chat to instantly continue:

```markdown
We are working on the CloudStream WebDAV Bridge project located at:
`C:\Users\sahil\workspaces\cloud-stream-bridge\`

Context and recent fixes:
1. Architecture: Google Cloud Shell runner (port 7860 + cloudflared tunnel) + Cloudflare Worker permanent edge router (`cloudstream-dav-bridge.sahil-cloudstream.workers.dev`) + CX File Explorer WebDAV client.
2. 4K streaming was made buttery smooth by using direct 302 redirects to upstream CDNs, and background data leakage on stop/scrub was killed with an instant `<10ms` `await resp.aclose()` in `range_proxy.py`.
3. Read `plan.md` in `C:\Users\sahil\workspaces\cloud-stream-bridge\plan.md` for full details.

Current Task:
Implement the Probe Shield and Live Readiness Engine as outlined in `plan.md`:
1. Add the "Probe Shield" in `webdav_engine.py` (asynchronously hold WebDAV queries during the 3-5s probe window or return 503 Retry-After so CX File Explorer never receives a 0-byte corrupt stream).
2. Add the Multi-Stage Readiness Tracker in `stream_probe.py` and `main.py` (tracking stages from 0% to 100%).
3. Upgrade `templates/index.html` with a visual multi-stage progress meter and clear `🟡 PROBING` vs `🟢 READY FOR CX FILE EXPLORER` status indicators so the user knows exactly when the cloud is ready to play smoothly.
```
