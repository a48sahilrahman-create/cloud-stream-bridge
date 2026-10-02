# Changelog & Architectural State — CloudStream WebDAV Bridge

## Current State
- **Production Cloud Deployment**: 24/7 WebDAV Bridge deployed on Render free cloud infrastructure at `https://cloud-stream-bridge.onrender.com`.
- **Zero-Lag Range Proxying**: Persistent connection pooling (`get_shared_client()`, `max_keepalive_connections=50`) deployed in `range_proxy.py`, eliminating repeated TLS/TCP handshake stalls during timeline scrubbing.
- **Automated Keep-Alive Loop**: Internal background `keep_alive_daemon` in `main.py` pings `RENDER_EXTERNAL_URL/ping` every 10 minutes, preventing Render free-tier 15-minute inactivity sleep.
- **CX File Explorer & VLC Integration**: Clean WebDAV endpoints (`/dav/` and `/dav/<filename>`), 1-click Direct Stream buttons, and verified Android intent URIs in `templates/index.html`.
- **Test Pass Count**: 11/11 unit tests passed (`pytest tests/test_webdav.py`).

## Known Issues
- Hugging Face Spaces free-tier requires PRO subscription for CPU/Docker backend containers (account `juvellerr` resolved via Render migration).
- Free-tier Render cold boot on very first request takes ~20-30s if dormant before keep-alive takes over.

## Important Decisions
- **Render Over Colab / Hugging Face**: Deployed directly to Render web services (`render.yaml`) to enable persistent 24/7 background operation without requiring an open browser tab or PC execution.
- **In-Process HTTP Keep-Alive Daemon**: Embedded a lightweight async loop in `main.py` using `RENDER_EXTERNAL_URL` instead of relying on external cron pingers or local machine scripts.
- **RFC 7233 Persistent Client Pooling**: Maintained a shared `httpx.AsyncClient` pool across range requests to match localhost scrubbing performance.

## Next Steps
- Add optional password authentication for public WebDAV deployments if desired.
- Explore persistent volume storage mounting for caching container metadata headers.
