# Changelog & Architectural State — CloudStream WebDAV Bridge

## Current State
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
- **Test Pass Count**: 16/16 unit and integration tests passed (`pytest tests/test_webdav.py`) in 1.32s.

## Known Issues
- Render free-tier domain (`*.onrender.com`) Cloudflare edge proxy blocks WebDAV directory discovery (`PROPFIND` -> 405), while fully allowing direct 4K byte-range streaming (`GET`). CX File Explorer WebDAV folder browsing must connect via Local LAN Wi-Fi or Cloudflare Tunnel.
- Koyeb free service deployments currently locked out during Mistral AI platform integration.
- Hugging Face Spaces now requires a paid PRO subscription for custom CPU compute spaces.

## Important Decisions
- **Permissive WebDAV Authentication**: Implemented RFC-permissive authentication by default so clients can connect anonymously or with arbitrary credentials without triggering client-side credential re-prompts.
- **Root Fallback Catch-All**: Rather than requiring strict `/dav` pathing, routed root-level file requests to `handle_webdav_request` to accommodate mobile clients that omit path segments.
- **Local Wi-Fi First for 4K Remux**: Recommended local LAN IP (`192.168.220.41:7860`) for home Android TV playback to achieve maximum unthrottled local bitrate with zero cloud proxy latency.

## Next Steps
- Add persistent volume metadata caching for presigned URL expiration rollover.
- Add optional tokenized Basic Authentication for public internet deployments when desired.
