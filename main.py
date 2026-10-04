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

from stream_probe import probe_stream, extract_filename, get_probe_status
from webdav_engine import handle_webdav_request, mount_manager
from range_proxy import telemetry_stats
from central_hub import hub_router, dav_redirect_router, registry
from library_vault import vault

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
    probe_id: Optional[str] = None


class UnmountRequest(BaseModel):
    filename: str


class RemoteFetchRequest(BaseModel):
    remote_url: str
    merge: Optional[bool] = True


class SaveItemRequest(BaseModel):
    url: str
    title: Optional[str] = None
    custom_headers: Optional[dict] = None
    size_hint: Optional[str] = None
    category: Optional[str] = "Movies"


class BatchMountRequest(BaseModel):
    items: list


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
    Probes upstream link, tracks multi-stage readiness, activates WebDAV Probe Shield,
    and mounts stream as a virtual file on WebDAV.
    Guarantees mounting even if upstream probe fails or returns non-200.
    """
    url = req.url.strip()
    if not url:
        return JSONResponse({"status": "error", "message": "URL cannot be empty"}, status_code=400)

    probe_id = req.probe_id or f"prb_{int(time.time()*1000)}"
    initial_filename = extract_filename(url) or f"stream_{int(time.time())}.mkv"
    title = req.title or initial_filename
    movie_id = f"m_{int(time.time())}"

    # Optimistic initial registration with probing=True & probe_id
    # This activates the WebDAV Probe Shield: incoming PROPFIND / HEAD / GET requests
    # from CX File Explorer will be held for up to 4.0s instead of receiving corrupt 0-byte sizes!
    mount_manager.add_mount(
        movie_id=movie_id,
        filename=initial_filename,
        upstream_url=url,
        total_bytes=0,
        content_type="video/x-matroska",
        formatted_size="Probing...",
        title=title,
        probing=True,
        probe_id=probe_id
    )

    # 1. Pre-flight probe with multi-stage telemetry
    try:
        probe_result = await probe_stream(url, probe_id=probe_id, custom_headers=req.custom_headers)
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
            "default_filename": initial_filename,
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

    # 2. Update mount with locked size & discovered parameters
    final_filename = probe_result.get("default_filename") or initial_filename
    total_bytes = probe_result.get("total_bytes", 0)
    content_type = probe_result.get("content_type", "video/x-matroska")
    formatted_size = probe_result.get("formatted_size") or "Dynamic Stream"
    upstream_url = probe_result.get("final_url") or url

    if final_filename != initial_filename and initial_filename in mount_manager.mounts:
        mount_manager.remove_mount(initial_filename)

    mount = mount_manager.add_mount(
        movie_id=movie_id,
        filename=final_filename,
        upstream_url=upstream_url,
        total_bytes=total_bytes,
        content_type=content_type,
        formatted_size=formatted_size,
        title=title,
        probing=False,
        probe_id=probe_id
    )

    # Auto-save to Library Vault for persistence across virtual environments
    vault.add_or_update(
        url=url,
        title=title,
        custom_headers=req.custom_headers,
        size_hint=formatted_size
    )

    # Base URL derivation
    base_url = str(request.base_url).rstrip("/")

    response_data = {
        "status": "success",
        "mount": mount,
        "probe": probe_result,
        "probe_id": probe_id,
        "stream_endpoints": {
            "webdav_folder": f"{base_url}/dav/",
            "webdav_file": f"{base_url}/dav/{final_filename}",
            "direct_stream": f"{base_url}/dav/{final_filename}",
            "vlc_intent": f"intent:{base_url}/dav/{final_filename}#Intent;action=android.intent.action.VIEW;type=video/*;end",
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


@app.get("/api/probe/status/{probe_id}")
async def api_probe_status(probe_id: str):
    """Returns real-time multi-stage probe progress for UI meter."""
    status = get_probe_status(probe_id)
    if not status:
        return JSONResponse({"status": "error", "message": "Probe ID not found"}, status_code=404)
    return {"status": "success", "probe": status}


# -------------------------------------------------------------
# Library Vault & Cloud Persistence APIs
# -------------------------------------------------------------
@app.get("/api/library/catalog")
async def api_library_catalog():
    """Retrieve all catalog items from persistent library vault."""
    return {"status": "success", "items": vault.list_items()}


@app.get("/api/library/export")
async def api_library_export():
    """Export complete stream catalog as JSON."""
    return {"status": "success", "catalog": vault.export_catalog()}


@app.post("/api/library/import")
async def api_library_import(payload: dict):
    """Import stream catalog JSON from client."""
    items = payload.get("items") or payload.get("catalog") or []
    if isinstance(payload, list):
        items = payload
    count = vault.import_catalog(items, merge=payload.get("merge", True) if isinstance(payload, dict) else True)
    return {"status": "success", "imported_count": count, "total_items": len(vault.catalog)}


@app.post("/api/library/fetch-remote")
async def api_library_fetch_remote(req: RemoteFetchRequest):
    """Fetch and sync stream links from GitHub Gist or raw JSON URL."""
    try:
        result = await vault.fetch_from_remote_url(req.remote_url, merge=req.merge)
        return {"status": "success", **result}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@app.post("/api/library/save-item")
async def api_library_save_item(req: SaveItemRequest):
    """Save an individual stream item to persistent catalog."""
    item = vault.add_or_update(
        url=req.url,
        title=req.title,
        custom_headers=req.custom_headers,
        size_hint=req.size_hint,
        category=req.category
    )
    return {"status": "success", "item": item}


@app.delete("/api/library/item/{item_id}")
async def api_library_delete_item(item_id: str):
    """Delete an item from persistent catalog."""
    removed = vault.remove_item(item_id)
    if removed:
        return {"status": "success", "message": f"Deleted item {item_id}"}
    return JSONResponse({"status": "error", "message": "Item not found"}, status_code=404)


@app.post("/api/library/batch-mount")
async def api_library_batch_mount(req: BatchMountRequest, request: Request):
    """
    Mounts multiple selected streams from the Library Vault into the active runner.
    """
    mounted = []
    base_url = str(request.base_url).rstrip("/")
    for item in req.items:
        url = item.get("url") if isinstance(item, dict) else str(item)
        if not url:
            continue
        title = item.get("title") if isinstance(item, dict) else None
        probe_id = f"prb_{int(time.time()*1000)}"
        filename = extract_filename(url) or f"stream_{int(time.time())}.mkv"

        try:
            probe_result = await probe_stream(url, probe_id=probe_id)
            final_filename = probe_result.get("default_filename") or filename
            total_bytes = probe_result.get("total_bytes", 0)
            content_type = probe_result.get("content_type", "video/x-matroska")
            formatted_size = probe_result.get("formatted_size") or "Dynamic Stream"
            upstream_url = probe_result.get("final_url") or url
        except Exception:
            final_filename = filename
            total_bytes = 0
            content_type = "video/x-matroska"
            formatted_size = "Dynamic Stream"
            upstream_url = url

        mount = mount_manager.add_mount(
            movie_id=f"m_{int(time.time())}",
            filename=final_filename,
            upstream_url=upstream_url,
            total_bytes=total_bytes,
            content_type=content_type,
            formatted_size=formatted_size,
            title=title or final_filename,
            probing=False,
            probe_id=probe_id
        )
        mounted.append(mount)

    return {"status": "success", "mounted_count": len(mounted), "mounts": mounted}


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
        "mounted_count": len(mount_manager.list_all()),
        "active_users": registry.get_active_count(),
        "total_registered": registry.get_total_count()
    }


# Include Multi-User Central Pointer Hub routes
# (/api/register, /api/heartbeat/{user_id}, /api/status/{user_id}, /api/mount/{user_id},
#  /api/unmount/{user_id}/{filename}, /api/unmount-all/{user_id}, /api/hub/users, /api/hub/status)
app.include_router(hub_router)


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


# WebDAV Endpoints (RFC 4918 Virtual Mount & Multi-User Router)
@app.api_route("/dav", methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH", "DELETE"])
@app.api_route("/dav/", methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH", "DELETE"])
async def webdav_dispatcher(request: Request):
    if request.method == "GET":
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            index_path = os.path.join(TEMPLATES_DIR, "index.html")
            if os.path.exists(index_path):
                return FileResponse(index_path, media_type="text/html")
    return await handle_webdav_request(request, "")


@app.api_route(
    "/dav/{user_id}/",
    methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH", "MKCOL", "DELETE", "POST", "PUT"],
)
@app.api_route(
    "/dav/{user_id}/{path:path}",
    methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH", "MKCOL", "DELETE", "POST", "PUT"],
)
async def webdav_user_path_dispatcher(user_id: str, request: Request, path: str = ""):
    return await dav_redirect_router(user_id=user_id, request=request, path=path)


@app.api_route(
    "/dav/{user_id}",
    methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH", "MKCOL", "DELETE", "POST", "PUT"],
)
async def webdav_user_single_dispatcher(user_id: str, request: Request):
    # Standalone mounted file check
    if mount_manager.get_mount(user_id):
        return await handle_webdav_request(request, user_id)
    # Check if this looks like a missing local media file vs a user_id
    lower_id = user_id.lower()
    media_exts = (".mkv", ".mp4", ".avi", ".ts", ".mov", ".m4v", ".webm", ".flv", ".iso", ".m3u8", ".mpd")
    if any(lower_id.endswith(ext) for ext in media_exts) and not registry.is_active(user_id) and not registry.get(user_id):
        return await handle_webdav_request(request, user_id)
    # Otherwise treat as multi-user hub root
    return await dav_redirect_router(user_id=user_id, request=request, path="")


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
