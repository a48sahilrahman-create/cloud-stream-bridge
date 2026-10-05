# API & Protocol Contracts — CloudStream WebDAV Bridge

> **Executive Summary**: Comprehensive protocol contracts covering RFC 4918 WebDAV virtual directory endpoints, RFC 7233 HTTP Range proxy routes, Cloudflare Worker edge WebDAV server endpoints, and REST management APIs.

---

## 1. WebDAV Protocol Specifications (RFC 4918)

### 1.1 WebDAV Discovery: `OPTIONS`
Advertises WebDAV capabilities and allowed verbs to clients (CX File Explorer, Windows WebDAV, VLC).

- **Endpoints**: `OPTIONS /`, `OPTIONS /dav`, `OPTIONS /dav/`, `OPTIONS /dav/{filename}`
- **Request Headers**: None required.
- **Response Status**: `200 OK`
- **Response Headers**:
  ```http
  DAV: 1
  MS-Author-Via: DAV
  Allow: OPTIONS, GET, HEAD, PROPFIND, PROPPATCH
  Accept-Ranges: bytes
  Content-Length: 0
  ```

---

### 1.2 Directory & Resource Inspection: `PROPFIND`
Queries virtual directory listings or specific file metadata.

- **Endpoints**: `PROPFIND /`, `PROPFIND /dav/`, `PROPFIND /dav/{filename}`
- **Request Headers**:
  - `Depth`: `0` (resource itself) or `1` (resource and immediate children).
- **Response Status**: `207 Multi-Status`
- **Response Headers**:
  ```http
  Content-Type: application/xml; charset=utf-8
  Content-Length: <exact_byte_count>
  ```

#### XML Payload: Directory Listing (`Depth: 1` on `/dav/`)
```xml
<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:">
  <!-- 1. The Directory Resource -->
  <D:response>
    <D:href>/dav/</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype><D:collection/></D:resourcetype>
        <D:displayname>dav</D:displayname>
        <D:getlastmodified>Fri, 02 Oct 2026 15:00:00 GMT</D:getlastmodified>
        <D:creationdate>2026-10-02T15:00:00Z</D:creationdate>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <!-- 2. Mounted Media File Resources -->
  <D:response>
    <D:href>/dav/Oppenheimer.2023.2160p.UHD.Remux.mkv</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype/>
        <D:displayname>Oppenheimer.2023.2160p.UHD.Remux.mkv</D:displayname>
        <D:getcontentlength>85899345920</D:getcontentlength>
        <D:getcontenttype>video/x-matroska</D:getcontenttype>
        <D:getlastmodified>Fri, 02 Oct 2026 15:00:00 GMT</D:getlastmodified>
        <D:creationdate>2026-10-02T15:00:00Z</D:creationdate>
        <D:getetag>"mount_abc123-85899345920"</D:getetag>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>
```

#### XML Payload: Single File Query (`PROPFIND /dav/{filename}`)
MUST return exactly 1 `<D:response>` element matching the target resource without collection tags.

---

### 1.3 Media Metadata Probe: `HEAD`
Called by media players prior to playback to query container length and seekability.

- **Endpoint**: `HEAD /dav/{filename}`
- **Response Status**: `200 OK`
- **Response Headers**:
  ```http
  Accept-Ranges: bytes
  Content-Length: <total_file_bytes>
  Content-Type: video/x-matroska (or video/mp4)
  ETag: "<mount_id>-<total_bytes>"
  ```

---

### 1.4 Streaming & Range Scrubbing: `GET`
Called by media players during continuous playback and scrubbing.

- **Endpoint**: `GET /dav/{filename}`
- **Request Headers**:
  - `Range`: `bytes=<start>-<end>` (e.g. `bytes=0-1048575` or `bytes=52428800000-`)
- **Response Status**:
  - `206 Partial Content` (when `Range` header is present)
  - `200 OK` (when no `Range` header is provided)
- **Response Headers**:
  ```http
  Content-Range: bytes <start>-<end>/<total_bytes>
  Accept-Ranges: bytes
  Content-Length: <chunk_bytes>
  Content-Type: video/x-matroska (or video/mp4)
  ```
- **Response Body**: Binary async streaming generator (128 KB chunks).

---

## 2. REST Management APIs

### 2.1 Mount Stream: `POST /api/mount`
Probes upstream URL in `<100ms` and mounts it into the virtual WebDAV directory.

- **Request Body**:
  ```json
  {
    "url": "https://pub-r2.dev/stream_blob?response-content-disposition=...",
    "title": "Optional Custom Display Title",
    "custom_headers": {
      "Authorization": "Bearer token",
      "Referer": "https://source.com"
    }
  }
  ```
- **Response Status**: `200 OK`
- **Response Body**:
  ```json
  {
    "status": "success",
    "mount": {
      "id": "mount_b164351e",
      "filename": "Movie.2026.4K.Remux.mkv",
      "title": "Movie 2026 4K",
      "total_bytes": 15200000000,
      "formatted_size": "14.16 GB",
      "content_type": "video/x-matroska",
      "container": "MKV (Matroska Remux)",
      "dav_url": "/dav/Movie.2026.4K.Remux.mkv"
    }
  }
  ```

---

### 2.2 System Telemetry: `GET /api/status`
Returns live bandwidth savings and active connection metrics.

- **Response Status**: `200 OK`
- **Response Body**:
  ```json
  {
    "status": "online",
    "active_streams": 1,
    "total_bytes_streamed": 104857600,
    "total_streamed_gb": 0.1,
    "home_bandwidth_saved_gb": 14.06,
    "uptime_seconds": 3600
  }
  ```

---

### 2.3 List Mounts: `GET /api/mounts`
Returns all currently registered virtual WebDAV files.

- **Response Status**: `200 OK`
- **Response Body**:
  ```json
  {
    "mounts": [
      {
        "id": "mount_b164351e",
        "filename": "Movie.2026.4K.Remux.mkv",
        "formatted_size": "14.16 GB",
        "content_type": "video/x-matroska",
        "created_at": "2026-10-02T15:00:00Z"
      }
    ]
  }
  ```

---

## 3. Cloudflare Worker Edge WebDAV Specifications (RFC 4918)

> **Architecture Note**: The Cloudflare Worker edge WebDAV server (`cloudstream-dav-bridge.<subdomain>.workers.dev`) operates as a permanent, serverless edge entry point. It provides native WebDAV directory navigation for CX File Explorer while offloading 100% of media data transfers.

### 3.1 Edge WebDAV Discovery: `OPTIONS /dav/:userId/`
Advertises WebDAV Class 1 & Class 2 capabilities and allowed methods to clients connecting to the permanent edge domain.

- **Endpoint**: `OPTIONS /dav/:userId/`
- **Request Headers**: None required.
- **Response Status**: `200 OK`
- **Response Headers**:
  ```http
  DAV: 1, 2
  MS-Author-Via: DAV
  Allow: OPTIONS, GET, HEAD, PROPFIND, DELETE, PROPPATCH, MKCOL
  Accept-Ranges: bytes
  Access-Control-Allow-Origin: *
  Access-Control-Allow-Methods: GET, HEAD, POST, PUT, DELETE, OPTIONS, PROPFIND, PROPPATCH, MKCOL
  Access-Control-Allow-Headers: *
  Access-Control-Expose-Headers: Location, Content-Range, Accept-Ranges, Content-Length, DAV
  ```

---

### 3.2 Directory & Resource Inspection: `PROPFIND /dav/:userId/`
Queries the user's virtual mounted directory or specific file metadata. Fully compliant with RFC 4918 and CX File Explorer XML parser requirements.

- **Endpoints**: `PROPFIND /dav/:userId/`, `PROPFIND /dav/:userId/:filename`
- **Request Headers**:
  - `Depth`: `0` (root collection or specific file metadata) or `1` (collection and immediate child mounts).
- **Response Status**: `207 Multi-Status`
- **Response Headers**:
  ```http
  Content-Type: application/xml; charset=utf-8
  DAV: 1, 2
  MS-Author-Via: DAV
  Access-Control-Allow-Origin: *
  ```

#### XML Payload: Root Collection Listing (`Depth: 0` on `/dav/:userId/`)
```xml
<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/dav/rmx3031-user/</D:href>
    <D:propstat>
      <D:prop>
        <D:displayname>rmx3031-user</D:displayname>
        <D:resourcetype><D:collection/></D:resourcetype>
        <D:getlastmodified>Mon, 05 Oct 2026 12:00:00 GMT</D:getlastmodified>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>
```

#### XML Payload: Full Directory Listing (`Depth: 1` on `/dav/:userId/`)
```xml
<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/dav/rmx3031-user/</D:href>
    <D:propstat>
      <D:prop>
        <D:displayname>rmx3031-user</D:displayname>
        <D:resourcetype><D:collection/></D:resourcetype>
        <D:getlastmodified>Mon, 05 Oct 2026 12:00:00 GMT</D:getlastmodified>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/dav/rmx3031-user/Avatar.The.Way.of.Water.2022.2160p.UHD.Remux.mkv</D:href>
    <D:propstat>
      <D:prop>
        <D:displayname>Avatar.The.Way.of.Water.2022.2160p.UHD.Remux.mkv</D:displayname>
        <D:resourcetype/>
        <D:getcontentlength>75161927680</D:getcontentlength>
        <D:getcontenttype>video/x-matroska</D:getcontenttype>
        <D:getetag>W/"mount_8f3a9e2c-75161927680"</D:getetag>
        <D:getlastmodified>Mon, 05 Oct 2026 12:00:00 GMT</D:getlastmodified>
        <D:supportedlock>
          <D:lockentry>
            <D:lockscope><D:exclusive/></D:lockscope>
            <D:locktype><D:write/></D:locktype>
          </D:lockentry>
        </D:supportedlock>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>
```

#### Single File Inspection: `PROPFIND /dav/:userId/:filename`
Returns exactly 1 `<D:response>` element matching the target virtual file resource without collection elements.

---

### 3.3 Zero-Byte Direct Stream Playback: `GET /dav/:userId/:filename`
Redirects media players directly to the upstream CDN origin. Zero media bytes traverse Cloudflare Worker isolates.

- **Endpoint**: `GET /dav/:userId/:filename`
- **Request Headers**: Media player range headers (e.g. `Range: bytes=0-`)
- **Response Status**: `302 Found`
- **Response Headers**:
  ```http
  Location: <upstream_cdn_url>
  Accept-Ranges: bytes
  Cache-Control: no-cache, no-store, must-revalidate
  DAV: 1, 2
  MS-Author-Via: DAV
  Access-Control-Allow-Origin: *
  Access-Control-Expose-Headers: Location, Content-Range, Accept-Ranges
  ```

---

### 3.4 Media Container Probe: `HEAD /dav/:userId/:filename`
Called by media player demuxers (ExoPlayer, VLC) before initiating playback. Returns direct 302 Found redirect to CDN URL.

- **Endpoint**: `HEAD /dav/:userId/:filename`
- **Response Status**: `302 Found`
- **Response Headers**:
  ```http
  Location: <upstream_cdn_url>
  Accept-Ranges: bytes
  Cache-Control: no-cache, no-store, must-revalidate
  DAV: 1, 2
  MS-Author-Via: DAV
  Access-Control-Allow-Origin: *
  Access-Control-Expose-Headers: Location, Content-Range, Accept-Ranges
  ```

---

### 3.5 Virtual Resource Removal: `DELETE /dav/:userId/:filename`
Removes the mounted file from the user's virtual directory via native WebDAV clients.

- **Endpoint**: `DELETE /dav/:userId/:filename`
- **Response Status**: `204 No Content`
- **Response Headers**:
  ```http
  DAV: 1, 2
  MS-Author-Via: DAV
  Access-Control-Allow-Origin: *
  ```

---

## 4. Cloudflare Worker Edge REST Management APIs

### 4.1 Mount Stream: `POST /api/mount/:userId`
Registers a new media stream under the user's permanent WebDAV directory. Automatically performs Tier 2 probe and applies the 100 GiB synthetic floor if size is missing or unprobed.

- **Endpoint**: `POST /api/mount/:userId`
- **Request Body**:
  ```json
  {
    "url": "https://pub-r2.dev/stream_blob?token=...",
    "title": "Avatar The Way of Water 2022 UHD Remux",
    "filename": "Avatar.The.Way.of.Water.2022.2160p.UHD.Remux.mkv",
    "size_bytes": 75161927680,
    "content_type": "video/x-matroska",
    "custom_headers": {
      "Authorization": "Bearer token",
      "Referer": "https://source.com"
    }
  }
  ```
  *(Note: `filename`, `size_bytes`, `content_type`, and `custom_headers` are optional)*
- **Response Status**: `200 OK`
- **Response Body**:
  ```json
  {
    "status": "mounted",
    "mount": {
      "id": "mount_a8f3b9c1",
      "filename": "Avatar.The.Way.of.Water.2022.2160p.UHD.Remux.mkv",
      "title": "Avatar The Way of Water 2022 UHD Remux",
      "upstream_url": "https://pub-r2.dev/stream_blob?token=...",
      "size_bytes": 75161927680,
      "content_type": "video/x-matroska",
      "created_at": 1728130800000,
      "etag": "W/\"a8f3b9c1-75161927680\""
    }
  }
  ```

---

### 4.2 List Mounts: `GET /api/mounts/:userId`
Retrieves all currently mounted virtual files for the user from KV / isolate cache.

- **Endpoint**: `GET /api/mounts/:userId`
- **Response Status**: `200 OK`
- **Response Body**:
  ```json
  {
    "user_id": "rmx3031-user",
    "count": 1,
    "mounts": [
      {
        "id": "mount_a8f3b9c1",
        "filename": "Avatar.The.Way.of.Water.2022.2160p.UHD.Remux.mkv",
        "title": "Avatar The Way of Water 2022 UHD Remux",
        "upstream_url": "https://pub-r2.dev/stream_blob?token=...",
        "size_bytes": 75161927680,
        "content_type": "video/x-matroska",
        "created_at": 1728130800000,
        "etag": "W/\"a8f3b9c1-75161927680\""
      }
    ]
  }
  ```

---

### 4.3 Remove Single Mount: `DELETE /api/mounts/:userId/:filename`
Unmounts a single virtual file from the user's WebDAV catalog.

- **Endpoint**: `DELETE /api/mounts/:userId/:filename`
- **Response Status**: `200 OK`
- **Response Body**:
  ```json
  {
    "status": "unmounted",
    "user_id": "rmx3031-user",
    "filename": "Avatar.The.Way.of.Water.2022.2160p.UHD.Remux.mkv"
  }
  ```

---

### 4.4 Unmount All Streams: `POST /api/unmount-all/:userId`
Clears all active stream mounts for the specified user from KV and cache.

- **Endpoint**: `POST /api/unmount-all/:userId`
- **Response Status**: `200 OK`
- **Response Body**:
  ```json
  {
    "status": "cleared",
    "user_id": "rmx3031-user",
    "mounts_cleared": 3
  }
  ```

---

### 4.5 Edge Health Check: `GET /health`
Returns runtime status, engine type, and Cloudflare KV binding status.

- **Endpoint**: `GET /health` (also available on `GET /`)
- **Response Status**: `200 OK`
- **Response Body**:
  ```json
  {
    "status": "ok",
    "engine": "serverless-edge",
    "kv_bound": true,
    "service": "cloudstream-cloudflare-worker",
    "render_hub_url": "https://cloud-stream-bridge.onrender.com",
    "cached_tunnels_count": 0,
    "timestamp": "2026-10-05T12:00:00.000Z"
  }
  ```
