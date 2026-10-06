"""
WebDAV Server Engine (RFC 4918 & RFC 7233)
Provides a fully compliant virtual WebDAV filesystem for CX File Explorer, VLC, Kodi, and Nova Video Player.
Methods:
  - OPTIONS: Advertises DAV level 1 & 2 support and allowed verbs.
  - PROPFIND: Emits XML multistatus directory listings with exact file sizes and MIME types.
  - HEAD: Returns file metadata, Accept-Ranges: bytes, and Content-Length.
  - GET: Routes byte-range requests directly to Range Proxy Engine.
"""

import time
import os
import json
import base64
import email.utils
import asyncio
from urllib.parse import unquote, quote, urlsplit
from xml.sax.saxutils import escape
from typing import Dict, Any, Optional
from starlette.requests import Request
from starlette.responses import Response, PlainTextResponse, RedirectResponse
from range_proxy import stream_range_proxy
from stream_probe import get_probe_event, warm_mkv_tail

def xml_escape(val: Any) -> str:
    """Escape special characters (&, <, >, \", ') for safe XML injection."""
    return escape(str(val), {'"': "&quot;", "'": "&apos;"})

def clean_relative_path(path_or_url: str) -> str:
    """
    Ensure WebDAV path is always a clean relative path (/dav/...) without
    scheme or host, preventing broken http:// URLs when behind an HTTPS tunnel (RFC 4918 §8.3).
    """
    if not path_or_url:
        return "/"
    str_val = str(path_or_url).strip()
    if str_val.startswith(("http://", "https://", "//")):
        str_val = urlsplit(str_val).path or "/"
    if not str_val.startswith("/"):
        str_val = "/" + str_val
    return str_val


def is_google_cdn(url_str: str) -> bool:
    """
    Check if upstream URL belongs to Google CDN, Google Drive, or Google Photos hosts.
    Domains: googleusercontent.com, googlevideo.com, drive.google.com, photos.google.com
    """
    if not url_str:
        return False
    u = str(url_str).lower()
    return any(domain in u for domain in (
        "googleusercontent.com",
        "googlevideo.com",
        "drive.google.com",
        "photos.google.com"
    ))


def infer_video_type(filename: str, current_type: Optional[str] = None) -> str:
    """
    Infer video MIME type from filename extension, defaulting to video/mp4 for Google CDN streams.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    ext_map = {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".avi": "video/x-msvideo",
        ".mov": "video/quicktime",
        ".ts": "video/mp2t",
        ".flv": "video/x-flv",
        ".wmv": "video/x-ms-wmv",
        ".asf": "video/x-ms-asf",
        ".ogv": "video/ogg",
        ".3gp": "video/3gpp"
    }
    if ext in ext_map:
        return ext_map[ext]
    if current_type and current_type.startswith("video/") and current_type != "video/octet-stream":
        return current_type
    return "video/mp4"

# Mount registry store
MOUNTS_DB_PATH = os.environ.get("MOUNTS_DB_PATH", os.path.join(os.path.dirname(__file__), "mounts.json"))


class MountManager:
    def __init__(self, db_path: Optional[str] = None):
        self.mounts: Dict[str, Dict[str, Any]] = {}
        self.db_path = db_path
        self.load()

    def get_path(self) -> str:
        return self.db_path or os.environ.get("MOUNTS_DB_PATH", MOUNTS_DB_PATH)

    def load(self, path: Optional[str] = None):
        target = path or self.get_path()
        if os.path.exists(target):
            try:
                with open(target, "r", encoding="utf-8") as f:
                    self.mounts = json.load(f)
            except Exception:
                self.mounts = {}
        else:
            self.mounts = {}

    def save(self, path: Optional[str] = None):
        target = path or self.get_path()
        try:
            parent_dir = os.path.dirname(target)
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(self.mounts, f, indent=2)
        except Exception:
            pass

    def add_mount(
        self,
        movie_id: str,
        filename: str,
        upstream_url: str,
        total_bytes: int,
        content_type: str,
        formatted_size: str,
        title: Optional[str] = None,
        probing: bool = False,
        probe_id: Optional[str] = None
    ) -> Dict[str, Any]:
        if is_google_cdn(upstream_url):
            raw_ct = content_type
            if not raw_ct or raw_ct.lower() in ("application/octet-stream", "application/x-octet-stream", "binary/octet-stream", "unknown"):
                content_type = infer_video_type(filename)
            elif not raw_ct.lower().startswith("video/"):
                content_type = infer_video_type(filename)
            elif raw_ct == "video/x-matroska" and not filename.lower().endswith((".mkv", ".webm")):
                content_type = infer_video_type(filename)

        mount_data = {
            "id": movie_id,
            "filename": filename,
            "title": title or filename,
            "upstream_url": upstream_url,
            "total_bytes": total_bytes,
            "content_type": content_type,
            "formatted_size": formatted_size,
            "probing": probing,
            "probe_id": probe_id,
            "created_at": time.time(),
            "last_accessed": time.time()
        }
        self.mounts[filename] = mount_data
        self.save()
        if (
            not probing
            and total_bytes > 65536
            and upstream_url
            and (filename.lower().endswith(".mkv") or content_type == "video/x-matroska")
        ):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(warm_mkv_tail(upstream_url, total_bytes))
            except RuntimeError:
                pass
        return mount_data

    def update_mount(self, filename: str, **kwargs) -> Optional[Dict[str, Any]]:
        mount = self.mounts.get(filename)
        if mount:
            mount.update(kwargs)
            mount["last_accessed"] = time.time()
            self.save()
            total_bytes = mount.get("total_bytes", 0)
            upstream_url = mount.get("upstream_url", "")
            content_type = mount.get("content_type", "")
            probing = mount.get("probing", False)
            if (
                not probing
                and total_bytes > 65536
                and upstream_url
                and (filename.lower().endswith(".mkv") or content_type == "video/x-matroska")
            ):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(warm_mkv_tail(upstream_url, total_bytes))
                except RuntimeError:
                    pass
            return mount
        return None

    def get_by_filename(self, filename: str) -> Optional[Dict[str, Any]]:
        return self.mounts.get(filename)

    def get_mount(self, filename: str) -> Optional[Dict[str, Any]]:
        return self.mounts.get(filename)

    def remove_mount(self, filename: str) -> bool:
        if filename in self.mounts:
            del self.mounts[filename]
            self.save()
            return True
        return False

    def clear_all(self) -> int:
        count = len(self.mounts)
        self.mounts.clear()
        self.save()
        return count

    def list_all(self):
        return list(self.mounts.values())


mount_manager = MountManager()


def format_http_date(timestamp: float) -> str:
    """Format Unix timestamp as RFC 1123 HTTP date."""
    return email.utils.formatdate(timestamp, usegmt=True)


def _build_file_propstat(mount: Dict[str, Any], base_path: str) -> list:
    base_path = clean_relative_path(base_path)
    filename = mount["filename"]
    safe_filename = xml_escape(filename)
    total_bytes = mount.get("total_bytes", 0)
    content_type = mount.get("content_type", "video/x-matroska")
    upstream_url = mount.get("upstream_url", "")
    if is_google_cdn(upstream_url):
        raw_ct = mount.get("content_type")
        if not raw_ct or raw_ct.lower() in ("application/octet-stream", "application/x-octet-stream", "binary/octet-stream", "unknown"):
            content_type = infer_video_type(filename)
        elif not raw_ct.lower().startswith("video/"):
            content_type = infer_video_type(filename)
        elif raw_ct == "video/x-matroska" and not filename.lower().endswith((".mkv", ".webm")):
            content_type = infer_video_type(filename)
    created_at = mount.get("created_at", time.time())
    mod_date = format_http_date(created_at)
    iso_creation_date = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(created_at))
    raw_href = f"{base_path.rstrip('/')}/{quote(filename)}"
    safe_href = xml_escape(raw_href)
    etag = f'"{abs(hash(raw_href))}"'

    return [
        '  <D:response>',
        f'    <D:href>{safe_href}</D:href>',
        '    <D:propstat>',
        '      <D:prop>',
        f'        <D:displayname>{safe_filename}</D:displayname>',
        '        <D:resourcetype/>',
        f'        <D:getcontentlength>{total_bytes}</D:getcontentlength>',
        f'        <D:getcontenttype>{content_type}</D:getcontenttype>',
        f'        <D:getlastmodified>{mod_date}</D:getlastmodified>',
        f'        <D:getetag>{etag}</D:getetag>',
        f'        <D:creationdate>{iso_creation_date}</D:creationdate>',
        '        <D:supportedlock>',
        '          <D:lockentry>',
        '            <D:lockscope><D:exclusive/></D:lockscope>',
        '            <D:locktype><D:write/></D:locktype>',
        '          </D:lockentry>',
        '        </D:supportedlock>',
        '      </D:prop>',
        '      <D:status>HTTP/1.1 200 OK</D:status>',
        '    </D:propstat>',
        '  </D:response>'
    ]


def build_propfind_xml(base_path: str, depth: str = "1", target_file: Optional[Dict[str, Any]] = None) -> str:
    """
    Generates standard WebDAV XML Multi-Status response (RFC 4918).
    CX File Explorer uses this to display folder contents and file sizes.
    Ensures all <D:href> values use clean relative paths (/dav/...).
    """
    base_path = clean_relative_path(base_path)
    if target_file is not None:
        xml_lines = [
            '<?xml version="1.0" encoding="utf-8"?>',
            '<D:multistatus xmlns:D="DAV:">'
        ]
        xml_lines.extend(_build_file_propstat(target_file, base_path))
        xml_lines.append('</D:multistatus>')
        return "\n".join(xml_lines)

    now_ts = time.time()
    now_date = format_http_date(now_ts)
    iso_creation_date = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now_ts))
    etag = f'"{abs(hash(base_path))}"'
    safe_base_path = xml_escape(base_path)

    xml_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<D:multistatus xmlns:D="DAV:">',
        '  <D:response>',
        f'    <D:href>{safe_base_path}</D:href>',
        '    <D:propstat>',
        '      <D:prop>',
        '        <D:displayname>CloudStream Movies</D:displayname>',
        '        <D:resourcetype><D:collection/></D:resourcetype>',
        f'        <D:getlastmodified>{now_date}</D:getlastmodified>',
        f'        <D:getetag>{etag}</D:getetag>',
        f'        <D:creationdate>{iso_creation_date}</D:creationdate>',
        '      </D:prop>',
        '      <D:status>HTTP/1.1 200 OK</D:status>',
        '    </D:propstat>',
        '  </D:response>'
    ]

    # If depth > 0, include virtual movie files
    if depth != "0":
        for mount in mount_manager.list_all():
            xml_lines.extend(_build_file_propstat(mount, base_path))

    xml_lines.append('</D:multistatus>')
    return "\n".join(xml_lines)


async def handle_webdav_request(request: Request, path: str) -> Response:
    """
    Master WebDAV request handler routing OPTIONS, PROPFIND, HEAD, GET.
    """
    method = request.method.upper()
    path = path.strip("/")

    # Dynamically determine base_dav_path ensuring clean relative path (/dav/ or /)
    req_path = clean_relative_path(str(request.url.path))
    raw_path = req_path.rstrip("/") + "/"
    base_dav_path = raw_path if not path else "/" + "/".join(req_path.strip("/").split("/")[:-1]).rstrip("/") + "/"
    if req_path == "/" or base_dav_path == "//":
        base_dav_path = "/"

    # 0. Basic Authentication Validation (Optional via env)
    required_user = os.environ.get("WEBDAV_USER")
    required_pass = os.environ.get("WEBDAV_PASS")
    if required_user and required_pass:
        auth_header = request.headers.get("Authorization", "")
        authorized = False
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8", errors="ignore")
                if ":" in decoded:
                    u, p = decoded.split(":", 1)
                    if u == required_user and p == required_pass:
                        authorized = True
            except Exception:
                pass
        if not authorized:
            return Response(
                content="Authentication required.",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="CloudStream WebDAV Bridge"'}
            )

    # 1. OPTIONS Method
    if method == "OPTIONS":
        return Response(
            content="",
            status_code=200,
            headers={
                "DAV": "1",
                "MS-Author-Via": "DAV",
                "Allow": "OPTIONS, GET, HEAD, PROPFIND, PROPPATCH, DELETE",
                "Accept-Ranges": "bytes",
                "Content-Length": "0"
            }
        )

    # 2. PROPFIND Method (Directory & File Metadata Listing)
    if method == "PROPFIND":
        if path:
            filename = unquote(path.split("/")[-1])
            mount = mount_manager.get_by_filename(filename)
            if not mount:
                return PlainTextResponse(f"File '{filename}' not found on WebDAV bridge.", status_code=404)

            # WebDAV Probe Shield: Hold client request if stream is actively probing or total_bytes <= 0
            if mount.get("probing", False) or mount.get("total_bytes", 0) <= 0:
                probe_id = mount.get("probe_id")
                if probe_id:
                    evt = get_probe_event(probe_id)
                    if not evt.is_set():
                        try:
                            await asyncio.wait_for(evt.wait(), timeout=4.0)
                            refreshed = mount_manager.get_by_filename(filename)
                            if refreshed:
                                mount = refreshed
                        except asyncio.TimeoutError:
                            return Response(
                                content='<?xml version="1.0" encoding="utf-8"?>\n<D:error xmlns:D="DAV:"><D:need-privileges/><message>CloudStream Probe Shield: Upstream probe in progress. Please retry in 2 seconds.</message></D:error>',
                                status_code=503,
                                headers={
                                    "Content-Type": 'application/xml; charset="utf-8"',
                                    "Retry-After": "2",
                                    "Cache-Control": "no-cache, no-store, must-revalidate"
                                }
                            )

            xml_resp = build_propfind_xml(base_dav_path, target_file=mount)
        else:
            depth = request.headers.get("Depth", "1")
            # WebDAV Probe Shield: If any mounted stream is currently probing, wait up to 3.0s for probes to complete
            probing_mounts = [m for m in mount_manager.list_all() if m.get("probing", False)]
            if probing_mounts:
                probe_evts = [get_probe_event(m["probe_id"]) for m in probing_mounts if m.get("probe_id")]
                if probe_evts:
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*(e.wait() for e in probe_evts if not e.is_set()), return_exceptions=True),
                            timeout=3.0
                        )
                    except asyncio.TimeoutError:
                        pass

            xml_resp = build_propfind_xml(base_dav_path, depth=depth)

        encoded_resp = xml_resp.encode("utf-8")
        return Response(
            content=encoded_resp,
            status_code=207,
            headers={
                "Content-Type": 'application/xml; charset="utf-8"',
                "DAV": "1",
                "Content-Length": str(len(encoded_resp))
            }
        )

    # 3. PROPPATCH Method (Property update acknowledgement)
    if method == "PROPPATCH":
        target_path = xml_escape(clean_relative_path(str(request.url.path)))
        proppatch_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<D:multistatus xmlns:D="DAV:">\n'
            '  <D:response>\n'
            f'    <D:href>{target_path}</D:href>\n'
            '    <D:propstat>\n'
            '      <D:prop/>\n'
            '      <D:status>HTTP/1.1 200 OK</D:status>\n'
            '    </D:propstat>\n'
            '  </D:response>\n'
            '</D:multistatus>'
        ).encode("utf-8")
        return Response(
            content=proppatch_xml,
            status_code=207,
            headers={
                "Content-Type": 'application/xml; charset="utf-8"',
                "DAV": "1",
                "Content-Length": str(len(proppatch_xml))
            }
        )

    # 4. Path resolution for specific file
    if not path:
        if method == "DELETE":
            return PlainTextResponse("Cannot delete WebDAV root collection.", status_code=403)
        # Requesting root /dav/ with GET or HEAD
        return PlainTextResponse("CloudStream WebDAV Bridge Active. Connect via CX File Explorer.", status_code=200)

    filename = unquote(path.split("/")[-1])
    mount = mount_manager.get_by_filename(filename)

    if not mount:
        return PlainTextResponse(f"File '{filename}' not found on WebDAV bridge.", status_code=404)

    # WebDAV Probe Shield: Asynchronously hold incoming HEAD or GET requests for up to 4 seconds,
    # or return HTTP 503 Retry-After so CX File Explorer never receives a 0-byte corrupt stream.
    if mount.get("probing", False) or mount.get("total_bytes", 0) <= 0:
        probe_id = mount.get("probe_id")
        if probe_id:
            evt = get_probe_event(probe_id)
            if not evt.is_set():
                try:
                    await asyncio.wait_for(evt.wait(), timeout=4.0)
                    refreshed = mount_manager.get_by_filename(filename)
                    if refreshed:
                        mount = refreshed
                except asyncio.TimeoutError:
                    return Response(
                        content="CloudStream Probe Shield: Upstream probe in progress. Please retry in 2 seconds.",
                        status_code=503,
                        headers={
                            "Retry-After": "2",
                            "Content-Type": "text/plain; charset=utf-8",
                            "Cache-Control": "no-cache, no-store, must-revalidate"
                        }
                    )

    total_bytes = mount.get("total_bytes", 0)
    content_type = mount.get("content_type", "video/x-matroska")
    upstream_url = mount.get("upstream_url", "")
    mount["last_accessed"] = time.time()

    if is_google_cdn(upstream_url):
        raw_ct = mount.get("content_type")
        if not raw_ct or raw_ct.lower() in ("application/octet-stream", "application/x-octet-stream", "binary/octet-stream", "unknown"):
            content_type = infer_video_type(filename)
        elif not raw_ct.lower().startswith("video/"):
            content_type = infer_video_type(filename)
        elif raw_ct == "video/x-matroska" and not filename.lower().endswith((".mkv", ".webm")):
            content_type = infer_video_type(filename)

    # 4. HEAD Method
    if method == "HEAD":
        if not is_google_cdn(upstream_url) and request.query_params.get("redirect") == "1" and upstream_url:
            return RedirectResponse(
                url=upstream_url,
                status_code=302,
                headers={
                    "Location": upstream_url,
                    "Accept-Ranges": "bytes",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Expose-Headers": "Location, Content-Range, Accept-Ranges",
                    "Cache-Control": "no-cache, no-store, must-revalidate"
                }
            )
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": content_type,
            "Last-Modified": format_http_date(mount.get("created_at", time.time())),
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "Location, Content-Range, Accept-Ranges, Content-Length",
            "Cache-Control": "no-cache, no-store, must-revalidate"
        }
        if total_bytes > 0:
            headers["Content-Length"] = str(total_bytes)
        return Response(status_code=200, headers=headers)

    # 5. GET Method (with 302 Redirect Bypass & Range Proxy Fallback)
    if method == "GET":
        force_proxy = (
            request.query_params.get("proxy") == "1"
            or os.environ.get("WEBDAV_DIRECT_REDIRECT") == "0"
            or is_google_cdn(upstream_url)
        )
        if not force_proxy and upstream_url:
            return RedirectResponse(
                url=upstream_url,
                status_code=302,
                headers={
                    "Location": upstream_url,
                    "Accept-Ranges": "bytes",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Expose-Headers": "Location, Content-Range, Accept-Ranges",
                    "Cache-Control": "private, max-age=1800, stale-while-revalidate=300",
                    "Vary": "Range",
                    "Keep-Alive": "timeout=60, max=1000"
                }
            )

        range_header = request.headers.get("Range")
        return await stream_range_proxy(
            upstream_url=upstream_url,
            range_header=range_header,
            total_size=total_bytes,
            content_type=content_type
        )

    # 6. DELETE Method (RFC 4918 File Unmounting)
    if method == "DELETE":
        if not path:
            return PlainTextResponse("Cannot delete WebDAV root collection.", status_code=403)
        filename = unquote(path.split("/")[-1])
        removed = mount_manager.remove_mount(filename)
        if removed:
            return Response(status_code=204)
        return PlainTextResponse(f"File '{filename}' not found on WebDAV bridge.", status_code=404)

    return PlainTextResponse(f"Method {method} not supported on WebDAV bridge.", status_code=405)
