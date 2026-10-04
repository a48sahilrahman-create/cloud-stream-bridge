# Master Architecture & Implementation Plan: CloudStream Multi-User Cloud Shell Suite

## 1. Executive Summary & Product Vision

Transform the CloudStream WebDAV Bridge into a multi-user, 100% free streaming platform:
1. **Android Client (Phone & Android TV)**:
   - One-tap sign-in via Google Account (using modern Android Credential Manager / Firebase Auth).
   - Generates a permanent, personalized WebDAV URL for CX File Explorer that never changes: `https://<hub-domain>/dav/<user_id>/`.
   - Generates a personalized 1-line command bound to the user's account token.
   - Provides a 1-tap button to launch Google Cloud Shell in the browser.
   - Remote stream mounting interface (paste link in app -> mounts in user's cloud container).
2. **Google Cloud Shell (Compute Backbone)**:
   - Runs a single command with the user's token: `curl -sSL https://.../cloud_shell_init.sh | bash -s <user_token>`.
   - Starts the WebDAV engine on Google's multi-gigabit infrastructure.
   - Automatically registers the new Cloudflare tunnel URL to the central hub.
   - 50-second anti-idle heartbeat keeps the session alive up to Google's 12-hour limit.
   - All mounted movies persist in `$HOME/.cloudstream-bridge/mounts.json`.
3. **Central Hub / Dynamic Pointer (Free Serverless / Fast Edge)**:
   - Routes incoming WebDAV traffic from CX File Explorer (`/dav/<user_id>/*`) via HTTP 302 Found redirects directly to the user's active Cloudflare tunnel.
   - Zero storage of video files; zero bandwidth consumption on the hub.

---

## 2. System Architecture & Data Flow

```
┌────────────────────────────────────────────────────────────────────────┐
│                        User Android App (Phone / TV)                   │
│  1. Sign in with Google (Firebase Auth / Credential Manager)           │
│  2. Permanent WebDAV Config: https://hub.domain.com/dav/{userId}/      │
│  3. Copy Account 1-Liner -> Tap "Open Google Cloud Shell"              │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    │ Taps "Open Cloud Shell" & pastes:
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   Google Cloud Shell (100% Free / User VM)             │
│  Command: curl -sSL https://.../cloud_shell_init.sh | bash -s {token}  │
│  - Launches WebDAV on port 7860 & trycloudflare tunnel                 │
│  - Registers Tunnel: POST https://hub.domain.com/api/register          │
│  - Maintains 12-Hour Anti-Idle Heartbeat pulse                         │
│  - Persistent library stored in $HOME/.cloudstream-bridge/mounts.json  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Registers {userId: tunnelUrl}
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│               Central Pointer Hub (Cloudflare Worker / Render)         │
│  - Maps: {userId} -> {active_trycloudflare_url}                        │
│  - Realtime SSE / WebSocket status broadcast to Android App            │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼ CX File Explorer requests /dav/{userId}/*
┌────────────────────────────────────────────────────────────────────────┐
│               CX File Explorer (Phone / Android TV)                    │
│  - Connects to permanent URL: https://hub.domain.com/dav/{userId}/    │
│  - Hub responds with HTTP 302: Location: https://*.trycloudflare.com   │
│  - Video plays with 4K hardware acceleration over Google's backbone!   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Detailed Component Specifications

### Component A: Android Client Application (`cloud-stream-bridge-android`)
- **Authentication Layer**:
  - Firebase Authentication with Google Sign-In SDK (`androidx.credentials.CredentialManager`).
  - Fallback: GitHub OAuth or anonymous guest token for users without Google Play Services.
  - Automatically derives a deterministic `userId` (e.g. `usr_a7f39b...`) stored in encrypted `SharedPreferences`.
- **Screen 1: Welcome & Authentication**:
  - Clean card design with Google Sign-In button.
  - Account profile summary (Name, Email, User ID badge).
- **Screen 2: Permanent CX File Explorer Setup**:
  - Displays the permanent connection parameters:
    - **Host**: `hub.domain.com` (or Render/Worker domain)
    - **Port**: `443` (HTTPS: ON)
    - **Path**: `/dav/{userId}/`
    - **User/Password**: Anonymous or User PIN
  - **"📋 COPY CX FILE EXPLORER CONFIG"** button: formats JSON and standard key-value blocks for CX's clipboard injector.
- **Screen 3: Cloud Shell 1-Tap Launcher**:
  - Personalized Command Display:
    ```bash
    curl -sSL https://raw.githubusercontent.com/a48sahilrahman-create/cloud-stream-bridge/main/cloud_shell_init.sh | bash -s {userId}
    ```
  - **"Copy Command"** button with haptic feedback and toast.
  - **"Open Google Cloud Shell"** primary action button: opens `https://shell.cloud.google.com/` directly in the user's browser.
- **Screen 4: Remote Stream Mounter & Telemetry**:
  - Real-time connection badge: `🔴 Cloud Shell Sleeping` vs `🟢 Cloud Shell Active (12h Backbone)`.
  - Stream mount card: paste link -> sends remote `POST` to Cloud Shell.
  - Live virtual library count and bandwidth metrics.

### Component B: Central Pointer Hub (`hub-pointer`)
- **Runtime Options (100% Free Tier, Zero Credit Card)**:
  - **Option 1 (Recommended)**: Cloudflare Worker + Cloudflare KV (100,000 free requests/day, 0ms cold boot globally).
  - **Option 2**: FastAPI on Render / Railway (already configured in existing repository).
- **Core Endpoints**:
  1. `POST /api/register`:
     - Payload: `{"user_id": "usr_...", "tunnel_url": "https://xyz.trycloudflare.com", "token": "..."}`
     - Stores mapping in KV / in-memory cache with an automated 12-hour TTL.
  2. `GET /api/status/{user_id}`:
     - Returns `{ "active": true|false, "last_seen": timestamp, "tunnel_url": "..." }`.
  3. `ALL /dav/{user_id}/{path:path}`:
     - If user is offline: returns HTTP 503 with a friendly XML message telling CX File Explorer: "Cloud Shell is dormant. Run your command in Google Cloud Shell to activate."
     - If user is active: returns immediate `HTTP 302 Found` with `Location: <tunnel_url>/dav/<path>`.
     - Completely bypasses bandwidth usage on the hub; video streams directly from Google Cloud Shell to CX File Explorer.

### Component C: Google Cloud Shell Runner (`cloud_shell_runner.py` & `init.sh`)
- Updated to parse user arguments:
  ```bash
  bash cloud_shell_init.sh usr_12345
  ```
- Automatically pings the central hub immediately after extracting the public Cloudflare tunnel URL:
  ```python
  httpx.post("https://hub.domain.com/api/register", json={"user_id": USER_ID, "tunnel_url": tunnel_url})
  ```
- Anti-idle heartbeat daemon continues to send periodic pulses to the hub every 60 seconds to refresh the active state.
- Mounts and library remain permanently stored in `$HOME/.cloudstream-bridge/mounts.json`.

---

## 4. Phased Implementation Roadmap

- **Phase 1: Central Pointer Hub Deployment**:
  - Deploy lightweight router endpoint (`/api/register` and `/dav/{user_id}/*` 302 redirection) to Cloudflare Workers or Render.
  - Test multi-user room separation (User A cannot see User B's mounts).
- **Phase 2: Cloud Shell Runner Integration**:
  - Update `cloud_shell_runner.py` to accept `--user <id>` CLI argument and auto-register with the hub.
  - Update `cloud_shell_init.sh` to forward arguments.
- **Phase 3: Android App Authentication & Multi-Screen UI**:
  - Remove all legacy Chaquopy local-mode code.
  - Integrate Google Credential Manager / Firebase Auth.
  - Build the 3-step setup wizard (Sign-in -> Permanent CX Config -> 1-Tap Cloud Shell Command).
- **Phase 4: End-to-End Verification & User Testing**:
  - Test on multiple distinct Google accounts.
  - Verify 1-click CX File Explorer auto-paste on Phone and Android TV.

---

## 5. Architectural Invariants & Security Guardrails
1. **Zero Credit Card & Zero Cost Invariant**: All components must remain 100% free forever (Google Cloud Shell 50h/wk quota, Cloudflare Quick Tunnels, Firebase Free Tier, Render/Cloudflare Worker free tiers).
2. **Zero Video Byte Proxying on Hub**: The central hub MUST NEVER proxy video stream payloads. It strictly issues HTTP 302 redirects, keeping hub bandwidth under a few megabytes per month regardless of petabyte video consumption.
3. **Persistent Cloud Shell Storage**: `$HOME/.cloudstream-bridge/mounts.json` guarantees that even after VM sleep, the user's library is never lost.
4. **Android TV Compatibility**: All screens must retain D-Pad remote navigation, large focusable targets, and high-contrast Leanback readability.
