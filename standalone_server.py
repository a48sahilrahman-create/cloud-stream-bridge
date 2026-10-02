"""
CloudStream WebDAV Bridge - Single-File Autonomous Server
Everything bundled into a single file: WebDAV RFC 4918 engine, Range Proxy,
Container Magic Bytes Detector, FastAPI REST routes, and embedded Web UI.
Can be executed anywhere (Colab, Linux, Windows, Mac, VPS, Docker) with zero external template files.
"""

import os
import sys
import time
import json
import re
import asyncio
import logging
import email.utils
from urllib.parse import urlparse, parse_qs, unquote
from typing import Dict, Any, Optional, Tuple, AsyncGenerator
import httpx
from pydantic import BaseModel
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cloudstream")

# Configuration constants
CHUNK_SIZE = 128 * 1024  # 128 KB buffer chunks
PROBE_TIMEOUT = 5.0
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
MOUNTS_DB_PATH = "mounts.json"

# Telemetry
telemetry_stats = {
    "total_bytes_streamed": 0,
    "total_requests_served": 0,
    "active_streams": 0
}

# --- 1. STREAM PROBE ---
def sanitize_filename(name: str) -> str:
    s = re.sub(r'[\\/*?:"<>|]', "", name)
    s = s.strip().replace(" ", "_")
    return s or "movie_stream.mkv"

async def probe_stream(url: str, custom_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    t0 = time.perf_counter()
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Range": "bytes=0-8191"
    }
    if custom_headers:
        headers.update(custom_headers)

    client_kwargs = {"timeout": PROBE_TIMEOUT, "follow_redirects": True, "verify": False}

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
                status_code = resp.status_code
                content_range = resp.headers.get("content-range", "")
                content_length = resp.headers.get("content-length", "")
                content_type = resp.headers.get("content-type", "").lower()
                final_url = str(resp.url)

                data = b""
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) >= 8192:
                        break

            total_bytes = 0
            range_supported = (status_code == 206) or ("bytes" in resp.headers.get("accept-ranges", "").lower())

            if content_range and "/" in content_range:
                try:
                    total_bytes = int(content_range.split("/")[-1])
                    range_supported = True
                except ValueError:
                    pass
            elif content_length and content_length.isdigit() and status_code == 200:
                total_bytes = int(content_length)

            if total_bytes == 0 and status_code in [200, 206]:
                try:
                    async with httpx.AsyncClient(**client_kwargs) as client:
                        head_resp = await client.head(url, headers={"User-Agent": DEFAULT_USER_AGENT})
                        if head_resp.status_code in [200, 206]:
                            h_len = head_resp.headers.get("content-length", "")
                            if h_len and h_len.isdigit():
                                total_bytes = int(h_len)
                            if "bytes" in head_resp.headers.get("accept-ranges", "").lower():
                                range_supported = True
                except Exception:
                    pass

            is_mkv = data.startswith(b'\x1a\x45\xdf\xa3')
            is_mp4 = (
                (len(data) >= 8 and data[4:8] == b'ftyp') or
                (b'moov' in data[:8192]) or
                (b'mdat' in data[:8192] and any(a in data[:8192] for a in [b'ftyp', b'wide', b'free', b'skip']))
            )
            is_ts = data.startswith(b'\x47') or ("video/mp2t" in content_type)
            is_avi = data.startswith(b'RIFF') and (b'AVI ' in data[:16])

            if is_mkv:
                container_format = "MKV (Matroska / 4K UHD Remux)"
                mime = "video/x-matroska"
                ext = ".mkv"
            elif is_mp4:
                container_format = "MP4 (ISOBMFF / H.264 / HEVC)"
                mime = "video/mp4"
                ext = ".mp4"
            elif is_ts:
                container_format = "MPEG-TS Stream"
                mime = "video/mp2t"
                ext = ".ts"
            elif is_avi:
                container_format = "AVI Container"
                mime = "video/x-msvideo"
                ext = ".avi"
            else:
                container_format = "Direct Video Stream"
                mime = content_type if "video" in content_type else "video/x-matroska"
                ext = ".mkv"

            if total_bytes >= 1024**3:
                formatted_size = f"{round(total_bytes / (1024**3), 2)} GB"
            elif total_bytes >= 1024**2:
                formatted_size = f"{round(total_bytes / (1024**2), 2)} MB"
            else:
                formatted_size = f"{total_bytes} bytes" if total_bytes > 0 else "Dynamic Stream"

            # Presigned R2/S3 and upstream filename extraction
            extracted_filename = None
            for check_u in (final_url, url):
                try:
                    pq = parse_qs(urlparse(check_u).query)
                    rcd = pq.get("response-content-disposition", [None])[0]
                    if rcd:
                        rcd_unquoted = unquote(rcd)
                        m_cd = re.search(r'''filename[*]?=(?:UTF-8'')?["']?([^"';]+)["']?''', rcd_unquoted, re.IGNORECASE)
                        if m_cd:
                            extracted_filename = unquote(m_cd.group(1)).strip().strip('"\'')
                            break
                except Exception:
                    pass

            if not extracted_filename:
                cd_header = resp.headers.get("content-disposition", "")
                if cd_header:
                    cd_unquoted = unquote(cd_header)
                    m_cd = re.search(r'''filename[*]?=(?:UTF-8'')?["']?([^"';]+)["']?''', cd_unquoted, re.IGNORECASE)
                    if m_cd:
                        extracted_filename = unquote(m_cd.group(1)).strip().strip('"\'')

            if extracted_filename:
                url_filename = sanitize_filename(extracted_filename)
                if not any(url_filename.lower().endswith(e) for e in [".mkv", ".mp4", ".ts", ".avi", ".mov", ".webm"]):
                    url_filename += ext
            else:
                url_filename = final_url.split("?")[0].split("/")[-1]
                if not url_filename or "." not in url_filename:
                    url_filename = f"stream_{int(time.time())}{ext}"
                else:
                    url_filename = sanitize_filename(url_filename)
                    if not any(url_filename.lower().endswith(e) for e in [".mkv", ".mp4", ".ts", ".avi", ".mov", ".webm"]):
                        url_filename += ext

            is_valid = (status_code in [200, 206]) and (total_bytes > 0 or range_supported)
            return {
                "valid": is_valid,
                "status_code": status_code,
                "range_supported": range_supported,
                "container_format": container_format,
                "content_type": mime,
                "total_bytes": total_bytes,
                "formatted_size": formatted_size,
                "default_filename": url_filename,
                "final_url": final_url,
                "elapsed_ms": elapsed_ms,
                "error": None if is_valid else f"HTTP {status_code} received from upstream"
            }
    except Exception as e:
        return {
            "valid": False, "status_code": 0, "range_supported": False,
            "container_format": "Unknown", "content_type": "video/x-matroska",
            "total_bytes": 0, "formatted_size": "Unknown Size",
            "default_filename": f"stream_{int(time.time())}.mkv",
            "final_url": url, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "error": str(e)
        }

# --- 2. RANGE PROXY ---
def parse_byte_range(range_header: Optional[str], total_size: int) -> Tuple[int, int]:
    if not range_header or "=" not in range_header:
        return 0, max(0, total_size - 1)
    units, spec = range_header.split("=", 1)
    if units.strip().lower() != "bytes":
        return 0, max(0, total_size - 1)
    spec = spec.strip().split(",")[0]
    parts = spec.split("-")
    if len(parts) != 2:
        return 0, max(0, total_size - 1)
    start_str, end_str = parts[0].strip(), parts[1].strip()
    if start_str and end_str:
        start = int(start_str)
        end = int(end_str)
    elif start_str:
        start = int(start_str)
        end = max(0, total_size - 1)
    elif end_str:
        suffix = int(end_str)
        start = max(0, total_size - suffix)
        end = max(0, total_size - 1)
    else:
        start = 0
        end = max(0, total_size - 1)
    if total_size > 0:
        end = min(end, total_size - 1)
        start = min(start, end)
    return start, end

async def stream_range_proxy(
    upstream_url: str,
    range_header: Optional[str],
    total_size: int,
    content_type: str,
    custom_headers: Optional[Dict[str, str]] = None
) -> Response:
    telemetry_stats["total_requests_served"] += 1
    start, end = parse_byte_range(range_header, total_size)
    content_length = (end - start + 1) if (end >= start and total_size > 0) else None

    upstream_req_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Range": f"bytes={start}-{end}"
    }
    if custom_headers:
        upstream_req_headers.update(custom_headers)

    response_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": content_type or "video/x-matroska",
    }
    status_code = 200
    if range_header:
        status_code = 206
        if total_size > 0:
            response_headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
        if content_length is not None:
            response_headers["Content-Length"] = str(content_length)
    elif content_length is not None:
        response_headers["Content-Length"] = str(content_length)

    async def chunk_generator() -> AsyncGenerator[bytes, None]:
        telemetry_stats["active_streams"] += 1
        client = httpx.AsyncClient(timeout=30.0, follow_redirects=True, verify=False)
        try:
            async with client.stream("GET", upstream_url, headers=upstream_req_headers) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                    telemetry_stats["total_bytes_streamed"] += len(chunk)
                    yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            logger.info(f"Client disconnected / scrubbed: range {start}-{end}")
        except Exception as e:
            logger.error(f"Upstream stream error: {e}")
        finally:
            telemetry_stats["active_streams"] = max(0, telemetry_stats["active_streams"] - 1)
            await client.aclose()

    return StreamingResponse(chunk_generator(), status_code=status_code, headers=response_headers)

# --- 3. WEBDAV ENGINE ---
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

    def add_mount(self, movie_id: str, filename: str, upstream_url: str, total_bytes: int, content_type: str, formatted_size: str, title: Optional[str] = None) -> Dict[str, Any]:
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
    return email.utils.formatdate(timestamp, usegmt=True)

def build_propfind_xml(base_path: str, depth: str = "1", target_file: Optional[Dict[str, Any]] = None) -> str:
    now_ts = time.time()
    now_date = format_http_date(now_ts)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts))

    if target_file:
        filename = target_file["filename"]
        total_bytes = target_file.get("total_bytes", 0)
        content_type = target_file.get("content_type", "video/x-matroska")
        created_at = target_file.get("created_at", now_ts)
        mod_date = format_http_date(created_at)
        creation_date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_at))
        file_id = target_file.get("id", filename)
        etag = f'"{file_id}-{total_bytes}"'
        href = base_path

        xml_lines = [
            '<?xml version="1.0" encoding="utf-8"?>',
            '<D:multistatus xmlns:D="DAV:">',
            '  <D:response>',
            f'    <D:href>{href}</D:href>',
            '    <D:propstat>',
            '      <D:prop>',
            f'        <D:displayname>{filename}</D:displayname>',
            '        <D:resourcetype/>',
            f'        <D:getcontentlength>{total_bytes}</D:getcontentlength>',
            f'        <D:getcontenttype>{content_type}</D:getcontenttype>',
            f'        <D:getlastmodified>{mod_date}</D:getlastmodified>',
            f'        <D:creationdate>{creation_date}</D:creationdate>',
            f'        <D:getetag>{etag}</D:getetag>',
            '      </D:prop>',
            '      <D:status>HTTP/1.1 200 OK</D:status>',
            '    </D:propstat>',
            '  </D:response>',
            '</D:multistatus>'
        ]
        return "\n".join(xml_lines)

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
        f'        <D:creationdate>{now_iso}</D:creationdate>',
        '      </D:prop>',
        '      <D:status>HTTP/1.1 200 OK</D:status>',
        '    </D:propstat>',
        '  </D:response>'
    ]
    if depth != "0":
        for mount in mount_manager.list_all():
            filename = mount["filename"]
            total_bytes = mount.get("total_bytes", 0)
            content_type = mount.get("content_type", "video/x-matroska")
            created_at = mount.get("created_at", now_ts)
            mod_date = format_http_date(created_at)
            creation_date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_at))
            file_id = mount.get("id", filename)
            etag = f'"{file_id}-{total_bytes}"'
            href = f"{base_path.rstrip('/')}/{filename}"
            xml_lines.extend([
                '  <D:response>',
                f'    <D:href>{href}</D:href>',
                '    <D:propstat>',
                '      <D:prop>',
                f'        <D:displayname>{filename}</D:displayname>',
                '        <D:resourcetype/>',
                f'        <D:getcontentlength>{total_bytes}</D:getcontentlength>',
                f'        <D:getcontenttype>{content_type}</D:getcontenttype>',
                f'        <D:getlastmodified>{mod_date}</D:getlastmodified>',
                f'        <D:creationdate>{creation_date}</D:creationdate>',
                f'        <D:getetag>{etag}</D:getetag>',
                '      </D:prop>',
                '      <D:status>HTTP/1.1 200 OK</D:status>',
                '    </D:propstat>',
                '  </D:response>'
            ])
    xml_lines.append('</D:multistatus>')
    return "\n".join(xml_lines)

async def handle_webdav_request(request: Request, path: str) -> Response:
    method = request.method.upper()
    path = path.strip("/")
    base_dav_path = request.url.path or "/"

    if method == "OPTIONS":
        return Response(content="", status_code=200, headers={
            "DAV": "1", "MS-Author-Via": "DAV", "Allow": "OPTIONS, GET, HEAD, PROPFIND, PROPPATCH",
            "Accept-Ranges": "bytes", "Content-Length": "0"
        })

    if not path:
        if method == "PROPFIND":
            depth = request.headers.get("Depth", "1")
            xml_resp = build_propfind_xml(base_dav_path, depth=depth)
            xml_bytes = xml_resp.encode("utf-8")
            return Response(content=xml_bytes, status_code=207, headers={
                "Content-Type": 'application/xml; charset="utf-8"',
                "DAV": "1",
                "Content-Length": str(len(xml_bytes))
            })
        return PlainTextResponse("CloudStream WebDAV Bridge Active. Connect via CX File Explorer.", status_code=200)

    filename = path.split("/")[-1]
    mount = mount_manager.get_by_filename(filename)
    if not mount:
        return PlainTextResponse(f"File '{filename}' not found on WebDAV bridge.", status_code=404)

    if method == "PROPFIND":
        depth = request.headers.get("Depth", "0")
        xml_resp = build_propfind_xml(base_dav_path, depth=depth, target_file=mount)
        xml_bytes = xml_resp.encode("utf-8")
        return Response(content=xml_bytes, status_code=207, headers={
            "Content-Type": 'application/xml; charset="utf-8"',
            "DAV": "1",
            "Content-Length": str(len(xml_bytes))
        })

    total_bytes = mount.get("total_bytes", 0)
    content_type = mount.get("content_type", "video/x-matroska")
    upstream_url = mount.get("upstream_url", "")
    mount["last_accessed"] = time.time()

    if method == "HEAD":
        headers = {
            "Accept-Ranges": "bytes", "Content-Type": content_type,
            "Last-Modified": format_http_date(mount.get("created_at", time.time()))
        }
        if total_bytes > 0:
            headers["Content-Length"] = str(total_bytes)
        return Response(status_code=200, headers=headers)

    if method == "GET":
        range_header = request.headers.get("Range")
        return await stream_range_proxy(upstream_url, range_header, total_bytes, content_type)

    return PlainTextResponse(f"Method {method} not supported.", status_code=405)

# --- 4. FASTAPI APPLICATION ---
app = FastAPI(title="CloudStream WebDAV Bridge", redirect_slashes=False)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Embedded full responsive dark-mode HTML UI (zero external file dependency)
import base64
_HTML_B64 = "PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CiAgPG1ldGEgY2hhcnNldD0iVVRGLTgiPgogIDxtZXRhIG5hbWU9InZpZXdwb3J0IiBjb250ZW50PSJ3aWR0aD1kZXZpY2Utd2lkdGgsIGluaXRpYWwtc2NhbGU9MS4wIj4KICA8dGl0bGU+Q2xvdWRTdHJlYW0gV2ViREFWIEJyaWRnZTwvdGl0bGU+CiAgPGxpbmsgcmVsPSJpY29uIiBocmVmPSJkYXRhOmltYWdlL3N2Zyt4bWwsPHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHZpZXdCb3g9JzAgMCAyNCAyNCcgZmlsbD0nJTIzNjM2NmYxJz48cGF0aCBkPSdNMTkuMzUgMTAuMDRDMTguNjcgNi41OSAxNS42NCA0IDEyIDQgOS4xMSA0IDYuNiA1LjY0IDUuMzUgOC4wNCAyLjM0IDguMzYgMCAxMC45MSAwIDE0YzAgMy4zMSAyLjY5IDYgNiA2aDEzYzIuNzYgMCA1LTIuMjQgNS01IDAtMi42NC0yLjA1LTQuNzgtNC42NS00Ljk2ek0xMCAxN2wtNS01IDEuNDEtMS40MUwxMCAxNC4xN2w3LjU5LTcuNTlMMTkgOGwtOSA5eicvPjwvc3ZnPiI+CiAgPHNjcmlwdCBzcmM9Imh0dHBzOi8vY2RuLnRhaWx3aW5kY3NzLmNvbSI+PC9zY3JpcHQ+CiAgPHNjcmlwdCBzcmM9Imh0dHBzOi8vY2RuanMuY2xvdWRmbGFyZS5jb20vYWpheC9saWJzL3FyY29kZWpzLzEuMC4wL3FyY29kZS5taW4uanMiPjwvc2NyaXB0PgogIDxzdHlsZT4KICAgIEBrZXlmcmFtZXMgcHVsc2Utc3VidGxlIHsgMCUsIDEwMCUgeyBvcGFjaXR5OiAxOyB9IDUwJSB7IG9wYWNpdHk6IDAuNjsgfSB9CiAgICAuYW5pbWF0ZS1zdWJ0bGUgeyBhbmltYXRpb246IHB1bHNlLXN1YnRsZSAycyBpbmZpbml0ZSBlYXNlLWluLW91dDsgfQogICAgYm9keSB7IGJhY2tncm91bmQtY29sb3I6ICMwYjBmMTk7IGZvbnQtZmFtaWx5OiAtYXBwbGUtc3lzdGVtLCBCbGlua01hY1N5c3RlbUZvbnQsICJTZWdvZSBVSSIsIFJvYm90bywgc2Fucy1zZXJpZjsgfQogIDwvc3R5bGU+CjwvaGVhZD4KPGJvZHkgY2xhc3M9InRleHQtc2xhdGUtMTAwIG1pbi1oLXNjcmVlbiBwLTQgc206cC02IGxnOnAtOCI+CiAgPGRpdiBjbGFzcz0ibWF4LXctNXhsIG14LWF1dG8gc3BhY2UteS02Ij4KCiAgICA8IS0tIFRvcCBOYXZpZ2F0aW9uICYgSGVybyBCYW5uZXIgLS0+CiAgICA8aGVhZGVyIGNsYXNzPSJmbGV4IGZsZXgtY29sIHNtOmZsZXgtcm93IGl0ZW1zLXN0YXJ0IHNtOml0ZW1zLWNlbnRlciBqdXN0aWZ5LWJldHdlZW4gcGItNiBib3JkZXItYiBib3JkZXItc2xhdGUtODAwIGdhcC00Ij4KICAgICAgPGRpdiBjbGFzcz0iZmxleCBpdGVtcy1jZW50ZXIgc3BhY2UteC0zIj4KICAgICAgICA8ZGl2IGNsYXNzPSJ3LTExIGgtMTEgcm91bmRlZC14bCBiZy1ncmFkaWVudC10by10ciBmcm9tLWluZGlnby02MDAgdG8tY3lhbi00MDAgZmxleCBpdGVtcy1jZW50ZXIganVzdGlmeS1jZW50ZXIgc2hhZG93LWxnIHNoYWRvdy1pbmRpZ28tNTAwLzIwIj4KICAgICAgICAgIDxzdmcgY2xhc3M9InctNiBoLTYgdGV4dC13aGl0ZSIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJjdXJyZW50Q29sb3IiIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIiBzdHJva2Utd2lkdGg9IjIiIGQ9Ik0xNC43NTIgMTEuMTY4bC0zLjE5Ny0yLjEzMkExIDEgMCAwMDEwIDkuODd2NC4yNjNhMSAxIDAgMDAxLjU1NS44MzJsMy4xOTctMi4xMzJhMSAxIDAgMDAwLTEuNjY0eiI+PC9wYXRoPjxwYXRoIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIgc3Ryb2tlLXdpZHRoPSIyIiBkPSJNMjEgMTJhOSA5IDAgMTEtMTggMCA5IDkgMCAwMTE4IDB6Ij48L3BhdGg+PC9zdmc+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdj4KICAgICAgICAgIDxoMSBjbGFzcz0idGV4dC14bCBzbTp0ZXh0LTJ4bCBmb250LWJvbGQgYmctZ3JhZGllbnQtdG8tciBmcm9tLXdoaXRlIHZpYS1zbGF0ZS0yMDAgdG8taW5kaWdvLTMwMCBiZy1jbGlwLXRleHQgdGV4dC10cmFuc3BhcmVudCI+CiAgICAgICAgICAgIENsb3VkU3RyZWFtIFdlYkRBViBCcmlkZ2UKICAgICAgICAgIDwvaDE+CiAgICAgICAgICA8cCBjbGFzcz0idGV4dC14cyBzbTp0ZXh0LXNtIHRleHQtc2xhdGUtNDAwIj5XYXRjaCAxMDAgR0IgUmVtdXhlcyBvbiA1MCBNYnBzIFdpLUZpIOKAoiAwIEJ5dGVzIExvY2FsIERpc2s8L3A+CiAgICAgICAgPC9kaXY+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJmbGV4IGl0ZW1zLWNlbnRlciBzcGFjZS14LTIgdGV4dC14cyBiZy1lbWVyYWxkLTk1MC82MCBib3JkZXIgYm9yZGVyLWVtZXJhbGQtNTAwLzMwIHRleHQtZW1lcmFsZC0zMDAgcHgtMyBweS0xLjUgcm91bmRlZC1mdWxsIGZvbnQtbWVkaXVtIj4KICAgICAgICA8c3BhbiBjbGFzcz0idy0yIGgtMiByb3VuZGVkLWZ1bGwgYmctZW1lcmFsZC00MDAgYW5pbWF0ZS1waW5nIj48L3NwYW4+CiAgICAgICAgPHNwYW4+QnJpZGdlIEFjdGl2ZSAmIFJlYWR5PC9zcGFuPgogICAgICA8L2Rpdj4KICAgIDwvaGVhZGVyPgoKICAgIDwhLS0gQmFuZHdpZHRoIFNhdmluZ3MgVGVsZW1ldHJ5IC0tPgogICAgPHNlY3Rpb24gY2xhc3M9ImdyaWQgZ3JpZC1jb2xzLTIgc206Z3JpZC1jb2xzLTQgZ2FwLTMgc206Z2FwLTQiPgogICAgICA8ZGl2IGNsYXNzPSJiZy1zbGF0ZS05MDAvODAgYm9yZGVyIGJvcmRlci1zbGF0ZS04MDAgcm91bmRlZC14bCBwLTQiPgogICAgICAgIDxkaXYgY2xhc3M9InRleHQteHMgdGV4dC1zbGF0ZS00MDAgZm9udC1tZWRpdW0iPlZpcnR1YWwgTGlicmFyeSBTaXplPC9kaXY+CiAgICAgICAgPGRpdiBpZD0ic3RhdC12aXJ0dWFsIiBjbGFzcz0idGV4dC14bCBzbTp0ZXh0LTJ4bCBmb250LWJvbGQgdGV4dC1pbmRpZ28tNDAwIG10LTEiPjAuMCBHQjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9InRleHQtWzEwcHhdIHRleHQtc2xhdGUtNTAwIG10LTAuNSI+TW91bnRlZCBjbG91ZCBkYXRhPC9kaXY+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJiZy1zbGF0ZS05MDAvODAgYm9yZGVyIGJvcmRlci1zbGF0ZS04MDAgcm91bmRlZC14bCBwLTQiPgogICAgICAgIDxkaXYgY2xhc3M9InRleHQteHMgdGV4dC1zbGF0ZS00MDAgZm9udC1tZWRpdW0iPkhvbWUgV2ktRmkgQ29uc3VtZWQ8L2Rpdj4KICAgICAgICA8ZGl2IGlkPSJzdGF0LXN0cmVhbWVkIiBjbGFzcz0idGV4dC14bCBzbTp0ZXh0LTJ4bCBmb250LWJvbGQgdGV4dC1jeWFuLTQwMCBtdC0xIj4wLjAgTUI8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJ0ZXh0LVsxMHB4XSB0ZXh0LXNsYXRlLTUwMCBtdC0wLjUiPk9ubHkgY2h1bmtzIHlvdSB3YXRjaGVkPC9kaXY+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJiZy1zbGF0ZS05MDAvODAgYm9yZGVyIGJvcmRlci1zbGF0ZS04MDAgcm91bmRlZC14bCBwLTQiPgogICAgICAgIDxkaXYgY2xhc3M9InRleHQteHMgdGV4dC1zbGF0ZS00MDAgZm9udC1tZWRpdW0iPkhvbWUgQmFuZHdpZHRoIFNhdmVkPC9kaXY+CiAgICAgICAgPGRpdiBpZD0ic3RhdC1zYXZlZCIgY2xhc3M9InRleHQteGwgc206dGV4dC0yeGwgZm9udC1ib2xkIHRleHQtZW1lcmFsZC00MDAgbXQtMSI+MC4wIEdCPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0idGV4dC1bMTBweF0gdGV4dC1zbGF0ZS01MDAgbXQtMC41Ij5TcGFyZWQgZnJvbSBkb3dubG9hZDwvZGl2PgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iYmctc2xhdGUtOTAwLzgwIGJvcmRlciBib3JkZXItc2xhdGUtODAwIHJvdW5kZWQteGwgcC00Ij4KICAgICAgICA8ZGl2IGNsYXNzPSJ0ZXh0LXhzIHRleHQtc2xhdGUtNDAwIGZvbnQtbWVkaXVtIj5BY3RpdmUgU3RyZWFtczwvZGl2PgogICAgICAgIDxkaXYgaWQ9InN0YXQtc3RyZWFtcyIgY2xhc3M9InRleHQteGwgc206dGV4dC0yeGwgZm9udC1ib2xkIHRleHQtcHVycGxlLTQwMCBtdC0xIj4wPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0idGV4dC1bMTBweF0gdGV4dC1zbGF0ZS01MDAgbXQtMC41Ij5MaXZlIHNlZWtpbmcgc29ja2V0czwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvc2VjdGlvbj4KCiAgICA8IS0tIE1haW4gTW91bnRpbmcgQ2FyZCAtLT4KICAgIDxzZWN0aW9uIGNsYXNzPSJiZy1zbGF0ZS05MDAvOTAgYm9yZGVyIGJvcmRlci1zbGF0ZS04MDAgcm91bmRlZC0yeGwgcC01IHNtOnAtNiBzaGFkb3cteGwgc3BhY2UteS00Ij4KICAgICAgPGRpdiBjbGFzcz0iZmxleCBpdGVtcy1jZW50ZXIganVzdGlmeS1iZXR3ZWVuIj4KICAgICAgICA8ZGl2PgogICAgICAgICAgPGgyIGNsYXNzPSJ0ZXh0LWJhc2Ugc206dGV4dC1sZyBmb250LXNlbWlib2xkIHRleHQtd2hpdGUiPk1vdW50IE1hc3NpdmUgTW92aWUgTGluazwvaDI+CiAgICAgICAgICA8cCBjbGFzcz0idGV4dC14cyB0ZXh0LXNsYXRlLTQwMCI+UGFzdGUgYW55IGRpcmVjdCBkb3dubG9hZCBvciBDRE4gc3RyZWFtIGxpbmsuIFRoZSBjbG91ZCBwcm9iZXMgaXQgaW4gJmx0OzEwMG1zLjwvcD4KICAgICAgICA8L2Rpdj4KICAgICAgPC9kaXY+CgogICAgICA8Zm9ybSBpZD0ibW91bnQtZm9ybSIgY2xhc3M9InNwYWNlLXktMyI+CiAgICAgICAgPGRpdiBjbGFzcz0ic3BhY2UteS0xIj4KICAgICAgICAgIDxsYWJlbCBjbGFzcz0idGV4dC14cyBmb250LW1lZGl1bSB0ZXh0LXNsYXRlLTMwMCI+MTAwIEdCIE1vdmllIExpbmsgKE1LViAvIE1QNCAvIFJlbXV4IC8gQ0ROKTwvbGFiZWw+CiAgICAgICAgICA8aW5wdXQgaWQ9InN0cmVhbS11cmwiIHR5cGU9InVybCIgcmVxdWlyZWQgcGxhY2Vob2xkZXI9Imh0dHBzOi8vZXhhbXBsZS5jb20vbW92aWVzL09wcGVuaGVpbWVyLjIwMjMuVUhELlJlbXV4Lm1rdiIKICAgICAgICAgICAgY2xhc3M9InctZnVsbCBiZy1zbGF0ZS05NTAgYm9yZGVyIGJvcmRlci1zbGF0ZS03MDAgcm91bmRlZC14bCBweC0zLjUgcHktMi41IHRleHQtc20gdGV4dC13aGl0ZSBwbGFjZWhvbGRlci1zbGF0ZS01MDAgZm9jdXM6b3V0bGluZS1ub25lIGZvY3VzOmJvcmRlci1pbmRpZ28tNTAwIGZvY3VzOnJpbmctMSBmb2N1czpyaW5nLWluZGlnby01MDAgdHJhbnNpdGlvbiI+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iZ3JpZCBncmlkLWNvbHMtMSBzbTpncmlkLWNvbHMtMyBnYXAtMyI+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJzbTpjb2wtc3Bhbi0yIHNwYWNlLXktMSI+CiAgICAgICAgICAgIDxsYWJlbCBjbGFzcz0idGV4dC14cyBmb250LW1lZGl1bSB0ZXh0LXNsYXRlLTMwMCI+Q3VzdG9tIFRpdGxlIChPcHRpb25hbCk8L2xhYmVsPgogICAgICAgICAgICA8aW5wdXQgaWQ9InN0cmVhbS10aXRsZSIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ImUuZy4gT3BwZW5oZWltZXIgKDRLIFVIRCBSZW11eCkiCiAgICAgICAgICAgICAgY2xhc3M9InctZnVsbCBiZy1zbGF0ZS05NTAgYm9yZGVyIGJvcmRlci1zbGF0ZS03MDAgcm91bmRlZC14bCBweC0zLjUgcHktMiB0ZXh0LXNtIHRleHQtd2hpdGUgcGxhY2Vob2xkZXItc2xhdGUtNTAwIGZvY3VzOm91dGxpbmUtbm9uZSBmb2N1czpib3JkZXItaW5kaWdvLTUwMCB0cmFuc2l0aW9uIj4KICAgICAgICAgIDwvZGl2PgogICAgICAgICAgPGRpdiBjbGFzcz0iZmxleCBpdGVtcy1lbmQiPgogICAgICAgICAgICA8YnV0dG9uIGlkPSJtb3VudC1idG4iIHR5cGU9InN1Ym1pdCIKICAgICAgICAgICAgICBjbGFzcz0idy1mdWxsIGJnLWdyYWRpZW50LXRvLXIgZnJvbS1pbmRpZ28tNjAwIHRvLWN5YW4tNTAwIGhvdmVyOmZyb20taW5kaWdvLTUwMCBob3Zlcjp0by1jeWFuLTQwMCB0ZXh0LXdoaXRlIGZvbnQtc2VtaWJvbGQgdGV4dC1zbSBweS0yLjUgcHgtNCByb3VuZGVkLXhsIHNoYWRvdy1sZyBzaGFkb3ctaW5kaWdvLTYwMC8yMCBmbGV4IGl0ZW1zLWNlbnRlciBqdXN0aWZ5LWNlbnRlciBzcGFjZS14LTIgdHJhbnNpdGlvbiBkaXNhYmxlZDpvcGFjaXR5LTUwIj4KICAgICAgICAgICAgICA8c3ZnIGlkPSJidG4taWNvbiIgY2xhc3M9InctNCBoLTQiIGZpbGw9Im5vbmUiIHN0cm9rZT0iY3VycmVudENvbG9yIiB2aWV3Qm94PSIwIDAgMjQgMjQiPjxwYXRoIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIgc3Ryb2tlLXdpZHRoPSIyIiBkPSJNMTMgMTBWM0w0IDE0aDd2N2w5LTExaC03eiI+PC9wYXRoPjwvc3ZnPgogICAgICAgICAgICAgIDxzcGFuIGlkPSJidG4tdGV4dCI+TW91bnQgVmlydHVhbCBXZWJEQVYgKDwxMDBtcyk8L3NwYW4+CiAgICAgICAgICAgIDwvYnV0dG9uPgogICAgICAgICAgPC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgIDwvZm9ybT4KCiAgICAgIDwhLS0gTW91bnQgU3RhdHVzIE5vdGljZSAtLT4KICAgICAgPGRpdiBpZD0ibW91bnQtZmVlZGJhY2siIGNsYXNzPSJoaWRkZW4gcC0zIHJvdW5kZWQteGwgdGV4dC14cyBib3JkZXIiPjwvZGl2PgogICAgPC9zZWN0aW9uPgoKICAgIDwhLS0gQ1ggRmlsZSBFeHBsb3JlciBRdWljayBDb25uZWN0aW9uIENhcmQgLS0+CiAgICA8c2VjdGlvbiBjbGFzcz0iYmctZ3JhZGllbnQtdG8tYnIgZnJvbS1pbmRpZ28tOTUwLzQwIHZpYS1zbGF0ZS05MDAgdG8tc2xhdGUtOTAwIGJvcmRlciBib3JkZXItaW5kaWdvLTUwMC8yMCByb3VuZGVkLTJ4bCBwLTUgc206cC02IHNoYWRvdy14bCI+CiAgICAgIDxkaXYgY2xhc3M9ImZsZXggZmxleC1jb2wgc206ZmxleC1yb3cgc206aXRlbXMtY2VudGVyIGp1c3RpZnktYmV0d2VlbiBwYi00IGJvcmRlci1iIGJvcmRlci1zbGF0ZS04MDAgZ2FwLTMiPgogICAgICAgIDxkaXY+CiAgICAgICAgICA8aDIgY2xhc3M9InRleHQtYmFzZSBzbTp0ZXh0LWxnIGZvbnQtc2VtaWJvbGQgdGV4dC1pbmRpZ28tMjAwIGZsZXggaXRlbXMtY2VudGVyIGdhcC0yIj4KICAgICAgICAgICAgPHN2ZyBjbGFzcz0idy01IGgtNSB0ZXh0LWluZGlnby00MDAiIGZpbGw9Im5vbmUiIHN0cm9rZT0iY3VycmVudENvbG9yIiB2aWV3Qm94PSIwIDAgMjQgMjQiPjxwYXRoIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIgc3Ryb2tlLXdpZHRoPSIyIiBkPSJNMyA3djEwYTIgMiAwIDAwMiAyaDE0YTIgMiAwIDAwMi0yVjlhMiAyIDAgMDAtMi0yaC02bC0yLTJINWEyIDIgMCAwMC0yIDJ6Ij48L3BhdGg+PC9zdmc+CiAgICAgICAgICAgIENYIEZpbGUgRXhwbG9yZXIgV2ViREFWIENvbmZpZ3VyYXRpb24gKEFuZHJvaWQgVFYgLyBQaG9uZSkKICAgICAgICAgIDwvaDI+CiAgICAgICAgICA8cCBjbGFzcz0idGV4dC14cyB0ZXh0LXNsYXRlLTQwMCI+T3BlbiBDWCBGaWxlIEV4cGxvcmVyICZndDsgTmV0d29yayAmZ3Q7IE5ldyBMb2NhdGlvbiAoKykgJmd0OyBSZW1vdGUgJmd0OyBXZWJEQVY8L3A+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJjb3B5RGF2VXJsKCkiIGNsYXNzPSJ0ZXh0LXhzIGJnLXNsYXRlLTgwMCBob3ZlcjpiZy1zbGF0ZS03MDAgdGV4dC1zbGF0ZS0yMDAgcHgtMyBweS0xLjUgcm91bmRlZC1sZyBib3JkZXIgYm9yZGVyLXNsYXRlLTcwMCB0cmFuc2l0aW9uIGZsZXggaXRlbXMtY2VudGVyIGdhcC0xLjUgc2VsZi1zdGFydCBzbTpzZWxmLWF1dG8iPgogICAgICAgICAgPHN2ZyBjbGFzcz0idy0zLjUgaC0zLjUiIGZpbGw9Im5vbmUiIHN0cm9rZT0iY3VycmVudENvbG9yIiB2aWV3Qm94PSIwIDAgMjQgMjQiPjxwYXRoIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIgc3Ryb2tlLXdpZHRoPSIyIiBkPSJNOCA1SDZhMiAyIDAgMDAtMiAydjEyYTIgMiAwIDAwMiAyaDEwYTIgMiAwIDAwMi0ydi0xTTggNWEyIDIgMCAwMDIgMmgyYTIgMiAwIDAwMi0yTTggNWEyIDIgMCAwMTItMmgyYTIgMiAwIDAxMiAybTAgMGgyYTIgMiAwIDAxMiAydjNtMiA0SDEwbTAgMGwzLTNtLTMgM2wzIDMiPjwvcGF0aD48L3N2Zz4KICAgICAgICAgIDxzcGFuIGlkPSJjb3B5LWJ0bi10ZXh0Ij5Db3B5IEZ1bGwgV2ViREFWIFVSTDwvc3Bhbj4KICAgICAgICA8L2J1dHRvbj4KICAgICAgPC9kaXY+CgogICAgICA8ZGl2IGNsYXNzPSJncmlkIGdyaWQtY29scy0yIHNtOmdyaWQtY29scy01IGdhcC0zIG10LTQgdGV4dC14cyI+CiAgICAgICAgPGRpdiBjbGFzcz0iYmctc2xhdGUtOTUwLzcwIHAtMyByb3VuZGVkLWxnIGJvcmRlciBib3JkZXItc2xhdGUtODAwIj4KICAgICAgICAgIDxzcGFuIGNsYXNzPSJ0ZXh0LXNsYXRlLTQwMCBibG9jayB0ZXh0LVsxMXB4XSI+SG9zdCAvIFNlcnZlcjwvc3Bhbj4KICAgICAgICAgIDxzcGFuIGlkPSJjeC1ob3N0IiBjbGFzcz0iZm9udC1tb25vIHRleHQtc2xhdGUtMjAwIGZvbnQtc2VtaWJvbGQgdHJ1bmNhdGUgYmxvY2sgbXQtMC41Ij4uLi48L3NwYW4+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iYmctc2xhdGUtOTUwLzcwIHAtMyByb3VuZGVkLWxnIGJvcmRlciBib3JkZXItc2xhdGUtODAwIj4KICAgICAgICAgIDxzcGFuIGNsYXNzPSJ0ZXh0LXNsYXRlLTQwMCBibG9jayB0ZXh0LVsxMXB4XSI+UGF0aDwvc3Bhbj4KICAgICAgICAgIDxzcGFuIGNsYXNzPSJmb250LW1vbm8gdGV4dC1pbmRpZ28tMzAwIGZvbnQtc2VtaWJvbGQgYmxvY2sgbXQtMC41Ij4vZGF2PC9zcGFuPgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9ImJnLXNsYXRlLTk1MC83MCBwLTMgcm91bmRlZC1sZyBib3JkZXIgYm9yZGVyLXNsYXRlLTgwMCI+CiAgICAgICAgICA8c3BhbiBjbGFzcz0idGV4dC1zbGF0ZS00MDAgYmxvY2sgdGV4dC1bMTFweF0iPlBvcnQ8L3NwYW4+CiAgICAgICAgICA8c3BhbiBpZD0iY3gtcG9ydCIgY2xhc3M9ImZvbnQtbW9ubyB0ZXh0LXNsYXRlLTIwMCBmb250LXNlbWlib2xkIGJsb2NrIG10LTAuNSI+NDQzPC9zcGFuPgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9ImJnLXNsYXRlLTk1MC83MCBwLTMgcm91bmRlZC1sZyBib3JkZXIgYm9yZGVyLXNsYXRlLTgwMCI+CiAgICAgICAgICA8c3BhbiBjbGFzcz0idGV4dC1zbGF0ZS00MDAgYmxvY2sgdGV4dC1bMTFweF0iPkVuY3J5cHRpb248L3NwYW4+CiAgICAgICAgICA8c3BhbiBpZD0iY3gtZW5jcnlwdGlvbiIgY2xhc3M9ImZvbnQtbW9ubyB0ZXh0LWVtZXJhbGQtNDAwIGZvbnQtc2VtaWJvbGQgYmxvY2sgbXQtMC41Ij5IVFRQUyAoT04pPC9zcGFuPgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9ImJnLXNsYXRlLTk1MC83MCBwLTMgcm91bmRlZC1sZyBib3JkZXIgYm9yZGVyLXNsYXRlLTgwMCBjb2wtc3Bhbi0yIHNtOmNvbC1zcGFuLTEiPgogICAgICAgICAgPHNwYW4gY2xhc3M9InRleHQtc2xhdGUtNDAwIGJsb2NrIHRleHQtWzExcHhdIj5BdXRoPC9zcGFuPgogICAgICAgICAgPHNwYW4gY2xhc3M9ImZvbnQtbW9ubyB0ZXh0LXNsYXRlLTMwMCBibG9jayBtdC0wLjUiPkFub255bW91cyAvIGFkbWluPC9zcGFuPgogICAgICAgIDwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvc2VjdGlvbj4KCiAgICA8IS0tIEFjdGl2ZSBNb3VudGVkIE1vdmllcyBMaXN0IC0tPgogICAgPHNlY3Rpb24gY2xhc3M9ImJnLXNsYXRlLTkwMC85MCBib3JkZXIgYm9yZGVyLXNsYXRlLTgwMCByb3VuZGVkLTJ4bCBwLTUgc206cC02IHNoYWRvdy14bCBzcGFjZS15LTQiPgogICAgICA8ZGl2IGNsYXNzPSJmbGV4IGl0ZW1zLWNlbnRlciBqdXN0aWZ5LWJldHdlZW4iPgogICAgICAgIDxoMiBjbGFzcz0idGV4dC1iYXNlIHNtOnRleHQtbGcgZm9udC1zZW1pYm9sZCB0ZXh0LXdoaXRlIj5BY3RpdmUgTW91bnRlZCBTdHJlYW1zPC9oMj4KICAgICAgICA8YnV0dG9uIG9uY2xpY2s9ImxvYWRNb3VudHMoKSIgY2xhc3M9InRleHQteHMgdGV4dC1pbmRpZ28tNDAwIGhvdmVyOnRleHQtaW5kaWdvLTMwMCB0cmFuc2l0aW9uIj5SZWZyZXNoIExpc3Q8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgaWQ9Im1vdW50cy1saXN0IiBjbGFzcz0iZGl2aWRlLXkgZGl2aWRlLXNsYXRlLTgwMCB0ZXh0LXNtIj4KICAgICAgICA8ZGl2IGNsYXNzPSJweS04IHRleHQtY2VudGVyIHRleHQtc2xhdGUtNTAwIHRleHQteHMiPk5vIHN0cmVhbXMgbW91bnRlZCB5ZXQuIFBhc3RlIGEgbGluayBhYm92ZSB0byBzdGFydC48L2Rpdj4KICAgICAgPC9kaXY+CiAgICA8L3NlY3Rpb24+CgogICAgPCEtLSBRUiBDb2RlIE1vZGFsIC8gUG9wdXAgLS0+CiAgICA8ZGl2IGlkPSJxci1tb2RhbCIgY2xhc3M9ImhpZGRlbiBmaXhlZCBpbnNldC0wIGJnLWJsYWNrLzgwIGJhY2tkcm9wLWJsdXItc20gei01MCBmbGV4IGl0ZW1zLWNlbnRlciBqdXN0aWZ5LWNlbnRlciBwLTQiPgogICAgICA8ZGl2IGNsYXNzPSJiZy1zbGF0ZS05MDAgYm9yZGVyIGJvcmRlci1zbGF0ZS03MDAgcm91bmRlZC0yeGwgcC02IG1heC13LXNtIHctZnVsbCBzcGFjZS15LTQgdGV4dC1jZW50ZXIiPgogICAgICAgIDxoMyBjbGFzcz0idGV4dC1iYXNlIGZvbnQtYm9sZCB0ZXh0LXdoaXRlIj5TY2FuIHdpdGggUGhvbmUgLyBUVjwvaDM+CiAgICAgICAgPHAgY2xhc3M9InRleHQteHMgdGV4dC1zbGF0ZS00MDAiPlNjYW4gdG8gb3BlbiBvciBzZW5kIHN0cmVhbSBkaXJlY3RseSB0byB5b3VyIG1vYmlsZSBwbGF5ZXI8L3A+CiAgICAgICAgPGRpdiBpZD0icXJjb2RlIiBjbGFzcz0icC0zIGJnLXdoaXRlIHJvdW5kZWQteGwgaW5saW5lLWJsb2NrIG14LWF1dG8iPjwvZGl2PgogICAgICAgIDxkaXYgaWQ9InFyLXVybC10ZXh0IiBjbGFzcz0idGV4dC1bMTFweF0gZm9udC1tb25vIHRleHQtc2xhdGUtNDAwIGJyZWFrLWFsbCI+PC9kaXY+CiAgICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJjbG9zZVFyTW9kYWwoKSIgY2xhc3M9InctZnVsbCBiZy1zbGF0ZS04MDAgaG92ZXI6Ymctc2xhdGUtNzAwIHRleHQtc2xhdGUtMjAwIHB5LTIgcm91bmRlZC14bCB0ZXh0LXhzIGZvbnQtc2VtaWJvbGQiPkNsb3NlPC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CgogIDwvZGl2PgoKICA8c2NyaXB0PgogICAgY29uc3QgY3VycmVudEJhc2VVcmwgPSB3aW5kb3cubG9jYXRpb24ub3JpZ2luOwoKICAgIC8vIEluaXRpYWxpemUgQ1ggRXhwbG9yZXIgY29uZmlnIHZhbHVlcwogICAgZG9jdW1lbnQuYWRkRXZlbnRMaXN0ZW5lcigiRE9NQ29udGVudExvYWRlZCIsICgpID0+IHsKICAgICAgY29uc3QgdXJsID0gbmV3IFVSTCh3aW5kb3cubG9jYXRpb24uaHJlZik7CiAgICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJjeC1ob3N0IikudGV4dENvbnRlbnQgPSB1cmwuaG9zdG5hbWU7CiAgICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJjeC1wb3J0IikudGV4dENvbnRlbnQgPSB1cmwucG9ydCB8fCAodXJsLnByb3RvY29sID09PSAiaHR0cHM6IiA/ICI0NDMiIDogIjgwIik7CiAgICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJjeC1lbmNyeXB0aW9uIikudGV4dENvbnRlbnQgPSB1cmwucHJvdG9jb2wgPT09ICJodHRwczoiID8gIkhUVFBTIChPTikiIDogIkhUVFAgKE9GRikiOwogICAgICBsb2FkTW91bnRzKCk7CiAgICAgIGZldGNoU3RhdHVzKCk7CiAgICAgIHNldEludGVydmFsKGZldGNoU3RhdHVzLCAzMDAwKTsKICAgIH0pOwoKICAgIGFzeW5jIGZ1bmN0aW9uIGZldGNoU3RhdHVzKCkgewogICAgICB0cnkgewogICAgICAgIGNvbnN0IHJlcyA9IGF3YWl0IGZldGNoKCIvYXBpL3N0YXR1cyIpOwogICAgICAgIGlmIChyZXMub2spIHsKICAgICAgICAgIGNvbnN0IGRhdGEgPSBhd2FpdCByZXMuanNvbigpOwogICAgICAgICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInN0YXQtdmlydHVhbCIpLnRleHRDb250ZW50ID0gYCR7ZGF0YS50b3RhbF92aXJ0dWFsX2xpYnJhcnlfZ2J9IEdCYDsKICAgICAgICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJzdGF0LXN0cmVhbWVkIikudGV4dENvbnRlbnQgPSBgJHtkYXRhLnN0cmVhbWVkX21ifSBNQmA7CiAgICAgICAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgic3RhdC1zYXZlZCIpLnRleHRDb250ZW50ID0gYCR7ZGF0YS5ob21lX2JhbmR3aWR0aF9zYXZlZF9nYn0gR0JgOwogICAgICAgICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInN0YXQtc3RyZWFtcyIpLnRleHRDb250ZW50ID0gZGF0YS5hY3RpdmVfc3RyZWFtczsKICAgICAgICB9CiAgICAgIH0gY2F0Y2ggKGUpIHt9CiAgICB9CgogICAgYXN5bmMgZnVuY3Rpb24gbG9hZE1vdW50cygpIHsKICAgICAgdHJ5IHsKICAgICAgICBjb25zdCByZXMgPSBhd2FpdCBmZXRjaCgiL2FwaS9tb3VudHMiKTsKICAgICAgICBjb25zdCBkYXRhID0gYXdhaXQgcmVzLmpzb24oKTsKICAgICAgICBjb25zdCBjb250YWluZXIgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgibW91bnRzLWxpc3QiKTsKICAgICAgICBpZiAoIWRhdGEubW91bnRzIHx8IGRhdGEubW91bnRzLmxlbmd0aCA9PT0gMCkgewogICAgICAgICAgY29udGFpbmVyLmlubmVySFRNTCA9ICc8ZGl2IGNsYXNzPSJweS04IHRleHQtY2VudGVyIHRleHQtc2xhdGUtNTAwIHRleHQteHMiPk5vIHN0cmVhbXMgbW91bnRlZCB5ZXQuIFBhc3RlIGEgbGluayBhYm92ZSB0byBzdGFydC48L2Rpdj4nOwogICAgICAgICAgcmV0dXJuOwogICAgICAgIH0KCiAgICAgICAgY29udGFpbmVyLmlubmVySFRNTCA9IGRhdGEubW91bnRzLm1hcChtID0+IHsKICAgICAgICAgIGNvbnN0IGRhdkZpbGVVcmwgPSBgJHtjdXJyZW50QmFzZVVybH0vZGF2LyR7ZW5jb2RlVVJJQ29tcG9uZW50KG0uZmlsZW5hbWUpfWA7CiAgICAgICAgICBjb25zdCB2bGNJbnRlbnQgPSBgdmxjOi8vJHtkYXZGaWxlVXJsfWA7CiAgICAgICAgICByZXR1cm4gYAogICAgICAgICAgICA8ZGl2IGNsYXNzPSJweS00IGZsZXggZmxleC1jb2wgbWQ6ZmxleC1yb3cgbWQ6aXRlbXMtY2VudGVyIGp1c3RpZnktYmV0d2VlbiBnYXAtMyI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0ic3BhY2UteS0xIG1heC13LXhsIj4KICAgICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImZsZXggaXRlbXMtY2VudGVyIHNwYWNlLXgtMiI+CiAgICAgICAgICAgICAgICAgIDxzcGFuIGNsYXNzPSJweC0yIHB5LTAuNSBiZy1pbmRpZ28tOTAwLzYwIGJvcmRlciBib3JkZXItaW5kaWdvLTcwMC81MCB0ZXh0LWluZGlnby0zMDAgdGV4dC1bMTBweF0gZm9udC1ib2xkIHJvdW5kZWQtbWQgdXBwZXJjYXNlIHRyYWNraW5nLXdpZGVyIj4KICAgICAgICAgICAgICAgICAgICAke20uY29udGVudF90eXBlLmluY2x1ZGVzKCJtYXRyb3NrYSIpID8gIk1LViA0SyIgOiAiTVA0In0KICAgICAgICAgICAgICAgICAgPC9zcGFuPgogICAgICAgICAgICAgICAgICA8c3BhbiBjbGFzcz0iZm9udC1zZW1pYm9sZCB0ZXh0LXNsYXRlLTIwMCB0ZXh0LXNtIHRydW5jYXRlIj4ke20udGl0bGUgfHwgbS5maWxlbmFtZX08L3NwYW4+CiAgICAgICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgICAgIDxkaXYgY2xhc3M9InRleHQteHMgdGV4dC1zbGF0ZS00MDAgZmxleCBmbGV4LXdyYXAgaXRlbXMtY2VudGVyIGdhcC14LTMgZ2FwLXktMSI+CiAgICAgICAgICAgICAgICAgIDxzcGFuIGNsYXNzPSJ0ZXh0LWVtZXJhbGQtNDAwIGZvbnQtbWVkaXVtIj5WaXJ0dWFsIFNpemU6ICR7bS5mb3JtYXR0ZWRfc2l6ZX08L3NwYW4+CiAgICAgICAgICAgICAgICAgIDxzcGFuIGNsYXNzPSJ0ZXh0LXNsYXRlLTYwMCI+4oCiPC9zcGFuPgogICAgICAgICAgICAgICAgICA8c3BhbiBjbGFzcz0iZm9udC1tb25vIHRleHQtWzExcHhdIHRleHQtc2xhdGUtNTAwIHRydW5jYXRlIG1heC13LXhzIj4ke20uZmlsZW5hbWV9PC9zcGFuPgogICAgICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iZmxleCBpdGVtcy1jZW50ZXIgZmxleC13cmFwIGdhcC0yIHRleHQteHMiPgogICAgICAgICAgICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJjb3B5VG9DbGlwYm9hcmQoJyR7ZGF2RmlsZVVybH0nKSIgY2xhc3M9ImJnLXNsYXRlLTgwMCBob3ZlcjpiZy1zbGF0ZS03MDAgdGV4dC1zbGF0ZS0yMDAgcHgtMi41IHB5LTEuNSByb3VuZGVkLWxnIGJvcmRlciBib3JkZXItc2xhdGUtNzAwIHRyYW5zaXRpb24iPgogICAgICAgICAgICAgICAgICBDb3B5IERBViBMaW5rCiAgICAgICAgICAgICAgICA8L2J1dHRvbj4KICAgICAgICAgICAgICAgIDxhIGhyZWY9IiR7dmxjSW50ZW50fSIgY2xhc3M9ImJnLW9yYW5nZS02MDAgaG92ZXI6Ymctb3JhbmdlLTUwMCB0ZXh0LXdoaXRlIHB4LTIuNSBweS0xLjUgcm91bmRlZC1sZyBmb250LW1lZGl1bSBzaGFkb3ctc20gdHJhbnNpdGlvbiBmbGV4IGl0ZW1zLWNlbnRlciBnYXAtMSI+CiAgICAgICAgICAgICAgICAgIDxzcGFuPk9wZW4gaW4gVkxDPC9zcGFuPgogICAgICAgICAgICAgICAgPC9hPgogICAgICAgICAgICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJvcGVuUXJNb2RhbCgnJHtkYXZGaWxlVXJsfScpIiBjbGFzcz0iYmctc2xhdGUtODAwIGhvdmVyOmJnLXNsYXRlLTcwMCB0ZXh0LXNsYXRlLTMwMCBweC0yIHB5LTEuNSByb3VuZGVkLWxnIGJvcmRlciBib3JkZXItc2xhdGUtNzAwIHRyYW5zaXRpb24iIHRpdGxlPSJTaG93IFFSIENvZGUiPgogICAgICAgICAgICAgICAgICA8c3ZnIGNsYXNzPSJ3LTQgaC00IiBmaWxsPSJub25lIiBzdHJva2U9ImN1cnJlbnRDb2xvciIgdmlld0JveD0iMCAwIDI0IDI0Ij48cGF0aCBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiIHN0cm9rZS13aWR0aD0iMiIgZD0iTTEyIDR2MW02IDExaDJtLTYgMGgtMnY0bTAtMTF2M20wIDBoLjAxTTEyIDEyaDQuMDFNMTYgMjBoNE00IDEyaDRtMTIgMGguMDFNNSA4aDJhMSAxIDAgMDAxLTFWNWExIDEgMCAwMC0xLTFINWExIDEgMCAwMC0xIDF2MmExIDEgMCAwMDEgMXptMTIgMGgyYTEgMSAwIDAwMS0xVjVhMSAxIDAgMDAtMS0xaC0yYTEgMSAwIDAwLTEgMXYyYTEgMSAwIDAwMSAxek01IDIwaDJhMSAxIDAgMDAxLTF2LTJhMSAxIDAgMDAtMS0xSDVhMSAxIDAgMDAtMSAxdjJhMSAxIDAgMDAxIDF6Ij48L3BhdGg+PC9zdmc+CiAgICAgICAgICAgICAgICA8L2J1dHRvbj4KICAgICAgICAgICAgICAgIDxidXR0b24gb25jbGljaz0idW5tb3VudEZpbGUoJyR7ZW5jb2RlVVJJQ29tcG9uZW50KG0uZmlsZW5hbWUpfScpIiBjbGFzcz0idGV4dC1yb3NlLTQwMCBob3Zlcjp0ZXh0LXJvc2UtMzAwIHAtMS41IHRyYW5zaXRpb24iIHRpdGxlPSJVbm1vdW50Ij4KICAgICAgICAgICAgICAgICAgPHN2ZyBjbGFzcz0idy00IGgtNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJjdXJyZW50Q29sb3IiIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIiBzdHJva2Utd2lkdGg9IjIiIGQ9Ik0xOSA3bC0uODY3IDEyLjE0MkEyIDIgMCAwMTE2LjEzOCAyMUg3Ljg2MmEyIDIgMCAwMS0xLjk5NS0xLjg1OEw1IDdtNSA0djZtNC02djZtMS0xMFY0YTEgMSAwIDAwLTEtMWgtNGExIDEgMCAwMC0xIDF2M000IDdoMTYiPjwvcGF0aD48L3N2Zz4KICAgICAgICAgICAgICAgIDwvYnV0dG9uPgogICAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgIGA7CiAgICAgICAgfSkuam9pbigiIik7CiAgICAgIH0gY2F0Y2ggKGUpIHt9CiAgICB9CgogICAgLy8gTW91bnQgZm9ybSBzdWJtaXQgaGFuZGxlcgogICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoIm1vdW50LWZvcm0iKS5hZGRFdmVudExpc3RlbmVyKCJzdWJtaXQiLCBhc3luYyAoZSkgPT4gewogICAgICBlLnByZXZlbnREZWZhdWx0KCk7CiAgICAgIGNvbnN0IHVybElucHV0ID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInN0cmVhbS11cmwiKTsKICAgICAgY29uc3QgdGl0bGVJbnB1dCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJzdHJlYW0tdGl0bGUiKTsKICAgICAgY29uc3QgYnRuID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoIm1vdW50LWJ0biIpOwogICAgICBjb25zdCBidG5UZXh0ID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoImJ0bi10ZXh0Iik7CiAgICAgIGNvbnN0IGZlZWRiYWNrID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoIm1vdW50LWZlZWRiYWNrIik7CgogICAgICBidG4uZGlzYWJsZWQgPSB0cnVlOwogICAgICBidG5UZXh0LnRleHRDb250ZW50ID0gIlByb2JpbmcgPDEwMG1zLi4uIjsKICAgICAgZmVlZGJhY2suY2xhc3NOYW1lID0gImhpZGRlbiI7CgogICAgICB0cnkgewogICAgICAgIGNvbnN0IHJlcyA9IGF3YWl0IGZldGNoKCIvYXBpL21vdW50IiwgewogICAgICAgICAgbWV0aG9kOiAiUE9TVCIsCiAgICAgICAgICBoZWFkZXJzOiB7ICJDb250ZW50LVR5cGUiOiAiYXBwbGljYXRpb24vanNvbiIgfSwKICAgICAgICAgIGJvZHk6IEpTT04uc3RyaW5naWZ5KHsKICAgICAgICAgICAgdXJsOiB1cmxJbnB1dC52YWx1ZS50cmltKCksCiAgICAgICAgICAgIHRpdGxlOiB0aXRsZUlucHV0LnZhbHVlLnRyaW0oKSB8fCB1bmRlZmluZWQKICAgICAgICAgIH0pCiAgICAgICAgfSk7CgogICAgICAgIGNvbnN0IGRhdGEgPSBhd2FpdCByZXMuanNvbigpOwogICAgICAgIGlmIChyZXMub2sgJiYgZGF0YS5zdGF0dXMgPT09ICJzdWNjZXNzIikgewogICAgICAgICAgZmVlZGJhY2suY2xhc3NOYW1lID0gInAtMyByb3VuZGVkLXhsIHRleHQteHMgYm9yZGVyIGJnLWVtZXJhbGQtOTUwLzYwIGJvcmRlci1lbWVyYWxkLTUwMC8zMCB0ZXh0LWVtZXJhbGQtMzAwIGJsb2NrIjsKICAgICAgICAgIGZlZWRiYWNrLmlubmVySFRNTCA9IGDinJMgPHN0cm9uZz5Nb3VudGVkIHN1Y2Nlc3NmdWxseTo8L3N0cm9uZz4gJHtkYXRhLm1vdW50LmZpbGVuYW1lfSAoJHtkYXRhLm1vdW50LmZvcm1hdHRlZF9zaXplfSkgcmVhZHkgb24gV2ViREFWIGluICR7ZGF0YS5wcm9iZS5lbGFwc2VkX21zfW1zLmA7CiAgICAgICAgICB1cmxJbnB1dC52YWx1ZSA9ICIiOwogICAgICAgICAgdGl0bGVJbnB1dC52YWx1ZSA9ICIiOwogICAgICAgICAgbG9hZE1vdW50cygpOwogICAgICAgICAgZmV0Y2hTdGF0dXMoKTsKICAgICAgICB9IGVsc2UgewogICAgICAgICAgZmVlZGJhY2suY2xhc3NOYW1lID0gInAtMyByb3VuZGVkLXhsIHRleHQteHMgYm9yZGVyIGJnLXJvc2UtOTUwLzYwIGJvcmRlci1yb3NlLTUwMC8zMCB0ZXh0LXJvc2UtMzAwIGJsb2NrIjsKICAgICAgICAgIGZlZWRiYWNrLmlubmVySFRNTCA9IGDinJcgPHN0cm9uZz5Nb3VudCBmYWlsZWQ6PC9zdHJvbmc+ICR7ZGF0YS5tZXNzYWdlIHx8ICJDb3VsZCBub3QgcHJvYmUgc3RyZWFtIn1gOwogICAgICAgIH0KICAgICAgfSBjYXRjaCAoZXJyKSB7CiAgICAgICAgZmVlZGJhY2suY2xhc3NOYW1lID0gInAtMyByb3VuZGVkLXhsIHRleHQteHMgYm9yZGVyIGJnLXJvc2UtOTUwLzYwIGJvcmRlci1yb3NlLTUwMC8zMCB0ZXh0LXJvc2UtMzAwIGJsb2NrIjsKICAgICAgICBmZWVkYmFjay5pbm5lckhUTUwgPSBg4pyXIDxzdHJvbmc+TmV0d29yayBlcnJvcjo8L3N0cm9uZz4gJHtlcnIubWVzc2FnZX1gOwogICAgICB9IGZpbmFsbHkgewogICAgICAgIGJ0bi5kaXNhYmxlZCA9IGZhbHNlOwogICAgICAgIGJ0blRleHQudGV4dENvbnRlbnQgPSAiTW91bnQgVmlydHVhbCBXZWJEQVYgKDwxMDBtcykiOwogICAgICB9CiAgICB9KTsKCiAgICBhc3luYyBmdW5jdGlvbiB1bm1vdW50RmlsZShmaWxlbmFtZSkgewogICAgICBpZiAoIWNvbmZpcm0oYFVubW91bnQgc3RyZWFtP2ApKSByZXR1cm47CiAgICAgIGF3YWl0IGZldGNoKGAvYXBpL21vdW50cy8ke2ZpbGVuYW1lfWAsIHsgbWV0aG9kOiAiREVMRVRFIiB9KTsKICAgICAgbG9hZE1vdW50cygpOwogICAgICBmZXRjaFN0YXR1cygpOwogICAgfQoKICAgIGZ1bmN0aW9uIGNvcHlUb0NsaXBib2FyZCh0ZXh0KSB7CiAgICAgIG5hdmlnYXRvci5jbGlwYm9hcmQud3JpdGVUZXh0KHRleHQpOwogICAgICBhbGVydCgiQ29waWVkIHRvIGNsaXBib2FyZCEiKTsKICAgIH0KCiAgICBmdW5jdGlvbiBjb3B5RGF2VXJsKCkgewogICAgICBjb25zdCB1cmwgPSBgJHtjdXJyZW50QmFzZVVybH0vZGF2L2A7CiAgICAgIG5hdmlnYXRvci5jbGlwYm9hcmQud3JpdGVUZXh0KHVybCk7CiAgICAgIGNvbnN0IGJ0blRleHQgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgiY29weS1idG4tdGV4dCIpOwogICAgICBjb25zdCBvcmlnID0gYnRuVGV4dC50ZXh0Q29udGVudDsKICAgICAgYnRuVGV4dC50ZXh0Q29udGVudCA9ICJDb3BpZWQhIjsKICAgICAgc2V0VGltZW91dCgoKSA9PiB7IGJ0blRleHQudGV4dENvbnRlbnQgPSBvcmlnOyB9LCAyMDAwKTsKICAgIH0KCiAgICBsZXQgcXJDb2RlSW5zdGFuY2UgPSBudWxsOwogICAgZnVuY3Rpb24gb3BlblFyTW9kYWwodXJsKSB7CiAgICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCJxci11cmwtdGV4dCIpLnRleHRDb250ZW50ID0gdXJsOwogICAgICBjb25zdCBxckVsID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInFyY29kZSIpOwogICAgICBxckVsLmlubmVySFRNTCA9ICIiOwogICAgICBxckNvZGVJbnN0YW5jZSA9IG5ldyBRUkNvZGUocXJFbCwgewogICAgICAgIHRleHQ6IHVybCwKICAgICAgICB3aWR0aDogMTkyLAogICAgICAgIGhlaWdodDogMTkyLAogICAgICAgIGNvbG9yRGFyazogIiMwYjBmMTkiLAogICAgICAgIGNvbG9yTGlnaHQ6ICIjZmZmZmZmIiwKICAgICAgICBjb3JyZWN0TGV2ZWw6IFFSQ29kZS5Db3JyZWN0TGV2ZWwuTQogICAgICB9KTsKICAgICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoInFyLW1vZGFsIikuY2xhc3NMaXN0LnJlbW92ZSgiaGlkZGVuIik7CiAgICB9CgogICAgZnVuY3Rpb24gY2xvc2VRck1vZGFsKCkgewogICAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgicXItbW9kYWwiKS5jbGFzc0xpc3QuYWRkKCJoaWRkZW4iKTsKICAgIH0KICA8L3NjcmlwdD4KPC9ib2R5Pgo8L2h0bWw+Cg=="
HTML_PAGE = base64.b64decode(_HTML_B64).decode("utf-8")

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
        return HTMLResponse(content=HTML_PAGE, status_code=200)
    return await handle_webdav_request(request, path="")

@app.post("/api/mount")
async def api_mount_stream(req: MountRequest, request: Request):
    url = req.url.strip()
    if not url:
        return JSONResponse({"status": "error", "message": "URL cannot be empty"}, status_code=400)

    probe = await probe_stream(url, custom_headers=req.custom_headers)
    if not probe["valid"]:
        return JSONResponse({"status": "error", "message": probe["error"] or "Probe failed", "details": probe}, status_code=422)

    filename = probe["default_filename"]
    title = req.title or filename
    movie_id = f"m_{int(time.time())}"

    mount = mount_manager.add_mount(
        movie_id=movie_id, filename=filename, upstream_url=probe["final_url"],
        total_bytes=probe["total_bytes"], content_type=probe["content_type"],
        formatted_size=probe["formatted_size"], title=title
    )
    base_url = str(request.base_url).rstrip("/")
    return {
        "status": "success",
        "mount": mount,
        "probe": probe,
        "stream_endpoints": {
            "webdav_folder": f"{base_url}/dav/",
            "webdav_file": f"{base_url}/dav/{filename}",
            "vlc_intent": f"vlc://{base_url}/dav/{filename}",
            "cx_file_explorer": {
                "server": request.url.hostname or "localhost",
                "port": request.url.port or (443 if request.url.scheme == "https" else 80),
                "path": "/dav",
                "https": request.url.scheme == "https",
                "username": "admin",
                "password": "none"
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
    streamed_gb = round(telemetry_stats["total_bytes_streamed"] / (1024**3), 3)
    streamed_mb = round(telemetry_stats["total_bytes_streamed"] / (1024**2), 1)
    total_virtual_bytes = sum(m.get("total_bytes", 0) for m in mount_manager.list_all())
    virtual_gb = round(total_virtual_bytes / (1024**3), 2)
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

@app.api_route("/dav", methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH"])
@app.api_route("/dav/{path:path}", methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "PROPPATCH"])
async def webdav_dispatcher(request: Request, path: str = ""):
    if not path and request.method == "GET":
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return HTMLResponse(content=HTML_PAGE, status_code=200)
    return await handle_webdav_request(request, path)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
