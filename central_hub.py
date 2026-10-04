"""
Central Pointer Hub - CloudStream Multi-User Suite
FastAPI Production Central Router Hub

Provides dynamic 302 Found WebDAV redirection, multi-user tunnel registration,
heartbeat telemetry, and stream mounting forwarding to Google Cloud Shell backbones.
Zero video byte proxying; zero bandwidth consumption on the hub.
"""

import os
import re
import time
import logging
import threading
from typing import Optional, Dict, Any
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("central_hub")

# XML payload returned to CX File Explorer when Cloud Shell VM is dormant
DORMANT_XML_RESPONSE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<error>'
    '<message>Cloud Shell is dormant. Run your command in Google Cloud Shell to activate.</message>'
    '<status>dormant</status>'
    '</error>'
)


class InMemoryUserRegistry:
    """
    Thread-safe in-memory registry mapping user_id to active tunnel endpoints.
    Tracks registration timestamp, last seen heartbeat, and TTL (default 12 hours).
    """

    def __init__(self, default_ttl_sec: int = 43200):
        self._lock = threading.RLock()
        self._users: Dict[str, Dict[str, Any]] = {}
        self.default_ttl_sec = default_ttl_sec

    def register(
        self,
        user_id: str,
        tunnel_url: str,
        token: Optional[str] = None,
        ttl_sec: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Register or update an active Cloud Shell tunnel for a user."""
        with self._lock:
            now = time.time()
            ttl = ttl_sec if ttl_sec is not None and ttl_sec > 0 else self.default_ttl_sec
            existing = self._users.get(user_id)
            registered_at = existing["registered_at"] if existing else now

            clean_tunnel = tunnel_url.strip().rstrip("/")
            entry = {
                "user_id": user_id,
                "tunnel_url": clean_tunnel,
                "token": token if token is not None else (existing.get("token") if existing else None),
                "registered_at": registered_at,
                "last_seen": now,
                "ttl_sec": ttl,
            }
            self._users[user_id] = entry
            logger.info("User registered/updated: user_id=%s, tunnel=%s, ttl=%ss", user_id, clean_tunnel, ttl)
            return dict(entry)

    def get(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve user record if present."""
        with self._lock:
            entry = self._users.get(user_id)
            return dict(entry) if entry else None

    def is_active(self, user_id: str) -> bool:
        """Check if user registration exists and is within TTL."""
        with self._lock:
            entry = self._users.get(user_id)
            if not entry:
                return False
            now = time.time()
            return (now - entry["last_seen"]) < entry["ttl_sec"]

    def touch(self, user_id: str) -> bool:
        """Refresh last_seen heartbeat for an active user."""
        with self._lock:
            entry = self._users.get(user_id)
            if entry and (time.time() - entry["last_seen"]) < entry["ttl_sec"]:
                entry["last_seen"] = time.time()
                return True
            return False

    def get_status(self, user_id: str) -> Dict[str, Any]:
        """Return status dictionary conforming to specification."""
        with self._lock:
            entry = self._users.get(user_id)
            now = time.time()
            if not entry:
                return {
                    "active": False,
                    "user_id": user_id,
                    "tunnel_url": None,
                    "last_seen": None,
                    "ttl_remaining_sec": 0,
                }

            active = (now - entry["last_seen"]) < entry["ttl_sec"]
            ttl_remaining = max(0, int(entry["last_seen"] + entry["ttl_sec"] - now)) if active else 0

            return {
                "active": active,
                "user_id": user_id,
                "tunnel_url": entry["tunnel_url"] if active else None,
                "last_seen": entry["last_seen"],
                "ttl_remaining_sec": ttl_remaining,
            }

    def get_active_count(self) -> int:
        """Return count of users with valid, unexpired sessions."""
        with self._lock:
            now = time.time()
            return sum(1 for e in self._users.values() if (now - e["last_seen"]) < e["ttl_sec"])

    def get_total_count(self) -> int:
        """Return total number of registered records."""
        with self._lock:
            return len(self._users)

    def remove(self, user_id: str) -> bool:
        """Remove user from registry."""
        with self._lock:
            return self._users.pop(user_id, None) is not None

    def clear(self) -> None:
        """Clear all entries (primarily for test resets)."""
        with self._lock:
            self._users.clear()


# Global singleton registry
registry = InMemoryUserRegistry(default_ttl_sec=43200)

# Request Models
class RegisterRequest(BaseModel):
    user_id: str = Field(..., min_length=1, description="Unique permanent user identifier")
    tunnel_url: str = Field(..., min_length=1, description="Active Cloudflare or reverse tunnel URL")
    token: Optional[str] = Field(default=None, description="Optional authentication token")
    ttl_sec: Optional[int] = Field(default=None, description="Custom TTL in seconds (default 43200 = 12h)")


class MountRequest(BaseModel):
    url: str = Field(..., min_length=1, description="Direct video stream upstream URL")
    title: Optional[str] = Field(default=None, description="Optional display title")
    custom_headers: Optional[Dict[str, str]] = Field(default=None, description="Optional custom HTTP headers")


class UnmountRequest(BaseModel):
    filename: str = Field(..., min_length=1, description="Filename of virtual stream to unmount")


# FastAPI Application Setup
app = FastAPI(
    title="CloudStream Central Pointer Hub",
    description="Zero-bandwidth central router providing dynamic 302 Found WebDAV redirection for Google Cloud Shell backbones",
    version="1.0.0",
)

# Enable CORS for all origins (Phone, Android TV, Browser, WebDAV clients)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
@app.get("/")
async def health_check():
    """Health check endpoint reporting hub status and active user count."""
    return {
        "status": "healthy",
        "service": "cloudstream-central-hub",
        "active_users": registry.get_active_count(),
        "total_registered": registry.get_total_count(),
        "timestamp": time.time(),
    }


@app.post("/api/register")
async def register_user(payload: RegisterRequest):
    """
    Register or refresh a user's active Cloud Shell tunnel.
    Called by cloud_shell_init.sh / cloud_shell_runner.py upon tunnel establishment and heartbeat.
    """
    entry = registry.register(
        user_id=payload.user_id.strip(),
        tunnel_url=payload.tunnel_url.strip(),
        token=payload.token,
        ttl_sec=payload.ttl_sec,
    )
    return {
        "status": "registered",
        "user_id": entry["user_id"],
        "tunnel_url": entry["tunnel_url"],
        "active": True,
    }


@app.get("/api/status/{user_id}")
async def get_user_status(user_id: str):
    """
    Get active status and remaining TTL for a user.
    If dormant or not found, active is False.
    """
    return registry.get_status(user_id.strip())


@app.post("/api/mount/{user_id}")
async def forward_mount_request(user_id: str, payload: MountRequest):
    """
    Forwards a stream mount request to the user's active Google Cloud Shell backend.
    If the user's session is dormant, returns HTTP 503.
    """
    uid = user_id.strip()
    if not registry.is_active(uid):
        logger.warning("Mount requested for dormant user: %s", uid)
        return JSONResponse(
            status_code=503,
            content={
                "error": "Cloud Shell is dormant. Run your command in Google Cloud Shell to activate.",
                "status": "dormant",
                "user_id": uid,
            },
        )

    entry = registry.get(uid)
    tunnel_url = entry["tunnel_url"].rstrip("/")
    target_url = f"{tunnel_url}/api/mount"

    logger.info("Forwarding mount request for %s to %s", uid, target_url)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                target_url,
                json=payload.model_dump(exclude_none=True),
                headers={"Content-Type": "application/json"},
            )
            media_type = resp.headers.get("content-type", "application/json")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=media_type,
            )
    except httpx.TimeoutException:
        logger.error("Timeout connecting to Cloud Shell tunnel for user %s at %s", uid, target_url)
        return JSONResponse(
            status_code=502,
            content={
                "error": f"Connection timed out forwarding mount to Cloud Shell at {tunnel_url}",
                "status": "forward_timeout",
                "user_id": uid,
            },
        )
    except httpx.RequestError as exc:
        logger.error("Network error forwarding mount for user %s: %s", uid, exc)
        return JSONResponse(
            status_code=502,
            content={
                "error": f"Failed to forward mount to Cloud Shell: {str(exc)}",
                "status": "forward_failed",
                "user_id": uid,
            },
        )


@app.get("/api/mounts/{user_id}")
async def get_user_mounts(user_id: str):
    """
    Retrieves the list of active mounted streams from user's Cloud Shell instance.
    """
    uid = user_id.strip()
    if not registry.is_active(uid):
        return {"status": "dormant", "user_id": uid, "mounts": []}

    entry = registry.get(uid)
    tunnel_url = entry["tunnel_url"].rstrip("/")
    target_url = f"{tunnel_url}/api/mounts"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(target_url)
            media_type = resp.headers.get("content-type", "application/json")
            return Response(content=resp.content, status_code=resp.status_code, media_type=media_type)
    except Exception as exc:
        logger.error("Failed to fetch mounts for %s from %s: %s", uid, target_url, exc)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "message": f"Failed to fetch mounts: {str(exc)}", "user_id": uid, "mounts": []}
        )


@app.delete("/api/mounts/{user_id}/{filename:path}")
async def forward_unmount_request(user_id: str, filename: str):
    """
    Forwards an unmount request for a specific file to user's Cloud Shell.
    """
    uid = user_id.strip()
    if not registry.is_active(uid):
        return JSONResponse(status_code=503, content={"error": "Cloud Shell is dormant", "status": "dormant"})

    entry = registry.get(uid)
    tunnel_url = entry["tunnel_url"].rstrip("/")
    clean_filename = filename.strip()
    target_url = f"{tunnel_url}/api/mounts/{clean_filename}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(target_url)
            media_type = resp.headers.get("content-type", "application/json")
            return Response(content=resp.content, status_code=resp.status_code, media_type=media_type)
    except Exception as exc:
        logger.error("Failed to unmount %s for %s: %s", clean_filename, uid, exc)
        return JSONResponse(status_code=502, content={"error": f"Failed to unmount: {str(exc)}", "status": "error"})


@app.post("/api/unmount/{user_id}")
async def forward_unmount_post_request(user_id: str, payload: UnmountRequest):
    """
    Forwards a JSON-based unmount request for a specific file to user's Cloud Shell.
    """
    return await forward_unmount_request(user_id=user_id, filename=payload.filename)


@app.delete("/api/mounts/{user_id}")
@app.post("/api/unmount-all/{user_id}")
async def forward_unmount_all_request(user_id: str):
    """
    Forwards a batch unmount-all request to wipe all mounted files in user's Cloud Shell.
    """
    uid = user_id.strip()
    if not registry.is_active(uid):
        return JSONResponse(status_code=503, content={"error": "Cloud Shell is dormant", "status": "dormant"})

    entry = registry.get(uid)
    tunnel_url = entry["tunnel_url"].rstrip("/")
    target_url = f"{tunnel_url}/api/unmount-all"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(target_url)
            media_type = resp.headers.get("content-type", "application/json")
            return Response(content=resp.content, status_code=resp.status_code, media_type=media_type)
    except Exception as exc:
        logger.error("Failed to unmount all for %s: %s", uid, exc)
        return JSONResponse(status_code=502, content={"error": f"Failed to unmount all: {str(exc)}", "status": "error"})


@app.api_route(
    "/dav/{user_id}",
    methods=["GET", "HEAD", "PROPFIND", "OPTIONS", "PROPPATCH", "MKCOL", "DELETE", "POST", "PUT"],
)
@app.api_route(
    "/dav/{user_id}/",
    methods=["GET", "HEAD", "PROPFIND", "OPTIONS", "PROPPATCH", "MKCOL", "DELETE", "POST", "PUT"],
)
@app.api_route(
    "/dav/{user_id}/{path:path}",
    methods=["GET", "HEAD", "PROPFIND", "OPTIONS", "PROPPATCH", "MKCOL", "DELETE", "POST", "PUT"],
)
async def dav_redirect_router(user_id: str, request: Request, path: str = ""):
    """
    Central WebDAV entry point for CX File Explorer and media players.
    - If user is inactive or not found: returns HTTP 503 with XML body.
    - WebDAV metadata/directory queries (PROPFIND, OPTIONS, PROPPATCH, MKCOL, DELETE):
      Reverse-proxies directly to active Cloud Shell tunnel to avoid client redirect failure.
    - Media streaming (GET, HEAD): returns HTTP 302 Found redirect to target Cloud Shell tunnel,
      preserving query string, enabling direct client-to-Cloud-Shell streaming (zero video byte proxying).
    """
    uid = user_id.strip()
    if not registry.is_active(uid):
        logger.info("WebDAV access attempted for dormant user %s (%s)", uid, request.method)
        return Response(
            content=DORMANT_XML_RESPONSE,
            status_code=503,
            media_type="application/xml; charset=utf-8",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "*",
                "Access-Control-Allow-Headers": "*",
                "DAV": "1",
            },
        )

    entry = registry.get(uid)
    registry.touch(uid)

    clean_path = path.lstrip("/")
    encoded_path = quote(clean_path, safe="/:@?=&") if clean_path else ""
    tunnel_url = entry["tunnel_url"].rstrip("/")
    target_url = f"{tunnel_url}/dav/{encoded_path}" if encoded_path else f"{tunnel_url}/dav/"

    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    # WebDAV metadata or directory query: reverse-proxy directly to upstream tunnel
    if request.method.upper() in {"PROPFIND", "OPTIONS", "PROPPATCH", "MKCOL", "DELETE"}:
        body = await request.body()
        fwd_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length")
        }
        logger.debug("Proxying WebDAV %s %s -> %s", request.method, request.url.path, target_url)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.request(
                    method=request.method,
                    url=target_url,
                    headers=fwd_headers,
                    content=body,
                )
                media_type = resp.headers.get("content-type", "application/xml; charset=utf-8")
                response_headers = {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "*",
                    "Access-Control-Allow-Headers": "*",
                    "DAV": "1",
                }
                for header_name in ("Allow", "DAV", "MS-Author-Via", "ETag"):
                    if header_name in resp.headers:
                        response_headers[header_name] = resp.headers[header_name]

                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    media_type=media_type,
                    headers=response_headers,
                )
        except Exception as exc:
            logger.error("WebDAV proxy %s to %s failed: %s", request.method, target_url, exc)
            return Response(
                content=f'<?xml version="1.0" encoding="utf-8"?><error><message>WebDAV proxy error: {str(exc)}</message></error>',
                status_code=502,
                media_type="application/xml; charset=utf-8",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "*",
                    "Access-Control-Allow-Headers": "*",
                    "DAV": "1",
                },
            )

    # Media streaming (GET, HEAD): 302 Found redirect preserving zero video proxying
    logger.debug("Redirecting %s %s -> %s", request.method, request.url.path, target_url)
    return Response(
        status_code=302,
        headers={
            "Location": target_url,
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*",
            "DAV": "1",
        },
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("central_hub:app", host="0.0.0.0", port=port, reload=False)
