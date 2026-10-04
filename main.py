"""
CloudStream WebDAV Bridge - FastAPI Main Server
Exposes Web UI, REST Control APIs, and RFC 4918 Virtual WebDAV Endpoint.
Embedded Android Chaquopy Engine Edition.
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

from stream_probe import probe_stream, extract_filename
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


class UnmountRequest(BaseModel):
    filename: str


@app.api_route("/", methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH"])
async def root_dispatcher(request: Request):
    if request.method in ("OPTIONS", "PROPFIND", "PROPPATCH"):
        return await handle_webdav_request(request, path="")
    accept = request.headers.get("accept", "")
    if "text/html" in accept or "*/*" in accept:
        index_path = os.path.join(TEMPLATES_DIR, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path, media_type="text/html")
        return HTMLResponse(
            "<html><head><title>CloudStream WebDAV Bridge</title></head>"
            "<body style='font-family:sans-serif;padding:2rem;background:#121212;color:#eee;'>"
            "<h2>CloudStream WebDAV Bridge Engine Active</h2>"
            "<p>Android Chaquopy Embedded Server running.</p>"
            "<ul>"
            "<li>WebDAV Root: <code>/dav/</code></li>"
            "<li>Status API: <code>/api/status</code></li>"
            "<li>Mount API: <code>/api/mount</code> (POST)</li>"
            "<li>Mounts List: <code>/api/mounts</code> (GET)</li>"
            "<li>Health Check: <code>/health</code></li>"
            "</ul></body></html>"
        )
    return await handle_webdav_request(request, path="")


@app.post("/api/mount")
async def api_mount_stream(req: MountRequest, request: Request):
    """
    Probes upstream link in <100ms, detects 4K Remux container/range support,
    and mounts it as a virtual file on WebDAV.
    Guarantees mounting even if upstream probe fails or returns non-200.
    """
    url = req.url.strip()
    if not url:
        return JSONResponse({"status": "error", "message": "URL cannot be empty"}, status_code=400)

    # 1. Pre-flight probe (never block or 422 if valid is False)
    try:
        probe_result = await probe_stream(url, custom_headers=req.custom_headers)
    except Exception as e:
        logger.warning(f"Unexpected probe exception for {url}: {e}")
        probe_result = {
            "valid": False,
            "status_code": 0,
            "range_supported": True,
            "container_format": "Direct Video Stream",
            "content_type": "video/x-matroska",
            "total_bytes": 0,
            "formatted_size": "Dynamic Stream",
            "default_filename": extract_filename(url, ext=".mkv"),
            "final_url": url,
            "elapsed_ms": 0.0,
            "error": str(e)
        }

    # If probe fails, times out, or returns non-200, log a warning, but PROCEED to mount
    # the stream in dynamic/fallback mode instead of blocking with HTTP 422.
    is_fallback = not probe_result.get("valid", False)
    if is_fallback:
        logger.warning(
            f"Stream probe warning for {url}: {probe_result.get('error', 'Unknown probe error')}. "
            "Mounting stream in dynamic/fallback mode."
        )

    # 2. Register virtual mount
    filename = probe_result.get("default_filename") or extract_filename(url) or f"stream_{int(time.time())}.mkv"
    total_bytes = probe_result.get("total_bytes", 0)
    content_type = probe_result.get("content_type", "video/x-matroska")
    formatted_size = probe_result.get("formatted_size") or "Dynamic Stream"
    upstream_url = probe_result.get("final_url") or url
    title = req.title or filename
    movie_id = f"m_{int(time.time())}"

    mount = mount_manager.add_mount(
        movie_id=movie_id,
        filename=filename,
        upstream_url=upstream_url,
        total_bytes=total_bytes,
        content_type=content_type,
        formatted_size=formatted_size,
        title=title
    )

    # Base URL derivation
    base_url = str(request.base_url).rstrip("/")

    response_data = {
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

    if is_fallback:
        response_data["warning"] = f"Stream mounted in fallback mode: {probe_result.get('error')}"

    return response_data


@app.get("/api/mounts")
async def api_list_mounts():
    return {"status": "success", "mounts": mount_manager.list_all()}


@app.delete("/api/mounts/{filename:path}")
async def api_unmount(filename: str):
    clean_filename = filename.strip()
    removed = mount_manager.remove_mount(clean_filename)
    if removed:
        return {"status": "success", "message": f"Unmounted {clean_filename}"}
    return JSONResponse({"status": "error", "message": f"Stream '{clean_filename}' not found"}, status_code=404)


@app.post("/api/unmount")
async def api_unmount_post(req: UnmountRequest):
    clean_filename = req.filename.strip()
    removed = mount_manager.remove_mount(clean_filename)
    if removed:
        return {"status": "success", "message": f"Unmounted {clean_filename}"}
    return JSONResponse({"status": "error", "message": f"Stream '{clean_filename}' not found"}, status_code=404)


@app.delete("/api/mounts")
@app.post("/api/unmount-all")
async def api_unmount_all():
    cleared_count = mount_manager.clear_all()
    return {"status": "success", "cleared_count": cleared_count, "message": f"Unmounted {cleared_count} streams"}


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
    Lightweight health and keep-alive endpoint.
    Supports both GET and HEAD requests.
    """
    return {
        "status": "online",
        "service": "cloud-stream-bridge",
        "timestamp": time.time(),
        "mounted_count": len(mount_manager.list_all())
    }


async def keep_alive_daemon():
    """
    Cloud keep-alive daemon:
    Only runs if explicitly configured with an external cloud hosting URL (Render, Koyeb, etc.).
    Remains dormant in local/Android environments.
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
    # Only launch keep-alive daemon if external cloud domain is configured
    if os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEP_ALIVE_URL") or os.environ.get("KOYEB_PUBLIC_DOMAIN"):
        asyncio.create_task(keep_alive_daemon())


# WebDAV Endpoints (RFC 4918 Virtual Mount)
@app.api_route("/dav", methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH", "DELETE"])
@app.api_route("/dav/{path:path}", methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH", "DELETE"])
async def webdav_dispatcher(request: Request, path: str = ""):
    if not path and request.method == "GET":
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            index_path = os.path.join(TEMPLATES_DIR, "index.html")
            if os.path.exists(index_path):
                return FileResponse(index_path, media_type="text/html")
    return await handle_webdav_request(request, path)


# Root Fallback Dispatcher for clients that mount without /dav (e.g. CX File Explorer with empty Path)
@app.api_route("/{filename:path}", methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH", "DELETE"])
async def root_fallback_dispatcher(request: Request, filename: str):
    clean_fn = filename.strip("/")
    if clean_fn.startswith("api/") or clean_fn in ("ping", "health", "favicon.ico"):
        return PlainTextResponse("Not Found", status_code=404)
    return await handle_webdav_request(request, clean_fn)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
