# Invariants & Safety Guardrails — CloudStream WebDAV Bridge

> **Core Directive**: These invariants represent non-negotiable operational, protocol, and architectural constraints. Any modification that violates these invariants breaks client compatibility with CX File Explorer, VLC, or Kodi, or induces cloud bandwidth exhaustion.

---

## 1. Zero Local Storage Invariant
- **Rule**: NEVER write, cache, or persist upstream media payloads to local disk (`C:\`, `/tmp`, Docker scratch storage).
- **Mechanism**: All streaming operations must use zero-copy in-memory async generators (`httpx.AsyncClient.stream()`) piped directly to Starlette `StreamingResponse`.
- **Buffer Bound**: The in-memory sliding ring buffer is strictly capped at 50 MB RAM per active stream.

---

## 2. RFC 4918 WebDAV Protocol Compliance
To guarantee native mounting on Android TV, Fire TV, Windows Explorer, macOS Finder, and Linux:
1. **Discovery Headers (`OPTIONS`)**:
   - MUST return `DAV: 1` (or `DAV: 1, 2`).
   - MUST return `MS-Author-Via: DAV`.
   - MUST return `Allow: OPTIONS, GET, HEAD, PROPFIND, PROPPATCH`.
   - MUST return `Accept-Ranges: bytes`.
   - MUST return `Content-Length: 0`.
2. **Multi-Status XML (`PROPFIND`)**:
   - Root element MUST be `<D:multistatus xmlns:D="DAV:">`.
   - MUST inject `<D:getetag>` (unique string hash of mount ID and file size).
   - MUST inject `<D:creationdate>` formatted as ISO 8601 UTC (`%Y-%m-%dT%H:%M:%SZ`).
   - MUST inject `<D:getlastmodified>` formatted as RFC 1123 HTTP date (`email.utils.formatdate(usegmt=True)`).
   - Dynamic `<D:href>`: The `<D:href>` tag must dynamically match the request path prefix (e.g. `/` vs `/dav/`) to prevent URL mismatch rejections.
   - Single `<D:response>`: A query for a specific file (e.g. `PROPFIND /dav/movie.mkv`) MUST return exactly 1 `<D:response>` element representing the file, never a directory collection.
3. **Explicit XML Content-Length**:
   - Every HTTP 207 Multi-Status response MUST carry an explicit `Content-Length` header matching `len(xml_content.encode("utf-8"))`. Missing `Content-Length` causes CX File Explorer to hang or drop the connection.

---

## 3. RFC 7233 HTTP Range Proxying & Scrubbing
1. **Range Header Parsing**:
   - `bytes=start-end`: Start byte to end byte inclusive.
   - `bytes=start-`: Start byte to `total_size - 1`.
   - `bytes=-suffix`: Last `suffix` bytes (`max(0, total_size - suffix)` to `total_size - 1`).
   - Boundary checks: Clamped to `total_size - 1` if end offset exceeds total file size.
2. **Response Status & Headers**:
   - MUST return `HTTP 206 Partial Content`.
   - MUST return `Content-Range: bytes <start>-<end>/<total>`.
   - MUST return `Accept-Ranges: bytes`.
   - MUST return `Content-Length: <end - start + 1>`.

---

## 4. Immediate Client Disconnect Trap (<10ms)
- **Problem**: When a user scrubs back and forth in VLC or CX File Explorer, the player aborts the current TCP connection and issues a new range request.
- **Invariant**: The streaming generator MUST intercept `asyncio.CancelledError`, `GeneratorExit`, or broken socket exceptions within `<10ms` and immediately terminate the upstream `httpx` stream.
- **Impact**: Eliminates zombie upstream downloads and prevents wasted cloud bandwidth.

---

## 5. Sub-100ms Container Magic Bytes Probe
- **Rule**: Upstream link viability must be established in `<100ms` before registering a mount.
- **Probe Probe Window**: Issues a single request with `Range: bytes=0-8191` (first 8 KB).
- **Magic Bytes Validation**:
  - Matroska (MKV): First 4 bytes MUST match EBML header `\x1a\x45\xdf\xa3`.
  - MP4 / QuickTime: Bytes 4-8 MUST contain brand identifiers (`ftyp`, `moov`, `mdat`).
- **Range Verification**: Server must verify `Accept-Ranges: bytes` or a valid `Content-Range` header. Links returning `HTTP 200` without range capabilities are rejected for remux streaming.

---

## 6. Presigned S3 / R2 Filename Extraction
- **Rule**: Direct download URLs frequently have non-descriptive hashes in the path (e.g. `/media/b164351e985feafe3e21fb194fa8e058?X-Amz-Algorithm=...`).
- **Invariant**: `stream_probe.extract_filename()` must parse `response-content-disposition` from URL query parameters (supporting both `filename="name"` and RFC 5987 `filename*=UTF-8''name`) and unquote percent-encoded characters before falling back to response headers or path components.

---

## 7. FastAPI Slashes & Root Routing Invariants
1. **`redirect_slashes=False`**:
   - FastAPI MUST be instantiated with `redirect_slashes=False`.
   - Default FastAPI redirects `/dav` to `/dav/` via HTTP 307 Temporary Redirect. CX File Explorer treats 307 redirects as fatal WebDAV discovery errors.
2. **Root `/` WebDAV Dispatcher**:
   - The root `/` route MUST support methods `["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH"]`.
   - If the incoming method is `OPTIONS`, `PROPFIND`, or `PROPPATCH`, it routes directly to `handle_webdav_request(request, path="")`.
   - If the incoming method is `GET` and the `Accept` header indicates a web browser (`text/html`), it serves the Web UI (`templates/index.html`).

---

## 8. 0-Byte Demuxer Immunity Invariant
- **Problem**: Media demuxers (ExoPlayer, VLC, CX File Explorer internal media player) immediately abort playback if `Content-Length` or `<D:getcontentlength>` is `0` or missing. Streams with delayed headers or non-range CDNs can fail to report size during initial probe.
- **Two-Tier Shield**:
  1. **Tier 1 (Residential 8 KB Probe)**: Android app / client probe executes a fast `Range: bytes=0-8191` probe within `<100ms` from residential network to retrieve true `Content-Range: bytes 0-8191/<total_bytes>`.
  2. **Tier 2 (100 GiB Synthetic Floor)**: If the Tier 1 probe fails or `size_bytes` is unspecified (`0` or missing), the Cloudflare Worker attempts a 4s edge probe. If size still cannot be determined, it injects a mandatory **100 GiB synthetic floor** (`107,374,182,400` bytes).
- **Invariant**: A mounted resource's `<D:getcontentlength>` MUST NEVER be `0` or negative. It must always be `>= 107,374,182,400` bytes when true size is unknown, guaranteeing media demuxer initialization without buffer underflow or instant EOS (End Of Stream).

---

## 9. Zero-Egress Direct Streaming Invariant
- **Rule**: NEVER proxy media payload bytes through Cloudflare Workers.
- **Mechanism**:
  - All `GET /dav/:userId/:filename` and `HEAD /dav/:userId/:filename` requests MUST immediately respond with `HTTP 302 Found` carrying a `Location: <upstream_cdn_url>` header pointing directly to the origin CDN or active tunnel.
  - Video stream scrubbing and continuous media transfers take place directly between client device and upstream CDN.
- **Bandwidth Bound**: Zero (0) media bytes egress through Cloudflare Worker edge isolates, preventing edge CPU timeout limits, bandwidth quota exhaustion, and latency overhead.

---

## 10. Isolate Cache Hit Rate Invariant (>90%)
- **Problem**: CX File Explorer and WebDAV clients aggressively poll `PROPFIND` (up to 5-10 queries per folder traversal/selection), which would rapidly deplete Cloudflare KV free read limits (100k reads/day).
- **Mechanism**:
  - The worker maintains an in-memory V8 isolate cache (`isolateMountsCache: Map<string, CacheEntry>`) with a **15-second TTL** (`ISOLATE_CACHE_TTL_MS = 15_000`).
  - Read requests (`PROPFIND`, `GET /api/mounts/:userId`) check the isolate memory cache first before dispatching `env.MOUNTS_KV.get()`.
- **Target Invariant**: Greater than **90%** of WebDAV `PROPFIND` metadata queries are absorbed by the 15s in-memory V8 isolate cache during typical browsing sessions, reducing KV operations to only cold starts and write mutations (`saveUserMount`, `deleteUserMount`, `clearUserMounts`).

---

## 11. Backwards Compatibility for CX File Explorer Invariant (`/dav/:userId/`)
- **Rule**: CX File Explorer connections configured with `/dav/:userId/` MUST remain 100% operational without requiring user re-configuration or path migration.
- **Guarantees**:
  - Host configuration in CX File Explorer points permanently to `cloudstream-dav-bridge.<subdomain>.workers.dev`.
  - Path prefix `/dav/:userId/` functions identically whether the user has standalone edge mounts in Cloudflare KV or an active dynamic Google Cloud Shell tunnel registered via Central Hub.
  - If standalone mounts exist in KV, the worker serves them directly from edge cache.
  - If no standalone mounts exist, the worker seamlessly delegates to the active Cloud Shell tunnel via `getActiveTunnel(hubUrl, userId)`, dynamically rewriting all `<D:href>` and `<href>` tags to `/dav/:userId/...`.
  - If Cloud Shell is dormant or offline and no standalone mounts exist, the worker returns an RFC 4918-compliant `503 Service Unavailable` with XML error description, preventing CX File Explorer connection crashes.
