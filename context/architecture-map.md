# Architecture & Directory Map — CloudStream WebDAV Bridge

> **Executive Overview**: CloudStream WebDAV Bridge acts as a high-speed, zero-local-storage cloud-to-cloud proxy. It bridges remote multi-gigabit direct download links (Cloudflare R2, AWS S3, debrid, direct CDN streams) to local media clients (CX File Explorer, VLC, Nova Video Player, Kodi) over RFC 4918 WebDAV and RFC 7233 HTTP Range protocols.

---

## 1. System Topology & Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    UPSTREAM CLOUD DATA SOURCES                              │
│       Multi-Gigabit Cloud Storage (Cloudflare R2 / AWS S3 / CDNs)           │
│       50 GB - 100 GB 4K UHD Remux MKV / MP4 Containers                     │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ Multi-Gigabit HTTP Range (RFC 7233)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│               CLOUDSTREAM WEBDAV BRIDGE (CLOUD / VPS / COLAB)               │
│                                                                             │
│  ┌──────────────────────┐  Sub-100ms   ┌─────────────────────────────────┐  │
│  │   stream_probe.py    │ ───────────► │ Container Magic Bytes & Ranges  │  │
│  │  (EBML / ISOBMFF)    │              │ (MKV/MP4, Total Size, Accept)   │  │
│  └──────────────────────┘              └────────────────┬────────────────┘  │
│                                                         │                   │
│  ┌──────────────────────┐  Mount Link  ┌────────────────▼────────────────┐  │
│  │   webdav_engine.py   │ ◄─────────── │ mount_manager (In-Memory / DB)  │  │
│  │ (RFC 4918 Multistatus│              │ Dynamic Virtual Filesystem Map  │  │
│  └──────────┬───────────┘              └─────────────────────────────────┘  │
│             │                                                               │
│             │ On-Demand Chunk Stream                                        │
│  ┌──────────▼───────────┐  <10ms Seek  ┌─────────────────────────────────┐  │
│  │    range_proxy.py    │ ───────────► │ Disconnect Trap & Ring Buffer   │  │
│  │ (Zero-Copy Pipe)     │              │ (50 MB RAM Cache, 0 Bytes Disk) │  │
│  └──────────┬───────────┘              └─────────────────────────────────┘  │
└─────────────┼───────────────────────────────────────────────────────────────┘
              │ Local Wi-Fi (Only 10-20 MB On-Demand Scrubbing Chunks)
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       LOCAL CLIENT DEVICES & PLAYERS                        │
│   - CX File Explorer (Android TV / Fire TV) via WebDAV Mount                │
│   - VLC Media Player / Kodi / Nova Video Player via Direct Stream           │
│   - Zero Local Disk Space Consumed: RAM Ring Buffer Playback Only           │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Core Architectural Subsystems

### Subsystem 1: Container & Range Probe Engine (`stream_probe.py`)
- **Latency Target**: Sub-100ms async probe execution.
- **Probe Request**: Issues `Range: bytes=0-8191` against the target upstream URL with `httpx.AsyncClient`.
- **Magic Bytes Validation**:
  - MKV (Matroska): Matches EBML header `\x1a\x45\xdf\xa3`.
  - MP4: Matches ISOBMFF major/compatible brands (`ftyp`, `moov`, `mdat`).
  - Fallbacks: TS, AVI, MOV, WebM.
- **Header Parsing**: Extracts exact byte length from `Content-Range: bytes 0-8191/<total>` or `Content-Length`.
- **Presigned Query Decoding**: Extracts authentic movie titles from `response-content-disposition` parameters in presigned Cloudflare R2 / AWS S3 URLs.

### Subsystem 2: RFC 4918 Virtual WebDAV Engine (`webdav_engine.py`)
- **Virtual Root & Subpaths**: Exposes `/` and `/dav/` as virtual directory structures without backing disk files.
- **RFC 4918 Compliance**:
  - `OPTIONS`: Returns `DAV: 1` discovery headers and allowed verbs (`PROPFIND, OPTIONS, HEAD, GET`).
  - `PROPFIND` (Depth: 0 / Depth: 1): Dynamically generates RFC 4918 `<D:multistatus>` XML payloads with `<D:getetag>`, `<D:creationdate>` (ISO 8601), `<D:getlastmodified>` (RFC 1123), and exact file sizes.
  - `HEAD`: Returns `Accept-Ranges: bytes`, `Content-Length`, and correct MIME content-types.
  - `GET`: Routes byte range requests directly to the Range Proxy Engine.

### Subsystem 3: RFC 7233 Range Proxy & Disconnect Trap (`range_proxy.py`)
- **Transparent Range Conversion**: Translates incoming client HTTP byte ranges (`bytes=start-end`, `bytes=start-`, `bytes=-suffix`) into upstream chunk requests.
- **Zero-Copy Async Streaming**: Emits 128 KB buffer chunks directly via `httpx.AsyncClient.stream()` into FastAPI's `StreamingResponse`.
- **Immediate Disconnect Trap**: When a user scrubs backwards or forwards in VLC/CX File Explorer, the client immediately drops the TCP socket. The proxy traps the disconnect within 10ms and cancels the upstream stream, preventing wasted cloud egress.
- **Sliding RAM Cache**: In-memory 50 MB ring buffer caches recent chunks for instantaneous backwards micro-seeks.

### Subsystem 4: Multi-Surface Deployment Engine
- **Local / Docker**: Exposed via FastAPI on port `7860` (`Dockerfile`).
- **Cloudflare Tunnel (`start_tunnel.ps1`)**: Zero-signup cloudflared quick tunnel exposing local/cloud bridge to public HTTPS.
- **Google Colab (`colab_run.py`, `run_colab_cell.ps1`)**: Runs headless in Colab with 12 GB+ RAM and multi-gigabit datacenter pipe.
- **Hugging Face Spaces (`hf_zero_touch_deployer.py`)**: One-click Git push to Hugging Face Docker Space.
- **Autonomous Standalone (`standalone_server.py`)**: Single-file bundle containing the entire engine, REST routes, and embedded HTML UI for zero-dependency execution.

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
├── main.py                      # FastAPI application entry & root dispatcher
├── webdav_engine.py             # RFC 4918 WebDAV virtual directory engine
├── range_proxy.py               # RFC 7233 Range streaming & disconnect trap
├── stream_probe.py              # Sub-100ms container magic bytes & size probe
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
