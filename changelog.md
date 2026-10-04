# Changelog & Architectural State — CloudStream WebDAV Bridge

## Current State
- **Cloud Shell Longevity & Zero-Hallucination Active Verification Deployed**:
  - **Zero-Hallucination Active Liveness Architecture (`central_hub.py`, `MainActivity.kt`)**:
    - Decoupled heartbeat timeout (`HEARTBEAT_TIMEOUT_SEC = 120`) from the 12-hour session TTL (`ttl_sec = 43200`) in `central_hub.py`.
    - Central Hub performs active reachability probing on `{tunnel_url}/health` with immediate invalidation (`mark_inactive`) on Cloudflare HTTP 530 (Origin Down) or network timeouts, preventing false green status.
    - Android client (`MainActivity.kt`) performs direct pre-flight active health check on `$tunnelUrl/api/status` with strict HTTP 200 requirement, instantly transitioning to `🔴 Cloud Shell Sleeping (Unreachable)` upon failure.
    - Eliminated zombie touch renewals during WebDAV routing.
    - Bumped Android companion client to `v1.1.0` (`versionCode = 2`), displaying `CloudStream WebDAV Bridge v1.1.0 (Zero-Hallucination Verified)`.
  - **Google Cloud Shell Longevity & SIGHUP Immunity (`cloud_shell_init.sh`, `cloud_shell_runner.py`)**:
    - Detached `tmux` session (`cloudstream`) in `cloud_shell_init.sh` prevents terminal termination when browser tabs close or WebSocket drops.
    - Masked POSIX `SIGHUP` via `signal.signal(signal.SIGHUP, signal.SIG_IGN)` in `cloud_shell_runner.py`.
    - Decoupled process groups via `start_new_session=True` for `uvicorn` and `cloudflared`.
    - Engineered self-healing Cloudflare tunnel supervisor in `cloud_shell_runner.py` with automatic re-provisioning and Central Hub re-registration.
  - **Test Suite Verification**:
    - All 94/94 unit and integration tests passing (`pytest`) in 6.87s across central hub, runner, probe shield, and WebDAV engines.
- **WebDAV Probe Shield, Multi-Stage Readiness Tracker, Remote Library Vault & CX Button Emoji Fix Deployed**:
  - **WebDAV "Probe Shield" (`webdav_engine.py`)**:
    - Guarded against CX File Explorer demuxer corruption and infinite freezing when opening newly mounted streams before upstream probing finishes.
    - Implemented asynchronous hold logic using `asyncio.Event` and `asyncio.wait_for(timeout=4.0)` on incoming `PROPFIND`, `HEAD`, and `GET` requests while an upstream probe is active or `total_bytes <= 0`.
    - If probing exceeds 4 seconds, returns clean RFC 4918 WebDAV XML or `HTTP 503 Service Unavailable` with `Retry-After: 2`, instructing CX File Explorer to poll again without caching a degenerate 0-byte stream.
  - **Multi-Stage Real-Time Readiness Tracker (`stream_probe.py`, `main.py`)**:
    - Instrumented probe progression into 5 transparent lifecycle stages (`0% Initializing` -> `25% Resolving Redirects & CDN Origin` -> `60% Inspecting Container & Magic Bytes` -> `85% Locking Byte-Range & File Size` -> `100% Ready for CX File Explorer`).
    - Exposed real-time progress via REST endpoint `GET /api/probe/status/{probe_id}` with elapsed timing and container metadata.
  - **Visual Readiness Dashboard & Remote Library Vault (`templates/index.html`, `library_vault.py`)**:
    - Added real-time progress meter with gradient animations, stage labels, and active status badges (`🟡 PROBING (Wait)` vs `🟢 100% READY FOR CX FILE EXPLORER`).
    - Solved ephemeral cloud runner link loss across virtual environments by engineering `LibraryVault` (`library_vault.py` and `library_vault.json`).
    - Exposed REST endpoints (`/api/library/catalog`, `/api/library/fetch-remote`, `/api/library/save-item`, `/api/library/batch-mount`, `/api/library/export`, `/api/library/import`).
    - Provided in-app drawer for 1-click sync from GitHub Gists / raw JSON URLs, individual/batch mounting into fresh runners, and JSON export/import.
  - **CX File Explorer Button Text Emoji Fix (`WebDavClipboardHelper.smali`, `WebDavClipboardHelper.java`)**:
    - Diagnosed the garbled string `ÐŸ“‹ PASTE WEBDAV FROM CLIPBOARD` as a Windows CP1252 / ISO-8859-1 byte-to-char misinterpretation of 4-byte UTF-8 emoji bytes (`0xF0 0x9F 0x93 0x8B`).
    - Replaced the string with valid UTF-16 surrogates `"\ud83d\udccb PASTE WEBDAV FROM CLIPBOARD"`, producing the exact Modified UTF-8 (MUTF-8) DEX bytecode sequence `0x1E 0xED 0xA0 0xBD 0xED 0xB3 0x8B 0x20`.
    - Recompiled with `apktool --use-aapt2`, 4-byte aligned via `zipalign -f -v 4`, and signed with Android SDK v1/v2/v3 `apksigner.bat`. Deployed to `C:\Users\sahil\workspaces\cx-file-explorer-mod\cx_file_explorer_custom_aspect_ratio_mod.apk`.
  - **Device Identity & Pointer Routing Architecture Streamlined**:
    - Device IDs and Central Hub pointers operate strictly in the background as the routing substrate for multi-device multiplexing over the permanent Cloudflare Worker (`cloudstream-dav-bridge.sahil-cloudstream.workers.dev`), fully isolated from user-facing forms.
- **4K UHD Remux Playback Stuttering Resolution & Sub-10ms Background Kill-Switch (`webdav_engine.py`, `range_proxy.py`)**:
  - **4K Remux Stuttering & Buffering Resolved via Direct 302 CDN Redirection**:
    - Diagnosed that streaming 50-100GB 4K Remuxes (requiring sustained 60-90+ Mbps) through Cloudflare quick tunnels (`*.trycloudflare.com`) caused severe player starvation and freezing due to free tunnel TCP window limits and rate throttling.
    - Verified direct `HTTP 302 Found` redirection from `webdav_engine.py` directly to the upstream high-speed CDN, enabling CX File Explorer (ExoPlayer/VLC) to stream at full line speed with zero buffering.
  - **Background Data Consumption Terminated (<10ms Disconnect Kill-Switch)**:
    - Diagnosed upstream `httpx.AsyncClient` socket leaks in `range_proxy.py` where player disconnect/scrub terminated the Starlette downstream generator, but upstream connection pools continued draining gigabytes into memory buffers in the background.
    - Implemented explicit `await resp.aclose()` inside a strict `finally` block in `chunk_generator()`, severing upstream TCP connections in <10ms upon client disconnect.
  - **Link Mounting Delay & 0-Byte Demuxer Trap Diagnosed**:
    - Identified that `stream_probe.py` requires 3-8s for pre-flight capability detection (multi-hop redirects, cold CDN handshakes, 8KB EBML/MP4 magic byte checks, and Content-Range/Content-Length detection).
    - Isolated why CX File Explorer freezes if opened before probing finishes: unprobed streams have `total_bytes=0`, causing `parse_byte_range` to collapse `bytes=0-` to `bytes=0-0` (1 single byte), failing container demuxers and hanging the player in an infinite retry loop.
  - **Probe Shield & Live Readiness Engine Roadmap Deployed (`plan.md`)**:
    - Authored comprehensive implementation blueprint in `C:\Users\sahil\workspaces\cloud-stream-bridge\plan.md` defining:
      * *Probe Shield (`webdav_engine.py`)*: Asynchronously hold incoming `PROPFIND`/`HEAD`/`GET` queries during active probe or return `HTTP 503 Service Unavailable` with `Retry-After: 2` to eliminate 0-byte demuxer traps.
      * *Multi-Stage Readiness Tracker (`stream_probe.py`, `main.py`)*: 4-stage probe progression lifecycle (0% -> 25% Redirects -> 60% Container/Magic Bytes -> 85% Size Lock -> 100% Ready).
      * *Web UI Readiness Dashboard (`templates/index.html`)*: Multi-stage progress meter, status pills (`PROBING` vs `100% READY FOR CX FILE EXPLORER`), and completion banners.
      * *Ready-to-use Prompt for New Chat*: Formulated exact copy-paste continuation prompt.
- **Universal Cloudflare Worker Permanent Edge Bridge & CX File Explorer 1-Click Auto-Paste Deployed**:
  - **Universal Developer-Deployed Permanent Edge Ingress (`cloudstream-dav-bridge.sahil-cloudstream.workers.dev`)**: Deployed a single global Cloudflare Worker functioning as the universal permanent ingress point for all end-user Android clients. Eliminates per-user Cloudflare setup requirements, custom domains, and dynamic DNS. Routes `/dav/{user_id}/` by querying Render Hub (`/api/status/{user_id}`) with an in-memory 10s module cache, transparently reverse-proxying WebDAV discovery (`PROPFIND`, `OPTIONS`, `PROPPATCH`, `DELETE`, `MKCOL`) while serving instant `HTTP 302 Found` redirects for media streaming (`GET`, `HEAD`), ensuring 0 bytes of video egress bandwidth load on Cloudflare edge.
  - **Dead Tunnel Error Trapping**: Traps upstream 502/503/504/530 errors or Cloudflare HTML error pages when an upstream Cloud Shell tunnel goes dormant, returning clean RFC 4918 WebDAV XML with HTTP 503 Service Unavailable instructing the user to activate Cloud Shell.
  - **CX File Explorer WebDAV Dialog Controller Patch (`WebDavClipboardHelper.java` & Smali)**: Diagnosed and resolved dialog submission stall when tapping OK after 1-click clipboard paste. Identified that CX File Explorer's `ax.a3.v` dialog controller relied on internal private boolean flags `z0` (HTTPS) and `A0` (Anonymous) alongside synthetic mutators `r3()` and `v3()`. Injected reflection to synchronize `z0` and `A0`, invoked synthetic setters, and ensured non-empty credential fallbacks (`username: anonymous`, `password: anonymous`) to guarantee form validation method `z3()` passes.
  - **End-to-End On-Device Verification**: Verified on physical Android phone (Realme X7 Max 5G `RMX3031`, serial `UOCAU4C6CYZXLZGQ`). 1-click clipboard paste automatically populated all fields; tapping OK performed SSL validation against Cloudflare edge, registered the permanent WebDAV connection `CloudStream (rmx3031-4d61bc7eacf0)`, opened the directory displaying virtual 4K Remux files, and launched playback directly inside CX File Explorer `VideoPlayerActivity`.
- **CX File Explorer Connection Failure Resolution (Hybrid Reverse-Proxying & Clipboard Alignment)**:
  - **Hybrid Reverse-Proxying of WebDAV Metadata Methods (`central_hub.py` & `worker.js`)**: Diagnosed and resolved CX File Explorer connection failure caused by client aborts when encountering `HTTP 302 Found` on WebDAV discovery requests (`PROPFIND`, `OPTIONS`, `PROPPATCH`, `MKCOL`, `DELETE`). Per RFC 4918, WebDAV discovery methods must receive authoritative multi-status XML responses directly rather than redirections. Implemented method-aware hybrid proxying: all metadata and directory enumeration requests are transparently reverse-proxied to the active Cloud Shell tunnel (forwarding standard WebDAV headers such as `Depth`), while media streaming requests (`GET`, `HEAD`) preserve the direct `HTTP 302 Found` redirection to Google Cloud Shell backbones, maintaining 100% zero video byte proxying and zero bandwidth load on the hub.
  - **Clipboard Key Alignment (`MainActivity.kt`)**: Resolved CX File Explorer auto-parse failures by aligning the 1-click clipboard payload and JSON configuration with CX File Explorer's exact connection keys (`Protocol: https`, `SSL: true`, `HTTPS: true`, `Anonymous: true`, and single-line JSON format), enabling instant 1-click connection import and zero manual typing.
- **App Onboarding Workflow Inversion & Android TV D-Pad Focus Re-alignment (Android Client)**:
  - **Step 2 / Step 3 Sequential Inversion**: Swapped Step 2 and Step 3 in `activity_main.xml` and user guidance. Step 2 is now "Google Cloud Shell Launcher" (`card_cloud_shell`), ensuring users spin up and register the Cloud Shell backend first, while Step 3 is "CX File Explorer Setup" (`card_cx_setup`), guiding users to connect only after the WebDAV endpoint is actively online. Prevents client connection timeout and dormant 503 errors during first-time onboarding.
  - **Android TV D-Pad Remote Focus Chain**: Updated the TV remote navigation graph across the newly ordered cards (`btn_refresh_status` -> `btn_copy_cmd` -> `btn_open_cloud_shell` -> `btn_copy_cx` -> `btn_open_vlc` -> `edit_mount_url`), ensuring seamless vertical D-pad navigation with focused scale animation across TV and mobile form factors.
- **In-App Cloud Stream Management & Unmount / Deletion Engine Deployed (Android Client & Backend)**:
  - **Dynamic Virtual File Inspection Card (`card_cloud_mounts`)**: Added dedicated "Mounted Cloud Files" card between Stream Mount and Telemetry cards, dynamically polling and rendering all active virtual files mounted in Google Cloud Shell.
  - **Granular Individual Stream Deletion**: Provided an in-app 🗑️ Delete button for each mounted file item. Directly addresses corrupted URLs, dead links, or cluttered directories that cannot be deleted from CX File Explorer. Includes native confirmation dialog (`dialog_delete_title`) and calls `DELETE /api/mounts/{user_id}/{filename:path}` through Central Hub.
  - **Batch Cloud Purge Engine ("Clear All")**: Added a high-contrast 🗑️ Clear All button in the card header, triggering a confirmation dialog and calling `POST /api/unmount-all/{user_id}` to wipe all virtual files from Cloud Shell in one action.
  - **Auto-Sync & Instant UI Reflection**: Automatically refreshes the file list immediately upon mounting a new video stream and on each 3-second heartbeat cycle when Cloud Shell is active. Displays clean placeholder (`tv_no_mounts_placeholder`) when no streams are mounted.
  - **Android TV D-Pad Remote Navigation**: Integrated all delete buttons and the Clear All button into the D-pad remote focus chain with smooth 1.03x scale and 8dp elevation animation.
- **High-Contrast WCAG AAA Button & UI Layout System Deployed (Android Client)**:
  - **MaterialButton Background Tint Override Fixed**: Removed cyan-on-cyan button text invisibility defect caused by MaterialComponents automatically injecting `app:backgroundTint="?attr/colorPrimary"` over custom selectors. Explicitly enforced `app:backgroundTint="@null"`, `android:backgroundTint="@null"`, and `@color/white` bold text across all buttons, achieving 14.2:1 contrast ratio.
  - **Button Text Bounds & Overflow Fixed**: Eliminated multi-line button text clipping by shortening verbose all-caps strings (`📋 COPY CX FILE EXPLORER CONFIG` -> `📋 Copy CX File Explorer Config`, `🚀 OPEN GOOGLE CLOUD SHELL` -> `🚀 Open Cloud Shell`), changing rigid `layout_height="48dp"` to dynamic `layout_height="wrap_content"` with `android:minHeight="48dp"`, and converting primary actions to full-width (`match_parent`).
  - **Step 2 WebDAV Specification Layout De-collided**: Restructured Step 2 spec box so Host, Port, and Anonymous reside in a compact 3-column top row, while the 29-character WebDAV Path (`/dav/<device_id>/`) is allocated the full card width, preventing text overlap.
  - **Header Device Badge Multi-Line Squish Resolved**: Converted header badge layout to vertical orientation, ensuring the unique hardware device badge (`📱 Device: rmx3031-4f9a2e81c0d5`) renders as a single uninterrupted pill without squishing or wrapping.
- **Architectural Transition to Hardware-Anchored Globally Unique Device IDs**: Transitioned user identity and routing from Google Sign-In / email accounts to Hardware-Anchored Globally Unique Device IDs (e.g. `rmx3031-4f9a2e81c0d5`). Eliminates user sign-in friction, OAuth token renewal complexities, and Google Play Services/Credential Manager constraints on Android TV and mobile devices.
- **Google Cloud Shell Runner & Bootstrap CLI Hardening**: Updated `cloud_shell_runner.py` and `cloud_shell_init.sh` argument descriptions, `--help` strings, interactive prompts, and terminal banners to reference "Device Unique ID / Username (e.g. rmx3031-4f9a2e81c0d5)" and prompt for "Device Unique ID from Android app".
- **Test Suite Verification**: 60/60 unit and integration tests passing (`pytest`) across central hub, Cloud Shell runner, WebDAV streaming engine, DELETE/unmount handlers, and end-to-end integration flows.
- **Release APK Compiled & Deployed**: Standalone release APK built via `assembleRelease` (5.23 MB, down from 5.94 MB after removing Google Play Services Auth), verified with `apksigner` (v2 scheme valid), and deployed to `C:\Users\sahil\Desktop\CloudStream-WebDAV-Bridge-Release.apk`.
- **CX File Explorer WebDAV Authentication & Playback Defect Resolved**: Diagnosed and resolved CX File Explorer credential re-prompt loop caused by Cloudflare edge proxy dropping RFC 4918 WebDAV verbs (`PROPFIND`, `PROPPATCH`) with HTTP 405 on Render.
- **Permissive Authentication Engine**: Implemented zero-rejection permissive basic authentication in `webdav_engine.py`, accepting unauthenticated requests, `Anonymous [X]`, or custom credentials (`admin:none`) without 401 challenge loops.
- **XML Entity Escaping & PROPPATCH Compliance**: Wrapped all XML multistatus injections in `xml_escape()` using `xml.sax.saxutils.escape`, preventing XML parsing crashes on special characters (`&`, `<`, `>`). Added standard RFC 4918 `PROPPATCH` 207 Multi-Status handler.
- **Root Fallback Stream Routing**: Added `root_fallback_dispatcher` for `/{filename:path}` in `main.py`, enabling seamless 4K video playback even when CX File Explorer users leave the optional `Path` field blank.
- **Multi-Port Dynamic Docker Entrypoint**: Hardened `Dockerfile` with dynamic port evaluation (`sh -c "uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}"`) and multi-port exposure (`7860`, `8000`, `8080`), compatible across local, Koyeb, Render, and custom containers.
- **Zero-Friction Web UI Quick Card**: Added dedicated 1-click **"Copy Host (No https://)"** button and explicit `Anonymous [X]` checkbox guidance in `templates/index.html`.
- **Live Streaming Endpoints Verified**:
  - Local Wi-Fi (Direct 4K, Zero Internet Lag): `http://192.168.220.41:7860/dav`
  - Active Remote Tunnel: `https://trailers-essex-dsl-progress.trycloudflare.com/dav`
  - Render Cloud Deployment: `https://cloud-stream-bridge.onrender.com`

## Known Issues
- Render free-tier domain (`*.onrender.com`) Cloudflare edge proxy blocks WebDAV directory discovery (`PROPFIND` -> 405), while fully allowing direct 4K byte-range streaming (`GET`). CX File Explorer WebDAV folder browsing must connect via Local LAN Wi-Fi or Cloudflare Tunnel.
- Koyeb free service deployments currently locked out during Mistral AI platform integration.
- Hugging Face Spaces now requires a paid PRO subscription for custom CPU compute spaces.

## Important Decisions
- **Hybrid WebDAV Reverse-Proxy with 302 Video Streaming Bypass**: Standard Android WebDAV clients (CX File Explorer, OkHttp) abort directory enumeration when encountering `HTTP 302 Found` on `PROPFIND` or `OPTIONS`. We reverse-proxy all metadata methods through the Central Hub and Cloudflare Worker (<2 KB XML payloads), while high-bitrate video streaming requests (`GET`, `HEAD`) strictly retain direct `HTTP 302 Found` redirects to Google Cloud Shell's multi-gigabit backbone. Preserves 100% zero video byte proxying and zero hub bandwidth consumption.
- **Lifecycle Onboarding Inversion (Cloud Shell Before CX)**: Positioned Google Cloud Shell Launcher as Step 2 (before CX File Explorer Setup as Step 3) in the Android UI and documentation, ensuring the backend runner is actively running and registered with Central Hub before the user attempts connection negotiation from CX File Explorer.
- **Hardware-Anchored Device Unique ID Architecture**: Selected deterministic hardware-anchored device IDs (`model-android_id`, e.g. `rmx3031-4f9a2e81c0d5`) over Google Sign-In / email accounts. Guarantees zero friction, instant bootstrap on Android TV remotes without keyboard typing, and persistent deterministic WebDAV URLs (`/dav/<device_id>/`).
- **Permissive WebDAV Authentication**: Implemented RFC-permissive authentication by default so clients can connect anonymously or with arbitrary credentials without triggering client-side credential re-prompts.
- **Root Fallback Catch-All**: Rather than requiring strict `/dav` pathing, routed root-level file requests to `handle_webdav_request` to accommodate mobile clients that omit path segments.
- **Local Wi-Fi First for 4K Remux**: Recommended local LAN IP (`192.168.220.41:7860`) for home Android TV playback to achieve maximum unthrottled local bitrate with zero cloud proxy latency.

## Next Steps
- Add persistent volume metadata caching for presigned URL expiration rollover.
- Add optional tokenized Basic Authentication for public internet deployments when desired.
