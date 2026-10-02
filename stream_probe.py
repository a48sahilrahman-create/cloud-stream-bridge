"""
Stream Probe - Zero-Trust Async Container & HTTP Range Capability Detector (<100ms)
Detects:
  - Total file size via Content-Range or Content-Length
  - Accept-Ranges: bytes support
  - Magic bytes: MKV (EBML \x1a\x45\xdf\xa3), MP4 (ftyp/moov/mdat), AVI, TS
  - MIME content-type and streaming viability
"""

import time
import httpx
import re
from urllib.parse import urlparse, parse_qs, unquote
from typing import Dict, Any, Optional

PROBE_TIMEOUT = 5.0
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

VIDEO_EXTENSIONS = (
    ".mkv", ".mp4", ".ts", ".avi", ".mov", ".webm",
    ".m4v", ".flv", ".wmv", ".iso", ".mpg", ".mpeg", ".vob"
)


def sanitize_filename(name: str) -> str:
    """Sanitize filename for WebDAV and CX File Explorer compatibility."""
    s = re.sub(r'[\\/*?:"<>|]', "", name)
    s = s.strip().replace(" ", "_")
    return s or "movie_stream.mkv"


def extract_filename(
    url: str,
    resp_headers: Optional[Any] = None,
    ext: str = ".mkv",
    fallback_url: Optional[str] = None
) -> str:
    """
    Extracts an authentic filename from presigned query parameters (AWS S3 / Cloudflare R2),
    upstream HTTP response headers, or the URL path, with sanitization and extension preservation.
    """
    disp_val = None

    # 1. Parse URL query parameters for response-content-disposition (R2/S3 presigned URLs)
    for u in [url, fallback_url]:
        if not u:
            continue
        try:
            parsed = urlparse(u)
            qs = parse_qs(parsed.query)
            for k, v in qs.items():
                if k.lower() == "response-content-disposition" and v:
                    disp_val = v[0]
                    break
            if disp_val:
                break
        except Exception:
            pass

    # 2. If not found in query params, check content-disposition in response headers
    if not disp_val and resp_headers:
        if hasattr(resp_headers, "get"):
            disp_val = resp_headers.get("content-disposition") or resp_headers.get("Content-Disposition")
        if not disp_val and hasattr(resp_headers, "items"):
            for k, v in resp_headers.items():
                if str(k).lower() == "content-disposition" and v:
                    disp_val = str(v)
                    break

    # 3. Parse disposition value with regex
    candidate = None
    if disp_val:
        # Check RFC 5987 encoded filename* (e.g. UTF-8''encoded_name.mkv)
        m_star = re.search(r"filename\*\s*=\s*([^;]+)", disp_val, re.IGNORECASE)
        if m_star:
            raw = m_star.group(1).strip().strip('"\'')
            if "'" in raw:
                raw = raw.split("'", 2)[-1]
            candidate = unquote(raw)

        # Check standard filename="name" or filename=name
        if not candidate:
            m_std = re.search(r'''filename\s*=\s*(?:"([^"]+)"|'([^']+)'|([^;\s]+))''', disp_val, re.IGNORECASE)
            if m_std:
                raw = m_std.group(1) or m_std.group(2) or m_std.group(3) or ""
                candidate = unquote(raw.strip())

    # 4. If authentic filename found from disposition, sanitize and ensure extension
    if candidate:
        candidate = candidate.replace("\\", "/").split("/")[-1].strip()
        candidate = sanitize_filename(candidate)
        if candidate:
            if not any(candidate.lower().endswith(e) for e in VIDEO_EXTENSIONS):
                candidate += ext
            return candidate

    # 5. Fallback to URL path filename if valid (contains dot and not just extension)
    for u in [url, fallback_url]:
        if not u:
            continue
        try:
            parsed_path = urlparse(u).path.rstrip("/")
            if parsed_path:
                path_fn = parsed_path.split("/")[-1]
                path_fn = unquote(path_fn).strip()
                if path_fn and "." in path_fn:
                    path_fn = sanitize_filename(path_fn)
                    if not any(path_fn.lower().endswith(e) for e in VIDEO_EXTENSIONS):
                        path_fn += ext
                    return path_fn
        except Exception:
            pass

    # 6. Fallback if neither disposition nor valid path filename is present
    return f"stream_{int(time.time())}{ext}"


async def probe_stream(url: str, custom_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Probes an upstream stream/download link.
    Performs targeted Range GET (bytes=0-8191) to verify:
      1. HTTP 206 Partial Content support
      2. Container magic bytes
      3. Total file size
    """
    t0 = time.perf_counter()
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Range": "bytes=0-8191"
    }
    if custom_headers:
        headers.update(custom_headers)

    client_kwargs = {
        "timeout": PROBE_TIMEOUT,
        "follow_redirects": True,
        "verify": False
    }

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
                status_code = resp.status_code
                content_range = resp.headers.get("content-range", "")
                content_length = resp.headers.get("content-length", "")
                content_type = resp.headers.get("content-type", "").lower()
                final_url = str(resp.url)
                resp_headers = resp.headers

                # Read first 8KB of data
                data = b""
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) >= 8192:
                        break

            # Parse total size
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

            # Fallback HEAD request if range was rejected or size is missing
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

            # Detect container format
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

            # Format human-readable size
            if total_bytes >= 1024**3:
                formatted_size = f"{round(total_bytes / (1024**3), 2)} GB"
            elif total_bytes >= 1024**2:
                formatted_size = f"{round(total_bytes / (1024**2), 2)} MB"
            else:
                formatted_size = f"{total_bytes} bytes" if total_bytes > 0 else "Dynamic Stream"

            # Derive default filename from presigned query params, headers, or URL path
            url_filename = extract_filename(final_url, resp_headers, ext=ext, fallback_url=url)

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
                "error": None if is_valid else f"HTTP {status_code} received from upstream server"
            }

    except Exception as e:
        return {
            "valid": False,
            "status_code": 0,
            "range_supported": False,
            "container_format": "Unknown",
            "content_type": "video/x-matroska",
            "total_bytes": 0,
            "formatted_size": "Unknown Size",
            "default_filename": extract_filename(url, ext=".mkv"),
            "final_url": url,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "error": str(e)
        }
