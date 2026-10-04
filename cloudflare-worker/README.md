# CloudStream WebDAV Bridge — Cloudflare Worker Deployment Guide

A serverless edge proxy that provides a **permanent, static WebDAV URL** for CX File Explorer, routing requests to Google Cloud Shell backbones with zero video byte proxying.

---

## 1. Quick Deployment (1-Click)

1. Double-click **`deploy_worker.bat`** in this directory, or run from terminal:
   ```cmd
   deploy_worker.bat
   ```
2. **First-time Authentication**:
   - The script verifies Wrangler status (`npx wrangler whoami`).
   - If not authenticated, it automatically triggers `npx wrangler login` in your browser.
3. **Automatic Deployment**:
   - Executes `npx wrangler deploy`.
   - Outputs your permanent worker URL:
     ```
     https://cloudstream-dav-bridge.<subdomain>.workers.dev
     ```

---

## 2. How the Permanent URL Works

```
[CX File Explorer / Android TV]
             │
             ▼ (Set host ONCE to static *.workers.dev)
[Cloudflare Edge Worker]
      │                   │
      │ (PROPFIND/OPTIONS)│ (GET/HEAD 4K Video)
      ▼                   ▼
[Reverse Proxy XML]    [HTTP 302 Found Direct Stream]
      │                   │
      ▼                   ▼
[Central Hub] ──► [Google Cloud Shell / Origin CDN]
```

- **Permanent Host Entry**: Google Cloud Shell ephemeral tunnel URLs rotate on restart. The Cloudflare Worker provides a fixed `*.workers.dev` domain that never changes.
- **Hybrid WebDAV Proxying**:
  - **Metadata Discovery (`PROPFIND`, `OPTIONS`, `PROPPATCH`)**: Transparently reverse-proxied with XML multi-status headers to allow CX File Explorer folder enumeration.
  - **Media Streaming (`GET`, `HEAD`)**: Returns immediate `HTTP 302 Found` redirects directly to Google Cloud Shell / CDN backbones, ensuring **0 video bytes traverse the worker** and avoiding any edge bandwidth limits.

---

## 3. CX File Explorer Configuration (Set Once & Forget)

In **CX File Explorer** (Android / Android TV):
1. Navigate to **Network** -> **Add (+)** -> **Remote Storage** -> **WebDAV**.
2. Enter the following parameters:

| Field | Configuration Value | Note |
| :--- | :--- | :--- |
| **Service** | `WebDAV` | Remote storage protocol |
| **Host** | `cloudstream-dav-bridge.<subdomain>.workers.dev` | **Omit `https://`** from the host field |
| **Path** | `/dav/<your-device-id>/` | e.g. `/dav/rmx3031-4f9a2e81c0d5/` |
| **Port** | `443` | Standard HTTPS port |
| **SSL / HTTPS** | `[X] Checked (ON)` | Mandatory for Cloudflare edge |
| **Anonymous** | `[X] Checked (ON)` | Or Username: `admin`, Password: `none` |

3. Click **OK**. CX File Explorer connects immediately and never needs host adjustments again.

---

## 4. Verification

Verify the deployed worker in your browser or curl:
```bash
curl -I https://cloudstream-dav-bridge.<subdomain>.workers.dev/dav/<your-device-id>/
```
Expected response: HTTP `200 OK` (when Cloud Shell backend is registered) or structured dormant XML if the backend is waiting to start.
