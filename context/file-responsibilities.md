# Key File Responsibilities & Component Contracts — CloudStream WebDAV Bridge

> **Executive Overview**: Exhaustive component ownership map defining source code boundaries, imports, exports, and operational roles across the entire codebase.

---

## 1. Serverless Cloudflare Worker Edge Engine (`cloudflare-worker/`)

### `cloudflare-worker/src/index.ts` (Standalone WebDAV Server & Edge REST Router)
- **Role**: Primary edge entry point and router for the production serverless WebDAV bridge.
- **Key Responsibilities**:
  - Implements RFC 4918 Standalone WebDAV server handling:
    - `OPTIONS`: Discovery headers (`DAV: 1, 2`, `MS-Author-Via: DAV`) and allowed verbs.
    - `PROPFIND`: Dynamic 207 Multi-Status XML generation for root directory collection (Depth: 0/1) and single-file metadata.
    - `GET` / `HEAD`: Returns `HTTP 302 Found` with direct `Location: <upstream_url>` (0 bytes video proxied through Worker).
    - `DELETE`: Unmounts virtual file stream from KV and isolate cache, returning `204 No Content`.
  - Routes Edge REST Control API:
    - `POST /api/mount/:userId`: Mounts a stream with title, URL, size, and custom headers.
    - `GET /api/mounts/:userId`: Returns current active mounts for a user.
    - `DELETE /api/mounts/:userId/:filename`: Unmounts a specific virtual file.
    - `POST /api/unmount-all/:userId`: Clears all mounts for a user.
    - `GET /health` and `GET /`: Health check confirming edge status and KV binding.
  - Orchestrates Two-Tier Stream Probing Shield:
    - Consumes Tier 1 probe metadata passed from Android client.
    - Executes Tier 2 edge probe (`Range: bytes=0-8191`, 4s timeout) if unprobed.
    - Falls back to 100 GiB synthetic floor (`SYNTHETIC_FLOOR_BYTES = 107_374_182_400`) to prevent mount failures.
  - Maintains backward-compatible reverse-proxy fallback to active Cloud Shell tunnels if dynamic tunnel mode is requested.
- **Dependencies**: `mount_manager.ts`, Cloudflare Workers Runtime (`fetch`, `Response`, `Request`).

---

### `cloudflare-worker/src/mount_manager.ts` (Edge Mount Storage & In-Memory Isolate Cache)
- **Role**: Virtual mount state persistence, validation, and multi-tier caching layer.
- **Key Responsibilities**:
  - Defines core TypeScript data schemas:
    - `StreamMount`: `id`, `filename`, `title`, `upstream_url`, `size_bytes`, `content_type`, `created_at`, `etag`, and optional `custom_headers`.
    - `UserMountsRecord`: `user_id`, `updated_at`, `mounts: StreamMount[]`.
  - Dual-Layer Persistence & Caching:
    - Cloudflare KV (`MOUNTS_KV`): Persistent key-value store (`mounts:{userId}`) with 24-hour expiration TTL (`KV_TTL_SECONDS = 86_400`).
    - In-Memory V8 Isolate Cache (`isolateMountsCache`): 15-second TTL cache (`ISOLATE_CACHE_TTL_MS = 15_000`) reducing Cloudflare KV read operations by over 90%.
    - Fallback In-Memory Storage (`fallbackMemoryStore`): In-memory Map enabling zero-configuration execution and unit testing when KV is not bound.
  - Exported Management Functions: `getUserMounts`, `saveUserMount`, `addMount`, `removeMount`, `clearMounts`, `findMount`, `deleteUserMount`.
- **Dependencies**: Cloudflare Workers KV API (`KVNamespace`).

---

## 2. Core Python Server & Streaming Modules (Local / Container Bridge)

### `main.py` (FastAPI Application & Root Dispatcher)
- **Role**: Application entry point exposing the Web UI, REST control endpoints, and root WebDAV dispatching.
- **Key Responsibilities**:
  - Initializes FastAPI with `redirect_slashes=False` to prevent HTTP 307 redirects.
  - Configures global permissive CORS middleware for web players.
  - Implements `root_dispatcher` handling `OPTIONS`, `PROPFIND`, `PROPPATCH` (WebDAV) and `GET` (Web UI).
  - Routes REST endpoints: `POST /api/mount`, `GET /api/status`, `GET /api/mounts`.
  - Dispatches `/dav/{path:path}` traffic directly to `webdav_engine.handle_webdav_request`.
- **Dependencies**: `fastapi`, `stream_probe`, `webdav_engine`, `range_proxy`.

---

### `webdav_engine.py` (RFC 4918 Virtual Filesystem Engine)
- **Role**: Emulates an in-memory RFC 4918 WebDAV server without storing files on disk.
- **Key Responsibilities**:
  - Manages the virtual mount registry (`MountManager`) loaded from/to `mounts.json`.
  - Generates compliant RFC 4918 `<D:multistatus>` XML with dynamic `<D:href>`, ISO 8601 creation dates, and RFC 1123 modification headers.
  - Handles single-file `PROPFIND /dav/{filename}` queries with a single `<D:response>` element.
  - Handles `OPTIONS` discovery requests returning `DAV: 1` and allowed methods.
  - Handles `HEAD` metadata requests for container sizes and seek capabilities.
  - Emits explicit `Content-Length` headers on all 207 Multi-Status responses.
- **Dependencies**: `range_proxy.stream_range_proxy`, `starlette.requests`, `starlette.responses`.

---

### `range_proxy.py` (RFC 7233 Zero-Copy Streaming Engine)
- **Role**: High-velocity async chunk streamer and seek-cancellation trap.
- **Key Responsibilities**:
  - Parses incoming HTTP Range headers via `parse_byte_range()` supporting all RFC 7233 variants (`start-end`, `start-`, `-suffix`).
  - Streams 128 KB buffer chunks directly from upstream via `httpx.AsyncClient.stream()` with zero local disk writes.
  - Intercepts client aborts/seeks within `<10ms`, immediately terminating upstream HTTP connections.
  - Maintains in-memory 50 MB sliding ring buffer for instantaneous backwards micro-seeks.
  - Tracks live streaming bandwidth and computes home internet savings metrics (`telemetry_stats`).
- **Dependencies**: `httpx`, `starlette.responses.StreamingResponse`.

---

### `stream_probe.py` (Sub-100ms Container & Capability Probe)
- **Role**: Zero-trust pre-flight validator for upstream URLs.
- **Key Responsibilities**:
  - Issues async 8 KB range probe (`Range: bytes=0-8191`) with 5-second timeout.
  - Detects MKV EBML magic bytes (`\x1a\x45\xdf\xa3`) and MP4 ISOBMFF boxes (`ftyp`, `moov`, `mdat`).
  - Verifies HTTP 206 support and extracts total file size from `Content-Range`.
  - Implements `extract_filename()` to parse authentic titles from presigned Cloudflare R2 / AWS S3 `response-content-disposition` parameters.
- **Dependencies**: `httpx`, `urllib.parse`.

---

### `standalone_server.py` (Autonomous Single-File Distribution)
- **Role**: Bundled single-file deployment containing the WebDAV engine, Range Proxy, Stream Probe, REST APIs, and embedded HTML Web UI.
- **Key Responsibilities**:
  - Enables zero-dependency execution in environments where external template files cannot be mounted (Google Colab, ephemeral containers, bare VPS).
  - Contains identical protocol compliance rules as modular files.

---

## 3. Cloud Deployment & Automation Scripts

### `colab_run.py` & `run_colab_cell.ps1`
- **Role**: Headless execution daemon and Windows UI automation runner for Google Colab.
- **Key Responsibilities**:
  - Automatically provisions a free Colab GPU/CPU runtime with datacenter multi-gigabit throughput.
  - Downloads and launches `cloudflared` tunnel, exposing the WebDAV bridge publicly.
  - Automates cell execution via Win32 UI automation in open Chrome tabs.

---

### `generate_colab_cell.py`
- **Role**: Generates the exact self-extracting, zero-dependency Python code block to paste into Google Colab notebooks.

---

### `hf_zero_touch_deployer.py`
- **Role**: Autonomous Hugging Face Spaces Docker deployer.
- **Key Responsibilities**:
  - Authenticates with Hugging Face API using user tokens.
  - Creates and updates Docker Spaces running `Dockerfile` on port `7860`.

---

### `start_tunnel.ps1`
- **Role**: Standalone PowerShell script for launching zero-signup Cloudflare Quick Tunnels (`cloudflared tunnel --url http://localhost:7860`).
- **Key Responsibilities**:
  - Captures the generated `https://*.trycloudflare.com` public URL.
  - Outputs ready-to-copy WebDAV and Web UI links.

---

## 4. Archived Legacy Cloud Shell Modules (`archive/cloud_shell/`)

### `archive/cloud_shell/cloud_shell_runner.py` (ARCHIVED)
- **Role**: Legacy Google Cloud Shell execution daemon and dynamic tunnel coordinator.
- **Archival Status**: ARCHIVED. Replaced by Standalone Serverless Cloudflare Worker (`cloudflare-worker/src/index.ts`).
- **Historical Functionality**:
  - Managed local FastAPI WebDAV processes inside Google Cloud Shell ephemeral containers.
  - Initialized `cloudflared` quick tunnels and registered active tunnel endpoints with Render Central Hub.
  - Handled automated heartbeat pings to prevent Cloud Shell 20-minute idle session termination.

---

### `archive/cloud_shell/cloud_shell_init.sh` (ARCHIVED)
- **Role**: Legacy bash bootstrap script for Cloud Shell instances.
- **Archival Status**: ARCHIVED.
- **Historical Functionality**:
  - Provisioned Python 3.11 virtual environment, downloaded `cloudflared` binary, and launched background headless runner.

---

## 5. Frontend & Presentation

### `templates/index.html` (Web UI & Mount Manager)
- **Role**: User-facing web dashboard and interactive mount manager.
- **Key Responsibilities**:
  - Provides 1-click mounting form for upstream URLs with custom title overrides.
  - Displays CX File Explorer configuration cards (Host, Port 7860, Path `/dav/`).
  - Provides VLC direct player intents (`vlc://...`) and dynamic QR codes for mobile scanning.
  - Displays live telemetry cards: total streamed data, active streams, and home bandwidth saved.

---

## 6. Canonical Testing & Verification Suites

### `tests/test_webdav.py` (Canonical Pytest Suite)
- **Role**: 11-test automated integration and unit test suite.
- **Coverage**:
  - `test_parse_byte_range`: Validates all RFC 7233 range permutations.
  - `test_webdav_options`: Validates `DAV: 1` discovery and allowed verbs on `/dav/`.
  - `test_root_webdav_options_and_propfind`: Validates Root `/` multi-method WebDAV dispatching.
  - `test_dav_without_trailing_slash_no_redirect`: Enforces zero 307/308 redirects.
  - `test_webdav_content_length_on_multistatus`: Verifies exact XML content length.
  - `test_presigned_r2_query_param_filename_extraction`: Tests R2/S3 URL title parsing.
  - `test_single_file_propfind`: Validates single `<D:response>` for file PROPFIND.
  - `test_webdav_propfind_xml_generation`: Verifies XML schema validation.
  - `test_fastapi_endpoints`: Verifies `/`, `/api/status`, `/api/mounts`.
  - `test_webdav_head_request`: Tests HEAD metadata and `Accept-Ranges`.
  - `test_stream_probe_container_detection`: Mocked MKV EBML magic bytes verification.

---

### `tests/verify_cx.py` (Live CX File Explorer Client Simulator)
- **Role**: 9-point end-to-end client verification suite simulating authentic CX File Explorer / Android TV WebDAV client handshakes against local or remote URLs.
