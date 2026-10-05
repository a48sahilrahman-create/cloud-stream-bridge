# Architecture & Directory Map — CloudStream WebDAV Bridge

> **Executive Overview**: CloudStream WebDAV Bridge acts as a high-speed, zero-local-storage cloud-to-cloud proxy. It bridges remote multi-gigabit direct download links (Cloudflare R2, AWS S3, debrid, direct CDN streams) to local media clients (CX File Explorer, VLC, Nova Video Player, Kodi) over RFC 4918 WebDAV and RFC 7233 HTTP Range protocols. In production, it operates as a **Standalone Serverless Cloudflare Worker Edge WebDAV Engine**, completely eliminating legacy dependencies on Google Cloud Shell VMs and Render centralized hubs.

---

## 1. System Topology & Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    UPSTREAM CLOUD DATA SOURCES                              │
│       Multi-Gigabit Cloud Storage (Cloudflare R2 / AWS S3 / CDNs)           │
│       50 GB - 100 GB 4K UHD Remux MKV / MP4 Containers                     │
└──────────────────────┬───────────────────────────────▲──────────────────────┘
                       │                               │
                       │ Range: bytes=0-8191           │ HTTP 302 Direct Stream
                       │ (Metadata / Magic Bytes)      │ (0 Bytes Proxied)
                       │                               │
┌──────────────────────▼───────────────────────────────┴──────────────────────┐
│       ANDROID APP & CLIENT DEVICES (CloudStream TV/Phone / CX / VLC)        │
│                                                                             │
│  ┌─────────────────────────────────┐  Mount Stream  ┌────────────────────┐  │
│  │ Tier 1 Residential Probe Shield │ ─────────────► │ Client Media Player│  │
│  │ (Range: 0-8191 from Client IP)  │                │ (CX / VLC / Kodi)  │  │
│  └─────────────────────────────────┘                └─────────▲──────────┘  │
└──────────────────────┬────────────────────────────────────────┼─────────────┘
                       │ POST /api/mount/:userId                │ RFC 4918
                       │ (URL, Filename, Size, Headers)         │ WebDAV
                       ▼                                        │ /dav/:userId/
┌───────────────────────────────────────────────────────────────┴─────────────┐
│          STANDALONE CLOUDFLARE WORKER SERVERLESS EDGE WEBDAV ENGINE         │
│          (`cloudstream-dav-bridge.<subdomain>.workers.dev`)                 │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ Edge REST API Router (`index.ts`)                                     │  │
│  │ - POST /api/mount/:userId                                             │  │
│  │ - GET  /api/mounts/:userId                                            │  │
│  │ - DELETE /api/mounts/:userId/:filename                                │  │
│  │ - POST /api/unmount-all/:userId | GET /health                         │  │
│  └───────────────────────────────────┬───────────────────────────────────┘  │
│                                      │                                      │
│  ┌───────────────────────────────────▼───────────────────────────────────┐  │
│  │ RFC 4918 Standalone WebDAV Server Engine (`index.ts`)                 │  │
│  │ - OPTIONS: DAV: 1, 2 discovery & allowed verbs                        │  │
│  │ - PROPFIND: Dynamic 207 Multi-Status XML (Depth 0/1, exact byte size) │  │
│  │ - GET / HEAD: HTTP 302 Found redirect to direct upstream URL          │  │
│  │ - DELETE: Unmounts virtual file stream                                │  │
│  └───────────────────────────────────┬───────────────────────────────────┘  │
│                                      │                                      │
│  ┌───────────────────────────────────▼───────────────────────────────────┐  │
│  │ Edge Storage & In-Memory Isolate Cache Layer (`mount_manager.ts`)     │  │
│  │ - 15-Second In-Memory V8 Isolate Cache (Eliminates 90%+ KV reads)     │  │
│  │ - Cloudflare KV (`MOUNTS_KV`) with 24-Hour (86,400s) Expiration TTL   │  │
│  │ - StreamMount & UserMountsRecord schema persistence                   │  │
│  └───────────────────────────────────┬───────────────────────────────────┘  │
│                                      │                                      │
│  ┌───────────────────────────────────▼───────────────────────────────────┐  │
│  │ Tier 2 Edge Probe Fallback & Synthetic Floor Engine                   │  │
│  │ - Edge Range Probe (Range: 0-8191, 4s timeout)                        │  │
│  │ - 100 GiB Synthetic Floor Fallback (107,374,182,400 bytes)            │  │
│  │   Guarantees zero mount failure even if upstream blocks probes        │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Core Architectural Subsystems

### Subsystem 1: Standalone Serverless Edge WebDAV Engine (`cloudflare-worker`)
- **Zero Infrastructure Footprint**: Completely eliminates Google Cloud Shell and Render server dependencies. Runs at the Cloudflare Edge across 300+ global datacenters.
- **RFC 4918 Compliance**:
  - `OPTIONS /dav/:userId/`: Returns `DAV: 1, 2`, `MS-Author-Via: DAV`, and allowed verbs (`OPTIONS, GET, HEAD, PROPFIND, DELETE, PROPPATCH, MKCOL`).
  - `PROPFIND /dav/:userId/` and `/dav/:userId/:filename`: Constructs dynamic RFC 4918 `<D:multistatus>` 207 XML payloads with `<D:getcontentlength>`, `<D:getetag>`, ISO 8601 creation dates, and RFC 1123 modified dates.
  - `GET / HEAD /dav/:userId/:filename`: Returns `HTTP 302 Found` with `Location: <upstream_url>`, offloading video traffic directly to upstream CDNs (0 bytes video proxied through Worker).
  - `DELETE /dav/:userId/:filename`: Removes virtual stream mount from KV and isolate cache, returning `204 No Content`.
- **Edge REST Control API**:
  - `POST /api/mount/:userId`: Mounts a stream with title, URL, size, and custom headers.
  - `GET /api/mounts/:userId`: Returns current active mounts for a user.
  - `DELETE /api/mounts/:userId/:filename`: Unmounts a specific file.
  - `POST /api/unmount-all/:userId`: Clears all mounts for a user.
  - `GET /health` / `GET /`: Health check confirming edge engine status and KV binding.

### Subsystem 2: Dual-Layer Edge Mount Storage & Caching (`mount_manager.ts`)
- **Cloudflare KV (`MOUNTS_KV`)**: Backing persistent key-value store for user mount records (`mounts:{userId}`) with a 24-hour (`86,400` seconds) TTL.
- **15-Second In-Memory V8 Isolate Cache**: Maintains `isolateMountsCache` Map within the active V8 isolate. Drops KV read operations by over 90% during rapid client polling and multi-file directory scans.
- **Memory Fallback**: Includes `fallbackMemoryStore` allowing zero-configuration testing and operation in environments without KV bindings.

### Subsystem 3: Two-Tier Stream Probing Shield Architecture
- **Tier 1 (Residential IP Probe Shield in Android App)**:
  - Executes directly on the user's Android phone/TV before mounting.
  - Issues `Range: bytes=0-8191` using the client's residential IP, completely bypassing Cloudflare/anti-bot datacenter IP blocks that reject serverless workers.
  - Parses `Content-Range: bytes 0-8191/<total>` for exact container size and extracts filename from `Content-Disposition`.
- **Tier 2 (Edge Fallback Probe & 100 GiB Synthetic Floor)**:
  - If the Android client passes `size_bytes: 0` or omits size, Worker issues an edge probe (`Range: bytes=0-8191`, 4s timeout).
  - If upstream blocks the probe or times out, the Worker automatically applies a **100 GiB Synthetic Floor** (`107,374,182,400` bytes).
  - Ensures media players (CX File Explorer, VLC) never encounter 0-byte mount errors and can always start playback and seek freely.

### Subsystem 4: Container & Range Probe Engine (`stream_probe.py` - Local/Python Bridge)
- **Latency Target**: Sub-100ms async probe execution in Python environments.
- **Probe Request**: Issues `Range: bytes=0-8191` against the target upstream URL with `httpx.AsyncClient`.
- **Magic Bytes Validation**:
  - MKV (Matroska): Matches EBML header `\x1a\x45\xdf\xa3`.
  - MP4: Matches ISOBMFF major/compatible brands (`ftyp`, `moov`, `mdat`).
  - Fallbacks: TS, AVI, MOV, WebM.
- **Header Parsing**: Extracts exact byte length from `Content-Range: bytes 0-8191/<total>` or `Content-Length`.
- **Presigned Query Decoding**: Extracts authentic movie titles from `response-content-disposition` parameters in presigned Cloudflare R2 / AWS S3 URLs.

### Subsystem 5: RFC 7233 Range Proxy & Disconnect Trap (`range_proxy.py` - Local/Python Bridge)
- **Transparent Range Conversion**: Translates incoming client HTTP byte ranges (`bytes=start-end`, `bytes=start-`, `bytes=-suffix`) into upstream chunk requests.
- **Zero-Copy Async Streaming**: Emits 128 KB buffer chunks directly via `httpx.AsyncClient.stream()` into FastAPI's `StreamingResponse`.
- **Immediate Disconnect Trap**: When a user scrubs in VLC/CX File Explorer, the client immediately drops the TCP socket. The proxy traps the disconnect within 10ms and cancels the upstream stream.
- **Sliding RAM Cache**: In-memory 50 MB ring buffer caches recent chunks for backwards micro-seeks.

### Subsystem 6: Elimination of Legacy Cloud Shell & Render Architecture
- **Elimination of Google Cloud Shell & Render**:
  - The legacy architecture required maintaining interactive Google Cloud Shell sessions, background keep-alive scripts, and a Render-hosted central hub with ephemeral tunnels.
  - Migrated entirely to the serverless Cloudflare Worker edge WebDAV architecture, providing 24/7 high availability, zero cold starts, zero session timeouts, and permanent URLs.
- **Archival**:
  - Legacy Cloud Shell runners, orchestration scripts, and init automation (`cloud_shell_runner.py`, `cloud_shell_init.sh`, etc.) are archived under `archive/cloud_shell/`.

---

## 3. Directory Structure

```
cloud-stream-bridge/
├── context.md                   # <= 40-line master entry hub for AI agents
├── context/                     # Atomized modular domain specifications
│   ├── architecture-map.md      # This file: system topology & data flows
│   ├── invariants.md            # Hard invariants & safety guardrails
│   ├── file-responsibilities.md # File contracts & component ownership
│   ├── api-contracts.md         # WebDAV & REST protocol schemas
│   └── project-state.md         # Verified milestones & defect resolutions
├── cloudflare-worker/           # Production Serverless Edge WebDAV Engine
│   ├── src/
│   │   ├── index.ts             # Standalone WebDAV server & Edge REST router
│   │   └── mount_manager.ts     # KV store, isolate cache & mount records
│   ├── wrangler.jsonc           # Cloudflare Worker configuration & KV bindings
│   ├── package.json             # Worker dependencies and scripts
│   └── deploy_worker.bat        # Automated 1-click deployment script
├── archive/                     # Archived legacy architectures
│   └── cloud_shell/             # Legacy Cloud Shell VM runner & scripts
│       ├── cloud_shell_runner.py# Archived Cloud Shell daemon
│       ├── cloud_shell_init.sh  # Archived Cloud Shell initialization script
│       └── README.md            # Legacy archival documentation
├── main.py                      # FastAPI application entry & root dispatcher (Local/Dev)
├── webdav_engine.py             # RFC 4918 WebDAV virtual directory engine (Local/Dev)
├── range_proxy.py               # RFC 7233 Range streaming & disconnect trap (Local/Dev)
├── stream_probe.py              # Sub-100ms container magic bytes & size probe (Local/Dev)
├── standalone_server.py         # Self-contained single-file distribution
├── colab_run.py                 # Google Colab autonomous daemon
├── generate_colab_cell.py       # Colab executable notebook cell generator
├── hf_zero_touch_deployer.py    # Zero-touch Hugging Face Space deployer
├── start_tunnel.ps1             # Automated Cloudflare Tunnel launcher
├── run_colab_cell.ps1           # Windows UI automation Colab launcher
├── run_bridge.bat               # Windows 1-click batch launcher
├── templates/
│   └── index.html               # Responsive web UI & mount manager
├── tests/
│   ├── test_webdav.py           # 11-point canonical pytest suite
│   └── verify_cx.py             # 9-point CX File Explorer live verifier
├── Dockerfile                   # Python 3.11-slim container spec
├── requirements.txt             # Python runtime dependencies
├── .repomixignore               # Anti-pollution ingestion boundary
└── repomix.config.json          # Repomix AST compression & priority configuration
```
