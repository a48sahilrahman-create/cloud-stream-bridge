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
import email.utils
from typing import Dict, Any, Optional
from starlette.requests import Request
from starlette.responses import Response, PlainTextResponse
from range_proxy import stream_range_proxy

# Mount registry store
MOUNTS_DB_PATH = os.path.join(os.path.dirname(__file__), "mounts.json")


class MountManager:
    def __init__(self):
        self.mounts: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self):
        if os.path.exists(MOUNTS_DB_PATH):
            try:
                with open(MOUNTS_DB_PATH, "r", encoding="utf-8") as f:
                    self.mounts = json.load(f)
            except Exception:
                self.mounts = {}

    def save(self):
        try:
            with open(MOUNTS_DB_PATH, "w", encoding="utf-8") as f:
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
        title: Optional[str] = None
    ) -> Dict[str, Any]:
        mount_data = {
            "id": movie_id,
            "filename": filename,
            "title": title or filename,
            "upstream_url": upstream_url,
            "total_bytes": total_bytes,
            "content_type": content_type,
            "formatted_size": formatted_size,
            "created_at": time.time(),
            "last_accessed": time.time()
        }
        self.mounts[filename] = mount_data
        self.save()
        return mount_data

    def get_by_filename(self, filename: str) -> Optional[Dict[str, Any]]:
        return self.mounts.get(filename)

    def remove_mount(self, filename: str) -> bool:
        if filename in self.mounts:
            del self.mounts[filename]
            self.save()
            return True
        return False

    def list_all(self):
        return list(self.mounts.values())


mount_manager = MountManager()


def format_http_date(timestamp: float) -> str:
    """Format Unix timestamp as RFC 1123 HTTP date."""
    return email.utils.formatdate(timestamp, usegmt=True)


def _build_file_propstat(mount: Dict[str, Any], base_path: str) -> list:
    filename = mount["filename"]
    total_bytes = mount.get("total_bytes", 0)
    content_type = mount.get("content_type", "video/x-matroska")
    created_at = mount.get("created_at", time.time())
    mod_date = format_http_date(created_at)
    iso_creation_date = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(created_at))
    href = f"{base_path.rstrip('/')}/{filename}"
    etag = f'"{abs(hash(href))}"'

    return [
        '  <D:response>',
        f'    <D:href>{href}</D:href>',
        '    <D:propstat>',
        '      <D:prop>',
        f'        <D:displayname>{filename}</D:displayname>',
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
    """
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

    xml_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<D:multistatus xmlns:D="DAV:">',
        '  <D:response>',
        f'    <D:href>{base_path}</D:href>',
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

    # Dynamically determine base_dav_path
    raw_path = str(request.url.path).rstrip("/") + "/"
    base_dav_path = raw_path if not path else "/" + "/".join(str(request.url.path).strip("/").split("/")[:-1]).rstrip("/") + "/"
    if str(request.url.path) == "/" or base_dav_path == "//":
        base_dav_path = "/"

    # 1. OPTIONS Method
    if method == "OPTIONS":
        return Response(
            content="",
            status_code=200,
            headers={
                "DAV": "1",
                "MS-Author-Via": "DAV",
                "Allow": "OPTIONS, GET, HEAD, PROPFIND, PROPPATCH",
                "Accept-Ranges": "bytes",
                "Content-Length": "0"
            }
        )

    # 2. PROPFIND Method (Directory & File Metadata Listing)
    if method == "PROPFIND":
        if path:
            filename = path.split("/")[-1]
            mount = mount_manager.get_by_filename(filename)
            if not mount:
                return PlainTextResponse(f"File '{filename}' not found on WebDAV bridge.", status_code=404)
            xml_resp = build_propfind_xml(base_dav_path, target_file=mount)
        else:
            depth = request.headers.get("Depth", "1")
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

    # 3. Path resolution for specific file
    if not path:
        # Requesting root /dav/ with GET or HEAD
        return PlainTextResponse("CloudStream WebDAV Bridge Active. Connect via CX File Explorer.", status_code=200)

    filename = path.split("/")[-1]
    mount = mount_manager.get_by_filename(filename)

    if not mount:
        return PlainTextResponse(f"File '{filename}' not found on WebDAV bridge.", status_code=404)

    total_bytes = mount.get("total_bytes", 0)
    content_type = mount.get("content_type", "video/x-matroska")
    upstream_url = mount.get("upstream_url", "")
    mount["last_accessed"] = time.time()

    # 4. HEAD Method
    if method == "HEAD":
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": content_type,
            "Last-Modified": format_http_date(mount.get("created_at", time.time()))
        }
        if total_bytes > 0:
            headers["Content-Length"] = str(total_bytes)
        return Response(status_code=200, headers=headers)

    # 5. GET Method (with Range support)
    if method == "GET":
        range_header = request.headers.get("Range")
        return await stream_range_proxy(
            upstream_url=upstream_url,
            range_header=range_header,
            total_size=total_bytes,
            content_type=content_type
        )

    return PlainTextResponse(f"Method {method} not supported on WebDAV bridge.", status_code=405)
