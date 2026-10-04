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


def rewrite_single_href(url: str, tunnel_url: str, user_id: str) -> str:
    """
    Rewrite a single URL from an upstream WebDAV XML href.
    - If URL points to temporary tunnel_url, rewrite to permanent /dav/{user_id}/...
    - If URL is relative to /dav/, prefix with user_id (/dav/{user_id}/...)
    - Preserves URLs that already contain /dav/{user_id}/ or external URLs.
    """
    url_stripped = url.strip()
    clean_tunnel = tunnel_url.strip().rstrip("/")
    hub_prefix = f"/dav/{user_id}"

    # Candidate tunnel base URLs (support https and http)
    tunnels = [clean_tunnel]
    if clean_tunnel.startswith("https://"):
        tunnels.append("http://" + clean_tunnel[8:])
    elif clean_tunnel.startswith("http://"):
        tunnels.append("https://" + clean_tunnel[7:])

    for t in tunnels:
        if url_stripped.startswith(t):
            remainder = url_stripped[len(t):]
            if remainder.startswith("/dav"):
                remainder = remainder[4:]
            if not remainder.startswith("/"):
                remainder = "/" + remainder if remainder else "/"
            return f"{hub_prefix}{remainder}"

    # Handle upstream relative paths starting with /dav/
    if url_stripped.startswith("/dav/") and not url_stripped.startswith(f"/dav/{user_id}/"):
        remainder = url_stripped[len("/dav/"):]
        return f"{hub_prefix}/{remainder}"
    elif url_stripped == "/dav":
        return f"{hub_prefix}/"

    return url


def rewrite_webdav_hrefs(xml_content: str, tunnel_url: str, user_id: str) -> str:
    """
    Scan XML payload and rewrite any <D:href> (or <href>) URLs containing
    tunnel_url or relative /dav/ paths to point to permanent hub URLs (/dav/{user_id}/...).
    """
    pattern = re.compile(
        r"(<(?P<tag>(?:[A-Za-z0-9_-]+:)?href)\b[^>]*>)(.*?)(</(?P=tag)>)",
        re.IGNORECASE | re.DOTALL,
    )

    def _replace_href(match: re.Match) -> str:
        open_tag = match.group(1)
        raw_url = match.group(3)
        close_tag = match.group(4)
        rewritten = rewrite_single_href(raw_url, tunnel_url, user_id)
        return f"{open_tag}{rewritten}{close_tag}"

    return pattern.sub(_replace_href, xml_content)


def make_offline_error_response(
    user_id: str,
    exc: Exception,
    request: Request,
    tunnel_url: Optional[str] = None,
) -> Response:
    """
    Return robust HTTP 503 with informative JSON, text, or XML when Cloud Shell tunnel is offline or expired.
    """
    error_msg = f"Cloud Shell tunnel is offline or unreachable: {str(exc)}"
    accept = request.headers.get("accept", "").lower()

    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "*",
        "Access-Control-Allow-Headers": "*",
        "DAV": "1",
        "Retry-After": "10",
    }

    if "application/json" in accept:
        return JSONResponse(
            status_code=503,
            content={
                "error": error_msg,
                "status": "offline",
                "user_id": user_id,
                "tunnel_url": tunnel_url,
            },
            headers=headers,
        )

    if "text/plain" in accept:
        return Response(
            content=error_msg,
            status_code=503,
            media_type="text/plain; charset=utf-8",
            headers=headers,
        )

    # Standard WebDAV XML error response for CX File Explorer & other DAV clients
    escaped_msg = xml_escape(error_msg)
    escaped_uid = xml_escape(user_id)
    xml_body = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<error>\n'
        f'  <message>{escaped_msg}</message>\n'
        '  <status>offline</status>\n'
        f'  <user_id>{escaped_uid}</user_id>\n'
        '</error>'
    )
    return Response(
        content=xml_body,
        status_code=503,
        media_type="application/xml; charset=utf-8",
        headers=headers,
    )


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
    - If user is inactive or expired: returns HTTP 503 with informative XML/JSON/text body.
    - WebDAV metadata/directory queries (PROPFIND, OPTIONS, PROPPATCH, MKCOL, DELETE):
      Reverse-proxies directly to active Cloud Shell tunnel to avoid client redirect failure.
      Rewrites any <D:href> (or <href>) URLs in XML responses that contain tunnel_url so they
      point to permanent hub URLs (/dav/{user_id}/...).
    - Media streaming (GET, HEAD):
      * Direct 302 redirect to tunnel preserves Range headers (Accept-Ranges: bytes, Range in response,
        Access-Control-Expose-Headers) and all query parameters (token, seek, range, proxy flags).
      * Direct CDN Resolution: If tunnel can directly return upstream CDN 302 (requested via resolve_cdn=1,
        resolve=1, direct_cdn=1, x-resolve-cdn header, or RESOLVE_CDN_REDIRECT env), the hub queries
        the tunnel and redirects the client cleanly to the upstream CDN without unnecessary intermediate hops.
      * Robust error handling: Returns HTTP 503 with informative JSON/text/XML when tunnel is offline.
    """
    uid = user_id.strip()
    if not registry.is_active(uid):
        logger.info("WebDAV access attempted for dormant/expired user %s (%s)", uid, request.method)
        entry = registry.get(uid)
        is_expired = entry is not None and not registry.is_active(uid)
        accept = request.headers.get("accept", "").lower()

        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*",
            "DAV": "1",
        }

        if "application/json" in accept:
            return JSONResponse(
                status_code=503,
                content={
                    "error": (
                        f"Cloud Shell session has expired for user '{uid}'. Please re-run your Cloud Shell runner to reactivate."
                        if is_expired
                        else f"Cloud Shell is dormant for user '{uid}'. Run your command in Google Cloud Shell to activate."
                    ),
                    "status": "expired" if is_expired else "dormant",
                    "user_id": uid,
                    "active": False,
                },
                headers=headers,
            )

        if "text/plain" in accept:
            msg = (
                f"Cloud Shell session expired for user '{uid}'."
                if is_expired
                else f"Cloud Shell is dormant for user '{uid}'."
            )
            return Response(
                content=msg,
                status_code=503,
                media_type="text/plain; charset=utf-8",
                headers=headers,
            )

        return Response(
            content=DORMANT_XML_RESPONSE,
            status_code=503,
            media_type="application/xml; charset=utf-8",
            headers=headers,
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

                # Check if tunnel returned a gateway failure or dead Cloudflare tunnel HTML page
                if resp.status_code in (502, 503, 504) and (
                    b"<html" in resp.content.lower() or "text/html" in resp.headers.get("content-type", "")
                ):
                    logger.warning("Upstream tunnel %s returned HTTP %s HTML (offline)", target_url, resp.status_code)
                    return make_offline_error_response(
                        uid,
                        Exception(f"Upstream Cloud Shell tunnel is offline (HTTP {resp.status_code})"),
                        request,
                        tunnel_url=tunnel_url,
                    )

                media_type = resp.headers.get("content-type", "application/xml; charset=utf-8")
                content = resp.content

                # Rewrite <D:href> URLs in XML responses that contain tunnel_url or relative /dav/ paths
                if content and ("xml" in media_type.lower() or content.strip().startswith(b"<?xml") or b"<" in content):
                    try:
                        decoded = content.decode("utf-8")
                        rewritten = rewrite_webdav_hrefs(decoded, tunnel_url, uid)
                        content = rewritten.encode("utf-8")
                    except Exception as rewrite_err:
                        logger.warning("Error rewriting XML hrefs for %s: %s", uid, rewrite_err)

                response_headers = {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "*",
                    "Access-Control-Allow-Headers": "*",
                    "DAV": "1",
                }
                for header_name in ("Allow", "DAV", "MS-Author-Via", "ETag"):
                    if header_name in resp.headers:
                        response_headers[header_name] = resp.headers[header_name]

                if "location" in resp.headers:
                    response_headers["Location"] = rewrite_single_href(resp.headers["location"], tunnel_url, uid)

                return Response(
                    content=content,
                    status_code=resp.status_code,
                    media_type=media_type,
                    headers=response_headers,
                )
        except Exception as exc:
            logger.error("WebDAV proxy %s to %s failed: %s", request.method, target_url, exc)
            return make_offline_error_response(uid, exc, request, tunnel_url=tunnel_url)

    # Media streaming (GET, HEAD): HTTP 302 Found redirect preserving zero video proxying
    client_range = request.headers.get("range")
    redirect_headers = {
        "Location": target_url,
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "*",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Expose-Headers": "Location, Range, Content-Range, Accept-Ranges",
        "DAV": "1",
    }
    if client_range:
        redirect_headers["Range"] = client_range

    # Clean CDN Direct Mode: If requested, query tunnel to return upstream CDN 302 directly without unnecessary hops
    should_resolve_cdn = (
        request.query_params.get("resolve_cdn") == "1"
        or request.query_params.get("resolve") == "1"
        or request.query_params.get("direct_cdn") == "1"
        or request.headers.get("x-resolve-cdn") == "1"
        or os.environ.get("RESOLVE_CDN_REDIRECT") == "1"
    )
    if should_resolve_cdn and clean_path:
        fwd_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length")
        }
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                tunnel_probe = await client.request(
                    method=request.method,
                    url=target_url,
                    headers=fwd_headers,
                    follow_redirects=False,
                )
                if tunnel_probe.status_code in (301, 302, 307, 308) and "location" in tunnel_probe.headers:
                    cdn_url = tunnel_probe.headers["location"]
                    logger.info("Direct CDN 302 resolution: user %s -> %s", uid, cdn_url)
                    cdn_headers = dict(redirect_headers)
                    cdn_headers["Location"] = cdn_url
                    return Response(status_code=302, headers=cdn_headers)
        except Exception as exc:
            logger.error("Failed to query tunnel for CDN 302 (%s): %s", target_url, exc)
            return make_offline_error_response(uid, exc, request, tunnel_url=tunnel_url)

    logger.debug("Redirecting %s %s -> %s", request.method, request.url.path, target_url)
    return Response(
        status_code=302,
        headers=redirect_headers,
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("central_hub:app", host="0.0.0.0", port=port, reload=False)
