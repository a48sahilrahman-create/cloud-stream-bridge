"""
Tests for Google CDN Turbo Streaming, Synthetic HEAD, and Container Tail Warming.
Validates:
1. is_google_cdn detection across webdav_engine, stream_probe, and range_proxy.
2. webdav_engine GET with Google CDN URL forces proxy mode (does NOT return 302).
3. webdav_engine HEAD with Google CDN returns 200 OK with Accept-Ranges: bytes and video/mp4.
4. range_proxy streaming with turbo mode / mocked Google CDN responses.
5. stream_probe container tail warming for MP4/Google CDN.
"""

import os
import sys
import asyncio
import pytest
import httpx

# Ensure parent directory is in Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from main import app
from webdav_engine import is_google_cdn, infer_video_type, mount_manager
from stream_probe import (
    is_google_cdn_url,
    warm_mp4_moov_tail,
    warm_container_tail,
    warmed_tails,
    warmed_heads
)
from range_proxy import (
    is_google_cdn_or_turbo,
    stream_range_proxy,
    telemetry_stats,
    get_shared_client
)

client = TestClient(app)

GOOGLE_CDN_SAMPLE_URL = (
    "https://video-downloads.googleusercontent.com/ADGPM2k1lfwCDV67XbM1qfKJWfExMAqffNhCfgJHpkTQL7g2"
    "Bp5oOyc1jqcGLOjDLIEEqOTrA4l7OxPxuyfiKJ6X1OrDVrPfVJG9Ft-xeJ2OYtpukuGyQ5Ck2EZHQT3UIEVjw5p5"
)
GOOGLE_VIDEO_SAMPLE_URL = (
    "https://rr1---sn-4g5ednks.googlevideo.com/videoplayback?expire=1791234567&ei=abc&ip=1.2.3.4"
)
STANDARD_CDN_SAMPLE_URL = "https://cdn.upstream.com/streams/Oppenheimer.2023.2160p.mkv"


class MockStreamResponse:
    """Mock httpx streaming response yielding byte chunks."""
    def __init__(self, data: bytes, status_code: int = 206):
        self.data = data
        self.status_code = status_code
        self.headers = {
            "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}",
            "Content-Length": str(len(data)),
            "Content-Type": "video/mp4",
            "Accept-Ranges": "bytes"
        }

    async def aiter_bytes(self, chunk_size: int = 128 * 1024):
        offset = 0
        while offset < len(self.data):
            chunk = self.data[offset:offset + chunk_size]
            offset += len(chunk)
            yield chunk

    async def aclose(self):
        pass


class MockAsyncClient:
    """Mock httpx.AsyncClient for stream_range_proxy."""
    def __init__(self, chunk_data: bytes = b"GOOGLE_TURBO_STREAM_BYTES" * 64):
        self.chunk_data = chunk_data
        self.is_closed = False

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers)

    async def send(self, request, stream=False):
        return MockStreamResponse(self.chunk_data)


# =============================================================================
# Test 1: is_google_cdn detection
# =============================================================================

def test_is_google_cdn_detection():
    """Verify is_google_cdn accurately detects all Google CDN, Drive, Photos, and Video hosts."""
    # 1. webdav_engine.is_google_cdn
    assert is_google_cdn(GOOGLE_CDN_SAMPLE_URL) is True
    assert is_google_cdn(GOOGLE_VIDEO_SAMPLE_URL) is True
    assert is_google_cdn("https://drive.google.com/uc?id=1AbCdEfGhIjKlMnOp") is True
    assert is_google_cdn("https://photos.google.com/share/AF1QipM-xyz/photo/abc") is True
    assert is_google_cdn(STANDARD_CDN_SAMPLE_URL) is False
    assert is_google_cdn("https://pub.r2.dev/sample_stream.mp4") is False
    assert is_google_cdn("https://archive.org/download/sample/movie.mp4") is False
    assert is_google_cdn("") is False
    assert is_google_cdn(None) is False

    # 2. stream_probe.is_google_cdn_url
    assert is_google_cdn_url(GOOGLE_CDN_SAMPLE_URL) is True
    assert is_google_cdn_url(GOOGLE_VIDEO_SAMPLE_URL) is True
    assert is_google_cdn_url("https://video.google.com/videoplayback?id=123") is True
    assert is_google_cdn_url("https://gvt1.com/edgedl/release2/video.mp4") is True
    assert is_google_cdn_url(STANDARD_CDN_SAMPLE_URL) is False
    assert is_google_cdn_url("") is False
    assert is_google_cdn_url(None) is False

    # 3. range_proxy.is_google_cdn_or_turbo
    assert is_google_cdn_or_turbo(GOOGLE_CDN_SAMPLE_URL) is True
    assert is_google_cdn_or_turbo(GOOGLE_VIDEO_SAMPLE_URL) is True
    assert is_google_cdn_or_turbo("https://storage.googleapis.com/bucket/video.mp4") is True
    assert is_google_cdn_or_turbo("https://example.com/video.mp4?turbo=1") is True
    assert is_google_cdn_or_turbo("https://example.com/video.mp4?turbo=true") is True
    assert is_google_cdn_or_turbo("https://example.com/video.mp4", custom_headers={"x-turbo": "1"}) is True
    assert is_google_cdn_or_turbo("https://example.com/video.mp4", custom_headers={"turbo": "true"}) is True
    assert is_google_cdn_or_turbo("https://example.com/video.mp4", turbo=True) is True
    assert is_google_cdn_or_turbo(GOOGLE_CDN_SAMPLE_URL, turbo=False) is False
    assert is_google_cdn_or_turbo(STANDARD_CDN_SAMPLE_URL) is False


# =============================================================================
# Test 2: webdav_engine GET with Google CDN URL forces proxy mode (NOT 302)
# =============================================================================

def test_webdav_get_google_cdn_forces_proxy_not_302(monkeypatch):
    """
    Verify GET on a mounted Google CDN URL forces proxy mode (never returns 302 Found)
    while a standard CDN URL defaults to direct HTTP 302 Found redirection.
    """
    mock_client = MockAsyncClient()
    monkeypatch.setattr("range_proxy.get_shared_client", lambda: mock_client)

    google_filename = "Google_Turbo_Movie.mp4"
    standard_filename = "Standard_Movie.mkv"

    # Mount 1: Google CDN stream
    mount_manager.add_mount(
        movie_id="test_google_cdn_1",
        filename=google_filename,
        upstream_url=GOOGLE_CDN_SAMPLE_URL,
        total_bytes=104857600,  # 100 MB
        content_type="video/mp4",
        formatted_size="100.0 MB"
    )

    # Mount 2: Standard CDN stream
    mount_manager.add_mount(
        movie_id="test_standard_cdn_1",
        filename=standard_filename,
        upstream_url=STANDARD_CDN_SAMPLE_URL,
        total_bytes=104857600,
        content_type="video/x-matroska",
        formatted_size="100.0 MB"
    )

    try:
        # A. Google CDN GET -> MUST NOT return 302 Found, must proxy directly (200 / 206)
        resp_google = client.get(f"/dav/{google_filename}", follow_redirects=False)
        assert resp_google.status_code != 302, "Google CDN stream must NOT return 302 Found redirect"
        assert resp_google.status_code in (200, 206), f"Expected 200 or 206, got {resp_google.status_code}"
        assert "location" not in resp_google.headers, "Location header must not be set on proxy mode"
        assert resp_google.headers.get("Accept-Ranges") == "bytes"
        assert resp_google.headers.get("Content-Type") == "video/mp4"

        # B. Google CDN GET with Range -> MUST return 206 Partial Content
        resp_google_range = client.get(
            f"/dav/{google_filename}",
            headers={"Range": "bytes=0-1023"},
            follow_redirects=False
        )
        assert resp_google_range.status_code == 206
        assert resp_google_range.headers.get("Content-Range") == "bytes 0-1023/104857600"
        assert resp_google_range.headers.get("Content-Length") == "1024"
        assert "location" not in resp_google_range.headers

        # C. Standard CDN GET -> Defaults to HTTP 302 Found direct redirect
        resp_std = client.get(f"/dav/{standard_filename}", follow_redirects=False)
        assert resp_std.status_code == 302, "Standard CDN stream should default to 302 Found"
        assert resp_std.headers.get("Location") == STANDARD_CDN_SAMPLE_URL
    finally:
        mount_manager.remove_mount(google_filename)
        mount_manager.remove_mount(standard_filename)


# =============================================================================
# Test 3: webdav_engine HEAD with Google CDN returns 200 OK & video/mp4
# =============================================================================

def test_webdav_head_google_cdn_returns_200_and_mp4():
    """
    Verify WebDAV HEAD for Google CDN stream returns synthetic HTTP 200 OK
    with Accept-Ranges: bytes, exact Content-Length, and inferred video/mp4.
    """
    filename = "Google_Stream_Clip.mp4"
    total_bytes = 52428800  # 50 MB

    mount_manager.add_mount(
        movie_id="test_google_head_1",
        filename=filename,
        upstream_url=GOOGLE_CDN_SAMPLE_URL,
        total_bytes=total_bytes,
        content_type="video/mp4",
        formatted_size="50.0 MB"
    )

    # Mount another with raw octet-stream to test MIME type auto-correction
    raw_filename = "Raw_Google_Stream.mp4"
    mount_manager.add_mount(
        movie_id="test_google_head_raw",
        filename=raw_filename,
        upstream_url=GOOGLE_VIDEO_SAMPLE_URL,
        total_bytes=total_bytes,
        content_type="application/octet-stream",
        formatted_size="50.0 MB"
    )

    try:
        # 1. Standard HEAD probe
        resp_head = client.head(f"/dav/{filename}")
        assert resp_head.status_code == 200, "HEAD probe must return 200 OK"
        assert resp_head.headers.get("Accept-Ranges") == "bytes"
        assert resp_head.headers.get("Content-Type") == "video/mp4"
        assert resp_head.headers.get("Content-Length") == str(total_bytes)
        assert resp_head.headers.get("Cache-Control") == "no-cache, no-store, must-revalidate"
        assert "location" not in resp_head.headers

        # 2. Raw octet-stream MIME auto-correction for Google CDN
        resp_head_raw = client.head(f"/dav/{raw_filename}")
        assert resp_head_raw.status_code == 200
        assert resp_head_raw.headers.get("Content-Type") == "video/mp4", (
            "Google CDN streams must infer video/mp4 over octet-stream"
        )
        assert resp_head_raw.headers.get("Accept-Ranges") == "bytes"
    finally:
        mount_manager.remove_mount(filename)
        mount_manager.remove_mount(raw_filename)


# =============================================================================
# Test 4: range_proxy streaming with turbo mode / mocked Google CDN responses
# =============================================================================

@pytest.mark.asyncio
async def test_range_proxy_turbo_streaming_mocked_google_cdn(monkeypatch):
    """
    Verify stream_range_proxy activates turbo-pipelined streaming for Google CDN URLs,
    populates X-Turbo-Prefetch and X-Streaming-Mode headers, and serves chunks.
    """
    mock_data = b"TURBO_CHUNK_" * 100
    mock_client = MockAsyncClient(chunk_data=mock_data)
    monkeypatch.setattr("range_proxy.get_shared_client", lambda: mock_client)

    initial_turbo_count = telemetry_stats.get("turbo_requests_served", 0)

    # 1. Google CDN URL triggers turbo mode automatically
    resp_turbo = await stream_range_proxy(
        upstream_url=GOOGLE_CDN_SAMPLE_URL,
        range_header="bytes=0-1023",
        total_size=10485760,  # 10 MB
        content_type="video/mp4"
    )

    assert resp_turbo.status_code == 206
    assert resp_turbo.headers.get("Accept-Ranges") == "bytes"
    assert resp_turbo.headers.get("Content-Range") == "bytes 0-1023/10485760"
    assert resp_turbo.headers.get("Content-Length") == "1024"
    assert resp_turbo.headers.get("X-Turbo-Prefetch") == "active"
    assert resp_turbo.headers.get("X-Streaming-Mode") == "turbo-pipelined"

    # Consume stream chunks
    chunks = []
    async for chunk in resp_turbo.body_iterator:
        chunks.append(chunk)

    assert len(chunks) > 0
    total_received = sum(len(c) for c in chunks)
    assert total_received > 0
    assert telemetry_stats.get("turbo_requests_served", 0) > initial_turbo_count

    # 2. Non-Google CDN URL with turbo=False uses standard streaming mode
    resp_std = await stream_range_proxy(
        upstream_url=STANDARD_CDN_SAMPLE_URL,
        range_header="bytes=0-511",
        total_size=10485760,
        content_type="video/x-matroska",
        turbo=False
    )
    assert resp_std.status_code == 206
    assert "X-Turbo-Prefetch" not in resp_std.headers
    assert "X-Streaming-Mode" not in resp_std.headers


# =============================================================================
# Test 5: stream_probe container tail warming for MP4/Google CDN
# =============================================================================

@pytest.mark.asyncio
async def test_stream_probe_container_tail_warming_mp4_google_cdn(monkeypatch):
    """
    Verify stream_probe pre-warms tail 2MB (moov atom) for Google CDN and MP4 files,
    and tail 64KB (Cues) for MKV files.
    """
    warmed_tails.clear()
    warmed_heads.clear()

    recorded_ranges = []

    class MockStreamContext:
        def __init__(self, status_code, headers, data):
            self.status_code = status_code
            self.headers = headers
            self.data = data

        async def aiter_bytes(self):
            yield self.data

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class MockProbeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        def stream(self, method, url, headers=None):
            req_range = (headers or {}).get("Range", "")
            recorded_ranges.append(req_range)
            return MockStreamContext(206, {"content-range": f"{req_range}/52428800"}, b"atom_data")

    monkeypatch.setattr("httpx.AsyncClient", MockProbeAsyncClient)

    # 1. Google CDN URL with > 2MB size triggers 2MB moov tail warming (bytes=-2097152)
    large_size = 52428800  # 50 MB
    success_google = await warm_container_tail(
        url=GOOGLE_CDN_SAMPLE_URL,
        total_bytes=large_size,
        container_type="auto"
    )
    assert success_google is True
    expected_google_range = f"bytes={large_size - 2097152}-{large_size - 1}"
    assert expected_google_range in recorded_ranges

    # 2. MP4 moov tail warming directly
    warmed_tails.clear()
    recorded_ranges.clear()
    success_mp4 = await warm_mp4_moov_tail(
        url="https://cdn.example.com/movie.mp4",
        total_bytes=large_size
    )
    assert success_mp4 is True
    assert expected_google_range in recorded_ranges

    # 3. Small file (<= 2MB) skips moov tail warming
    recorded_ranges.clear()
    small_size = 1048576  # 1 MB
    success_small = await warm_container_tail(
        url=GOOGLE_CDN_SAMPLE_URL,
        total_bytes=small_size,
        container_type="auto"
    )
    assert success_small is False
    assert len(recorded_ranges) == 0

    # 4. Standard MKV triggers 64KB tail warming (bytes=-65536)
    warmed_tails.clear()
    recorded_ranges.clear()
    success_mkv = await warm_container_tail(
        url=STANDARD_CDN_SAMPLE_URL,
        total_bytes=large_size,
        container_type="auto"
    )
    assert success_mkv is True
    expected_mkv_range = f"bytes={large_size - 65536}-{large_size - 1}"
    assert expected_mkv_range in recorded_ranges
