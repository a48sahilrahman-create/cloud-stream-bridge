"""
CloudStream WebDAV Bridge - FastAPI Main Server
Exposes Web UI, REST Control APIs, and RFC 4918 Virtual WebDAV Endpoint.
Designed for 1-Click Zero-Cost Cloud Deployment (Hugging Face Spaces / Render / Local).
"""

import os
import time
import asyncio
import logging
from typing import Optional
from pydantic import BaseModel
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("cloudstream_main")

from stream_probe import probe_stream
from webdav_engine import handle_webdav_request, mount_manager
from range_proxy import telemetry_stats

app = FastAPI(
    title="CloudStream WebDAV Bridge",
    description="High-Speed Cloud-to-Cloud Streaming Proxy for 50-100GB Remuxes",
    version="1.0.0",
    redirect_slashes=False
)

# Enable CORS for cross-device web players
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


class MountRequest(BaseModel):
    url: str
    title: Optional[str] = None
    custom_headers: Optional[dict] = None


@app.api_route("/", methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH"])
async def root_dispatcher(request: Request):
    if request.method in ("OPTIONS", "PROPFIND", "PROPPATCH"):
        return await handle_webdav_request(request, path="")
    accept = request.headers.get("accept", "")
    if "text/html" in accept or "*/*" in accept:
        index_path = os.path.join(TEMPLATES_DIR, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path, media_type="text/html")
        return HTMLResponse("<h1>CloudStream WebDAV Bridge Active</h1>")
    return await handle_webdav_request(request, path="")


@app.post("/api/mount")
async def api_mount_stream(req: MountRequest, request: Request):
    """
    Probes upstream link in <100ms, detects 4K Remux container/range support,
    and mounts it as a virtual file on WebDAV.
    """
    url = req.url.strip()
    if not url:
        return JSONResponse({"status": "error", "message": "URL cannot be empty"}, status_code=400)

    # 1. Pre-flight probe
    probe_result = await probe_stream(url, custom_headers=req.custom_headers)
    if not probe_result["valid"]:
        return JSONResponse({
            "status": "error",
            "message": f"Stream probe failed: {probe_result['error'] or 'Non-200/206 status'}",
            "details": probe_result
        }, status_code=422)

    # 2. Register virtual mount
    filename = probe_result["default_filename"]
    title = req.title or filename
    movie_id = f"m_{int(time.time())}"

    mount = mount_manager.add_mount(
        movie_id=movie_id,
        filename=filename,
        upstream_url=probe_result["final_url"],
        total_bytes=probe_result["total_bytes"],
        content_type=probe_result["content_type"],
        formatted_size=probe_result["formatted_size"],
        title=title
    )

    # Base URL derivation
    base_url = str(request.base_url).rstrip("/")

    return {
        "status": "success",
        "mount": mount,
        "probe": probe_result,
        "stream_endpoints": {
            "webdav_folder": f"{base_url}/dav/",
            "webdav_file": f"{base_url}/dav/{filename}",
            "direct_stream": f"{base_url}/dav/{filename}",
            "vlc_intent": f"intent:{base_url}/dav/{filename}#Intent;action=android.intent.action.VIEW;type=video/*;end",
            "cx_file_explorer": {
                "server": request.url.hostname or "localhost",
                "port": request.url.port or (443 if request.url.scheme == "https" else 80),
                "path": "/dav",
                "https": request.url.scheme == "https",
                "anonymous": True,
                "auth_note": "Check 'Anonymous' box in CX File Explorer"
            }
        }
    }


@app.get("/api/mounts")
async def api_list_mounts():
    return {"status": "success", "mounts": mount_manager.list_all()}


@app.delete("/api/mounts/{filename}")
async def api_unmount(filename: str):
    removed = mount_manager.remove_mount(filename)
    if removed:
        return {"status": "success", "message": f"Unmounted {filename}"}
    return JSONResponse({"status": "error", "message": "Not found"}, status_code=404)


@app.get("/api/status")
async def api_status():
    """
    Returns live performance telemetry and home bandwidth savings.
    """
    streamed_gb = round(telemetry_stats["total_bytes_streamed"] / (1024**3), 3)
    streamed_mb = round(telemetry_stats["total_bytes_streamed"] / (1024**2), 1)

    # Calculate total mounted virtual storage
    total_virtual_bytes = sum(m.get("total_bytes", 0) for m in mount_manager.list_all())
    virtual_gb = round(total_virtual_bytes / (1024**3), 2)

    # Home bandwidth saved = virtual size of mounted files minus what was actually streamed
    bandwidth_saved_gb = max(0.0, round(virtual_gb - streamed_gb, 2))

    return {
        "status": "online",
        "active_streams": telemetry_stats["active_streams"],
        "total_requests": telemetry_stats["total_requests_served"],
        "streamed_mb": streamed_mb,
        "streamed_gb": streamed_gb,
        "total_virtual_library_gb": virtual_gb,
        "home_bandwidth_saved_gb": bandwidth_saved_gb,
        "mounted_count": len(mount_manager.list_all())
    }


@app.api_route("/ping", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def api_ping():
    """
    Lightweight health and keep-alive endpoint for cloud hosting daemons.
    Supports both GET and HEAD requests across Koyeb, Render, Hugging Face, etc.
    """
    return {
        "status": "online",
        "service": "cloud-stream-bridge",
        "timestamp": time.time(),
        "mounted_count": len(mount_manager.list_all())
    }


async def keep_alive_daemon():
    """
    Automatic cloud keep-alive daemon:
    Pings RENDER_EXTERNAL_URL, KOYEB_PUBLIC_DOMAIN, or KEEP_ALIVE_URL every 10 minutes to prevent
    free-tier inactivity sleep on Render / Koyeb / cloud containers.
    """
    target = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEP_ALIVE_URL")
    if not target and os.environ.get("KOYEB_PUBLIC_DOMAIN"):
        kdomain = os.environ["KOYEB_PUBLIC_DOMAIN"]
        target = kdomain if kdomain.startswith("http") else f"https://{kdomain}"
    if not target:
        return
    ping_url = f"{target.rstrip('/')}/ping"
    logger.info(f"Keep-alive self-ping loop started for: {ping_url} (every 10m)")
    await asyncio.sleep(60)  # Initial grace period on boot
    while True:
        try:
            async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
                res = await client.get(ping_url)
                logger.info(f"[Keep-Alive] Ping {ping_url} -> {res.status_code}")
        except Exception as e:
            logger.warning(f"[Keep-Alive] Ping {ping_url} failed: {e}")
        await asyncio.sleep(600)  # 10 minutes


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(keep_alive_daemon())


# WebDAV Endpoints (RFC 4918 Virtual Mount)
@app.api_route("/dav", methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH"])
@app.api_route("/dav/{path:path}", methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH"])
async def webdav_dispatcher(request: Request, path: str = ""):
    if not path and request.method == "GET":
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            index_path = os.path.join(TEMPLATES_DIR, "index.html")
            if os.path.exists(index_path):
                return FileResponse(index_path, media_type="text/html")
    return await handle_webdav_request(request, path)


# Root Fallback Dispatcher for clients that mount without /dav (e.g. CX File Explorer with empty Path)
@app.api_route("/{filename:path}", methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH"])
async def root_fallback_dispatcher(request: Request, filename: str):
    clean_fn = filename.strip("/")
    if clean_fn.startswith("api/") or clean_fn in ("ping", "health", "favicon.ico"):
        return PlainTextResponse("Not Found", status_code=404)
    return await handle_webdav_request(request, clean_fn)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
