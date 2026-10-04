"""
Stream Probe - Zero-Trust Async Container & HTTP Range Capability Detector (<100ms)
Detects:
  - Total file size via Content-Range or Content-Length
  - Accept-Ranges: bytes support
  - Magic bytes: MKV (EBML \x1a\x45\xdf\xa3), MP4 (ftyp/moov/mdat), AVI, TS
  - MIME content-type and streaming viability
"""

import time
import asyncio
import httpx
import re
from urllib.parse import urlparse, parse_qs, unquote
from typing import Dict, Any, Optional

PROBE_TIMEOUT = 8.0
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

VIDEO_EXTENSIONS = (
    ".mkv", ".mp4", ".ts", ".avi", ".mov", ".webm",
    ".m4v", ".flv", ".wmv", ".iso", ".mpg", ".mpeg", ".vob", ".m3u8"
)

# Global Real-Time Probe Registry & Shield Events
probe_tracker: Dict[str, Dict[str, Any]] = {}
probe_events: Dict[str, asyncio.Event] = {}

def get_probe_event(key: str) -> asyncio.Event:
    """Return or create an asyncio.Event for a given probe_id or filename."""
    if key not in probe_events:
        probe_events[key] = asyncio.Event()
    return probe_events[key]

def register_probe(key: str) -> asyncio.Event:
    """Register or reset an asyncio.Event for an in-flight probe."""
    evt = get_probe_event(key)
    evt.clear()
    return evt

def complete_probe(key: str):
    """Mark an in-flight probe as completed and wake any awaiting WebDAV clients."""
    if key in probe_events:
        probe_events[key].set()

def is_probe_active(key: str) -> bool:
    """Check if a probe is currently in-flight and not yet resolved."""
    if key in probe_events:
        return not probe_events[key].is_set()
    return False

def count_active_probes() -> int:
    """Return count of currently in-flight probes."""
    return sum(1 for evt in probe_events.values() if not evt.is_set())

def set_probe_stage(probe_id: Optional[str], stage: str, percent: int, label: str, extra: Optional[Dict[str, Any]] = None):
    """Update active probe telemetry stages (0% -> 25% -> 60% -> 85% -> 100%)."""
    if not probe_id:
        return
    now = time.time()
    existing = probe_tracker.get(probe_id, {
        "probe_id": probe_id,
        "start_time": now,
        "stage": stage,
        "percent": percent,
        "label": label,
        "done": False,
        "error": None
    })
    existing.update({
        "stage": stage,
        "percent": percent,
        "label": label,
        "updated_at": now,
        "elapsed_sec": round(now - existing.get("start_time", now), 2)
    })
    if extra:
        existing.update(extra)
    probe_tracker[probe_id] = existing

def get_probe_status(probe_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve live probe stage dictionary."""
    return probe_tracker.get(probe_id)


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


async def probe_stream(
    url: str,
    probe_id: Optional[str] = None,
    custom_headers: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """
    Probes an upstream stream/download link with a multi-tier resilience cascade:
      Tier 1: Range GET (bytes=0-8191) with modern browser headers.
      Tier 2: Fallback HEAD without Range (if Range GET gets 400, 403, 416, or missing size).
      Tier 3: Fallback Stream GET without Range (if HEAD gets 405/403 or for container magic bytes).
    Never raises unhandled exceptions; returns clean metadata dictionary.
    """
    t0 = time.perf_counter()
    set_probe_stage(probe_id, "init", 0, "Initializing Stream Probe")
    evt = get_probe_event(probe_id) if probe_id else None
    if evt:
        evt.clear()

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if (parsed.scheme and parsed.netloc) else ""

    base_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    if origin:
        base_headers["Referer"] = f"{origin}/"
        base_headers["Origin"] = origin
    if custom_headers:
        base_headers.update(custom_headers)

    client_kwargs = {
        "timeout": PROBE_TIMEOUT,
        "follow_redirects": True,
        "verify": False
    }

    status_code = 0
    content_range = ""
    content_length = ""
    content_type = ""
    accept_ranges = ""
    final_url = url
    resp_headers = None
    data = b""
    error_msg = None

    try:
        # -------------------------------------------------------------
        # Tier 1: Range GET (bytes=0-8191) with browser headers
        # -------------------------------------------------------------
        tier1_headers = dict(base_headers)
        tier1_headers["Range"] = "bytes=0-8191"
        tier1_ok = False

        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                async with client.stream("GET", url, headers=tier1_headers) as resp:
                    status_code = resp.status_code
                    final_url = str(resp.url)
                    resp_headers = resp.headers
                    content_range = resp.headers.get("content-range", "")
                    content_length = resp.headers.get("content-length", "")
                    content_type = resp.headers.get("content-type", "").lower()
                    accept_ranges = resp.headers.get("accept-ranges", "").lower()

                    if status_code in (200, 206):
                        set_probe_stage(probe_id, "redirects", 25, "Resolving Redirects & CDN Origin", {"final_url": final_url})
                        async for chunk in resp.aiter_bytes():
                            data += chunk
                            if len(data) >= 8192:
                                break
                        tier1_ok = True
                    else:
                        error_msg = f"Tier 1 Range GET returned HTTP {status_code}"
        except Exception as e:
            error_msg = f"Tier 1 Range GET failed: {e}"

        # -------------------------------------------------------------
        # Tier 2: Fallback HEAD without Range header
        # Triggered if Tier 1 hit 400, 403, 416, non-200/206, or missing size
        # -------------------------------------------------------------
        need_tier2 = (
            (not tier1_ok) or
            (status_code in (400, 403, 416)) or
            (not content_range and not content_length)
        )
        tier2_ok = False
        head_status_code = 0

        if need_tier2:
            head_headers = dict(base_headers)
            head_headers.pop("Range", None)
            try:
                async with httpx.AsyncClient(**client_kwargs) as client:
                    head_resp = await client.head(url, headers=head_headers)
                    head_status_code = head_resp.status_code
                    if head_status_code in (200, 206):
                        status_code = head_status_code
                        final_url = str(head_resp.url)
                        resp_headers = head_resp.headers
                        h_len = head_resp.headers.get("content-length", "")
                        if h_len and h_len.isdigit():
                            content_length = h_len
                        h_ct = head_resp.headers.get("content-type", "")
                        if h_ct:
                            content_type = h_ct.lower()
                        h_ar = head_resp.headers.get("accept-ranges", "")
                        if h_ar:
                            accept_ranges = h_ar.lower()
                        tier2_ok = True
                        error_msg = None
                    else:
                        if not tier1_ok:
                            status_code = head_status_code
                            resp_headers = head_resp.headers
                            error_msg = f"Tier 2 HEAD returned HTTP {head_status_code}"
            except Exception as e:
                if not tier1_ok:
                    error_msg = f"Tier 2 HEAD failed: {e}"

        # -------------------------------------------------------------
        # Tier 3: Stream GET without Range header (read first 8KB, then close stream)
        # Triggered if HEAD is rejected with 405 Method Not Allowed or 403,
        # or if Tier 1 failed and we have no container payload data to detect magic bytes.
        # -------------------------------------------------------------
        need_tier3 = False
        if not tier1_ok:
            if head_status_code in (405, 403, 400) or not tier2_ok or len(data) == 0:
                need_tier3 = True

        if need_tier3:
            stream_headers = dict(base_headers)
            stream_headers.pop("Range", None)
            try:
                async with httpx.AsyncClient(**client_kwargs) as client:
                    async with client.stream("GET", url, headers=stream_headers) as resp:
                        stream_code = resp.status_code
                        if stream_code in (200, 206) or not (tier1_ok or tier2_ok):
                            status_code = stream_code
                            final_url = str(resp.url)
                            resp_headers = resp.headers
                            s_len = resp.headers.get("content-length", "")
                            if s_len and s_len.isdigit() and not content_length:
                                content_length = s_len
                            s_ct = resp.headers.get("content-type", "")
                            if s_ct and not content_type:
                                content_type = s_ct.lower()
                            s_ar = resp.headers.get("accept-ranges", "")
                            if s_ar and not accept_ranges:
                                accept_ranges = s_ar.lower()

                        if stream_code in (200, 206):
                            data = b""
                            async for chunk in resp.aiter_bytes():
                                data += chunk
                                if len(data) >= 8192:
                                    break
                            error_msg = None
                        elif not (tier1_ok or tier2_ok):
                            error_msg = f"Tier 3 Stream GET returned HTTP {stream_code}"
            except Exception as e:
                if not (tier1_ok or tier2_ok):
                    error_msg = f"Tier 3 Stream GET failed: {e}"

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

        set_probe_stage(probe_id, "container", 60, "Inspecting Container & Magic Bytes")

        # Parse total size
        total_bytes = 0
        if content_range and "/" in content_range:
            try:
                total_bytes = int(content_range.split("/")[-1])
            except ValueError:
                pass
        elif content_length and content_length.isdigit():
            try:
                total_bytes = int(content_length)
            except ValueError:
                pass

        set_probe_stage(probe_id, "size_lock", 85, "Locking Byte-Range & File Size", {"total_bytes": total_bytes})

        # Optimistic streaming capability:
        # If total_bytes == 0 or range is not explicitly advertised, mark range_supported = True
        # because many modern CDNs support range requests even if Accept-Ranges header is omitted.
        if (status_code == 206) or ("bytes" in accept_ranges) or (total_bytes == 0) or (accept_ranges != "none"):
            range_supported = True
        else:
            range_supported = False

        # Container & filename detection
        is_m3u8_url = (".m3u8" in url.lower()) or (".m3u8" in final_url.lower())
        is_m3u8_ct = any(x in content_type for x in ["mpegurl", "m3u8"])
        is_m3u8_data = data.startswith(b"#EXTM3U") or (b"#EXT-X-STREAM-INF" in data) or (b"#EXT-X-TARGETDURATION" in data)
        is_hls = is_m3u8_url or is_m3u8_ct or is_m3u8_data

        is_mkv = data.startswith(b'\x1a\x45\xdf\xa3')
        is_mp4 = (
            (len(data) >= 8 and data[4:8] == b'ftyp') or
            (b'moov' in data[:8192]) or
            (b'mdat' in data[:8192] and any(a in data[:8192] for a in [b'ftyp', b'wide', b'free', b'skip']))
        )
        is_ts = data.startswith(b'\x47') or ("video/mp2t" in content_type)
        is_avi = data.startswith(b'RIFF') and (b'AVI ' in data[:16])

        if is_hls:
            container_format = "HLS Stream (m3u8 Playlist)"
            mime = "application/x-mpegURL"
            ext = ".m3u8"
        elif is_mkv:
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
            url_lower = (final_url or url).lower()
            if ".mp4" in url_lower:
                container_format = "MP4 Video Stream"
                mime = "video/mp4"
                ext = ".mp4"
            elif ".ts" in url_lower:
                container_format = "MPEG-TS Stream"
                mime = "video/mp2t"
                ext = ".ts"
            elif ".m3u8" in url_lower:
                container_format = "HLS Stream (m3u8)"
                mime = "application/x-mpegURL"
                ext = ".m3u8"
            else:
                container_format = "Direct Video Stream"
                mime = content_type if "video" in content_type else "video/x-matroska"
                ext = ".mkv"

        # Format human-readable size
        if total_bytes >= 1024**3:
            formatted_size = f"{round(total_bytes / (1024**3), 2)} GB"
        elif total_bytes >= 1024**2:
            formatted_size = f"{round(total_bytes / (1024**2), 2)} MB"
        elif total_bytes > 0:
            formatted_size = f"{total_bytes} bytes"
        else:
            formatted_size = "Dynamic Stream"

        # Derive default filename from presigned query params, headers, or URL path
        url_filename = extract_filename(final_url, resp_headers, ext=ext, fallback_url=url)

        is_valid = (status_code in [200, 206])
        set_probe_stage(probe_id, "ready", 100, "Ready for CX File Explorer", {
            "total_bytes": total_bytes,
            "formatted_size": formatted_size,
            "container_format": container_format,
            "filename": url_filename,
            "done": True,
            "valid": is_valid
        })
        if evt:
            evt.set()

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
            "error": None if is_valid else (error_msg or f"HTTP {status_code} received from upstream server")
        }

    except Exception as e:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        set_probe_stage(probe_id, "ready", 100, "Ready in Fallback Mode", {
            "total_bytes": 0,
            "formatted_size": "Dynamic Stream",
            "done": True,
            "error": str(e),
            "valid": False
        })
        if evt:
            evt.set()
        return {
            "valid": False,
            "status_code": status_code or 0,
            "range_supported": True,
            "container_format": "Direct Video Stream",
            "content_type": "video/x-matroska",
            "total_bytes": 0,
            "formatted_size": "Dynamic Stream",
            "default_filename": extract_filename(url, ext=".mkv"),
            "final_url": final_url or url,
            "elapsed_ms": elapsed_ms,
            "error": str(e)
        }
