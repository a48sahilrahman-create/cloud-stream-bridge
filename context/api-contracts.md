# API & Protocol Contracts — CloudStream WebDAV Bridge

> **Executive Summary**: Comprehensive protocol contracts covering RFC 4918 WebDAV virtual directory endpoints, RFC 7233 HTTP Range proxy routes, and FastAPI REST management APIs.

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
