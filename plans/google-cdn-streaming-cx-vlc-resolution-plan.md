# Plan: Google CDN Stream Playback & Stuttering Resolution (CX File Explorer, VLC, MPV)

- **Date:** 2026-10-06
- **Target Project:** `C:\Users\sahil\workspaces\cloud-stream-bridge`
- **Sub-Projects:**
  - `cloudflare-worker/` (Cloudflare Worker Edge WebDAV Proxy)
  - `webdav_engine.py` (Local / Render Python WebDAV Server)
- **Target Device:** Realme X7 Max 5G (`RMX3031`, Android 13) via ADB `172.19.11.121:5555`
- **Active Test Mount:** `mounts:rmx3031-4d61bc7eacf0` (19.55 GB 4K UHD Matroska stream on `video-downloads.googleusercontent.com`)

---

## 1. Executive Summary & Root-Cause Synthesis

### The Problem
When mounting high-bitrate media streams (e.g., 19.55 GB 4K UHD Remux `.mkv`) hosted on Google CDN (`video-downloads.googleusercontent.com`):
1. **CX File Explorer Media Player** immediately fails with a `"Playback error"` modal (`0x7f130158`).
2. **VLC and MPV** freeze, suffer extreme stuttering, and enter infinite disconnect/reconnect loops.
3. **PLAYit** plays smoothly without error.

### Root Cause Analysis

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              ROOT CAUSE TRIPLE BREAKDOWN                                │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ 1. Upstream Server Invariant (Google UploadServer):                                    │
│    - Endpoints on video-downloads.googleusercontent.com ignore HTTP Range headers.    │
│    - Range: bytes=1000000- or Range: bytes=0-1024 unconditionally return 200 OK       │
│      streaming from byte 0. Query parameter &range=... returns HTTP 400 Bad Request.  │
│                                                                                        │
│ 2. RFC 9110 Violation & 200 OK Content-Range Collision (Our Cloudflare Worker):       │
│    - When client requests Range and upstream returns 200 OK, Cloudflare Worker passed  │
│      status 200 OK but attached Content-Range: bytes 0-19557625797/19557625798.       │
│    - RFC 9110 Section 14.4 explicitly forbids Content-Range on HTTP 200 OK.           │
│    - AndroidX Media3 / ExoPlayer DefaultHttpDataSource validates responses:          │
│      receiving 200 OK on a range request or receiving illegal Content-Range throws    │
│      HttpDataSource$InvalidResponseCodeException: Response code: 200 or               │
│      DataSourceException: POSITION_OUT_OF_RANGE, triggering 0x7f130158 "Playback error".│
│                                                                                        │
│ 3. Hardcoded MIME Type Override on HEAD Probes:                                        │
│    - cloudflare-worker/src/index.ts (line 862) forces Content-Type: video/mp4 on all   │
│      Google CDN URLs, even for .mkv files. webdav_engine.py (line 81) also stripped   │
│      video/x-matroska and defaulted to video/mp4.                                      │
│    - ExoPlayer's DefaultExtractorsFactory prioritized Mp4Extractor, which failed on    │
│      the EBML header (0x1A45DFA3), crashing playback.                                  │
│                                                                                        │
│ 4. MKV Container Tail Seeking vs. Socket Thrashing (VLC / MPV):                       │
│    - Matroska stores EBML Cues (seek tables) at the file tail (~19.55 GB).             │
│    - WebDAV bridge advertised Accept-Ranges: bytes. VLC/MPV sent HTTP Range to the    │
│      tail. Google UploadServer ignored Range and sent byte 0. VLC saw header data      │
│      instead of Cues, treated the stream as corrupted, sent TCP RST, and looped.       │
│    - PLAYit succeeds because it treats HTTP streams as linear progressive downloads   │
│      from byte 0 without issuing random Range seek queries upfront.                    │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Architecture & File Modifications

### File 1: `cloudflare-worker/src/index.ts`

#### Fix A: Preserve Matroska / True MIME Types on HEAD & PROPFIND
- **Location:** Line 861–864 & Line 896
- **Current Defect:**
  ```typescript
  const contentType = isGoogleCdn(mount.upstream_url)
    ? "video/mp4"
    : (mount.content_type || inferContentType(mount.filename));
  ```
- **Correction:**
  ```typescript
  // Respect user-specified or filename-inferred MIME type; only default to video/mp4 if unknown
  const contentType = mount.content_type && mount.content_type !== "application/octet-stream"
    ? mount.content_type
    : inferContentType(mount.filename);
  ```

#### Fix B: RFC 9110 Protocol Compliance & Synthetic 206 Partial Content on GET
- **Location:** Lines 1002–1036
- **Current Defect:** Emits `Content-Range` header while preserving `status: 200`.
- **Correction:**
  1. If client requested `Range: bytes=0-...` (or `Range: bytes=0-`) and upstream returned `200 OK`:
     - Synthesize `HTTP 206 Partial Content` status.
     - Set valid `Content-Range: bytes 0-${contentLength - 1}/${totalBytes}`.
     - Set `Content-Length: ${contentLength}`.
  2. If upstream returned `200 OK` and client did NOT request a range:
     - Keep `HTTP 200 OK`.
     - **NEVER** set `Content-Range` header.
  3. If client requested a non-zero range (e.g. `Range: bytes=1000000-`) and upstream cannot seek (Google CDN returning 200):
     - For Google CDN endpoints, signal `Accept-Ranges: none` on `HEAD` and `GET` responses so players know the stream is sequential progressive and do not thrash sockets seeking for tail Cues.

#### Fix C: Range Header Advertising Strategy
- For known Google CDN / `UploadServer` URLs:
  - Return `Accept-Ranges: none` (or omit `Accept-Ranges`) on HEAD probes when seeking is unsupported.
  - This informs VLC, MPV, and ExoPlayer to treat the stream as progressive sequential, eliminating the tail-seek loop.

---

### File 2: `webdav_engine.py`

#### Fix A: Restore `video/x-matroska` in `infer_video_type`
- **Location:** Lines 81–83
- **Current Defect:**
  ```python
  if current_type and current_type.startswith("video/") and current_type not in ("video/octet-stream", "video/x-matroska"):
      return current_type
  return "video/mp4"
  ```
- **Correction:**
  ```python
  if current_type and current_type.startswith("video/") and current_type != "video/octet-stream":
      return current_type
  if ext in ext_map:
      return ext_map[ext]
  return "video/mp4"
  ```

#### Fix B: Synthetic 206 Response Generation
- Ensure Python range proxy never emits `Content-Range` on `200 OK` and returns clean `206 Partial Content` when range is fulfilled from byte 0.

---

### File 3: Automated Test Suites

1. `cloudflare-worker/test_worker.js`:
   - Add test: `HEAD on mounted Google CDN MKV returns video/x-matroska`.
   - Add test: `GET with Range: bytes=0- returns HTTP 206 Partial Content with Content-Range`.
   - Add test: `GET without Range returns HTTP 200 OK with NO Content-Range`.
   - Add test: `Google CDN responses advertise Accept-Ranges: none when range slicing is unsupported`.
2. `tests/test_google_stream_turbo.py`:
   - Update assertions to expect `video/x-matroska` for `.mkv` files.

---

## 3. Step-by-Step Execution Guide for New Chat

### Step 1: Apply Code Edits
Execute the edits in:
1. `C:\Users\sahil\workspaces\cloud-stream-bridge\cloudflare-worker\src\index.ts`
2. `C:\Users\sahil\workspaces\cloud-stream-bridge\webdav_engine.py`

### Step 2: Run Automated Regression Tests
```bash
# 1. Cloudflare Worker test suite
cd C:\Users\sahil\workspaces\cloud-stream-bridge\cloudflare-worker
node test_worker.js

# 2. Python WebDAV and Turbo Stream test suite
cd C:\Users\sahil\workspaces\cloud-stream-bridge
python -m pytest tests/test_google_stream_turbo.py -v
python -m pytest tests/test_webdav.py -v
```

### Step 3: Deploy Updated Edge Worker
```bash
cd C:\Users\sahil\workspaces\cloud-stream-bridge\cloudflare-worker
npx wrangler deploy
```

### Step 4: Verify via Live cURL
```bash
# 1. HEAD request - Verify MIME is video/x-matroska and Accept-Ranges: none
curl -I "https://cloudstream-dav-bridge.sahil-cloudstream.workers.dev/dav/rmx3031-4d61bc7eacf0/4K_UHD_Stream.mkv"

# 2. GET with Range bytes=0-1023 - Verify 206 Partial Content with valid Content-Range
curl -i -H "Range: bytes=0-1023" "https://cloudstream-dav-bridge.sahil-cloudstream.workers.dev/dav/rmx3031-4d61bc7eacf0/4K_UHD_Stream.mkv"

# 3. GET without Range - Verify 200 OK with NO Content-Range header
curl -i "https://cloudstream-dav-bridge.sahil-cloudstream.workers.dev/dav/rmx3031-4d61bc7eacf0/4K_UHD_Stream.mkv" | head -n 25
```

### Step 5: On-Device Verification (Realme X7 Max `RMX3031`)
1. Ensure wireless ADB is connected:
   ```bash
   adb connect 172.19.11.121:5555
   ```
2. Launch CX File Explorer and trigger playback of the mounted stream.
3. Observe logcat for zero `PlaybackException` / `InvalidResponseCodeException`:
   ```bash
   adb logcat -s ExoPlayerImpl:D EventLogger:D VideoPlayerActivity:D
   ```
4. Verify VLC for Android plays smoothly without infinite reconnection loops.

---

## 4. Key Reference Paths & Evidence
- **Plan File (Local Workspace):** `C:\Users\sahil\workspaces\cloud-stream-bridge\plans\google-cdn-streaming-cx-vlc-resolution-plan.md`
- **Plan File (Global Claude Plans):** `C:\Users\sahil\.claude\plans\google-cdn-streaming-cx-vlc-resolution-plan.md`
- **CX File Explorer Decompiled Reference:** `C:\Users\sahil\workspaces\cx-file-explorer-mod\cx_decompiled\smali\com\alphainventor\filemanager\viewer\VideoPlayerActivity$v.smali`
- **String Resource Reference:** `0x7f130158` (`<string name="error_playback">Playback error</string>`)
- **Active Mount KV Key:** `mounts:rmx3031-4d61bc7eacf0` on Cloudflare KV namespace
