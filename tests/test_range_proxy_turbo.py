"""
Tests for Range Proxy Turbo Multi-Connection Pre-buffering & Google CDN Detection Engine
"""

import asyncio
import pytest
import httpx
from range_proxy import (
    is_google_cdn_or_turbo,
    parse_byte_range,
    stream_range_proxy,
    fetch_segment_data,
    telemetry_stats,
    TURBO_SEGMENT_SIZE,
    CHUNK_SIZE
)


def test_google_cdn_and_turbo_detection():
    """Verify Google CDN domain patterns, query params, headers, and overrides."""
    # 1. Google CDN domain matching
    assert is_google_cdn_or_turbo("https://rr1---sn-4g5edn6s.googlevideo.com/videoplayback?expire=123") is True
    assert is_google_cdn_or_turbo("https://storage.googleapis.com/stream-bucket/remux.mkv") is True
    assert is_google_cdn_or_turbo("https://drive.google.com/uc?id=file123") is True
    assert is_google_cdn_or_turbo("http://edge-node.1e100.net/data") is True
    assert is_google_cdn_or_turbo("https://redirector.gvt1.com/videoplayback") is True

    # 1b. Google UploadServer does NOT support Range segments and defaults to False
    assert is_google_cdn_or_turbo("https://doc-04-00-docs.googleusercontent.com/docs/securesc/xyz") is False
    assert is_google_cdn_or_turbo("https://video-downloads.googleusercontent.com/xyz") is False
    assert is_google_cdn_or_turbo("https://video-downloads.googleusercontent.com/xyz?turbo=1") is True
    assert is_google_cdn_or_turbo("https://video-downloads.googleusercontent.com/xyz", turbo=True) is True

    # 2. Standard / non-Google URLs
    assert is_google_cdn_or_turbo("https://cdn.cloudflare.com/remux.mkv") is False
    assert is_google_cdn_or_turbo("https://fastly.net/video.mp4") is False
    assert is_google_cdn_or_turbo("http://192.168.1.100:8000/stream.mkv") is False

    # 3. Query parameter turbo triggers
    assert is_google_cdn_or_turbo("https://cdn.cloudflare.com/remux.mkv?turbo=1") is True
    assert is_google_cdn_or_turbo("https://generic-cdn.org/video.mp4?auth=abc&turbo=true") is True

    # 4. Custom header triggers
    assert is_google_cdn_or_turbo("https://generic-cdn.org/video.mp4", custom_headers={"X-Turbo": "1"}) is True
    assert is_google_cdn_or_turbo("https://generic-cdn.org/video.mp4", custom_headers={"turbo": "true"}) is True
    assert is_google_cdn_or_turbo("https://generic-cdn.org/video.mp4", custom_headers={"Other": "val"}) is False

    # 5. Explicit overrides
    assert is_google_cdn_or_turbo("https://rr1.googlevideo.com/video", turbo=False) is False
    assert is_google_cdn_or_turbo("https://generic-cdn.org/video.mp4", turbo=True) is True


@pytest.mark.asyncio
async def test_zero_byte_guard():
    """Ensure 0-Byte Guard returns 503 Retry-After for unverified/0-byte streams."""
    resp = await stream_range_proxy(
        upstream_url="https://storage.googleapis.com/test.mkv",
        range_header="bytes=0-100",
        total_size=0,
        content_type="video/x-matroska"
    )
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "2"


@pytest.mark.asyncio
async def test_standard_stream_fallback(monkeypatch):
    """Verify non-Google standard streams bypass turbo and stream zero-copy."""
    mock_payload = b"A" * (256 * 1024)  # 256 KB
    upstream_url = "https://standard-cdn.org/video.mkv"

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                206,
                headers={"Content-Length": str(len(mock_payload)), "Content-Range": f"bytes 0-{len(mock_payload)-1}/{len(mock_payload)}"},
                content=mock_payload
            )

    mock_client = httpx.AsyncClient(transport=MockTransport())
    monkeypatch.setattr("range_proxy.get_shared_client", lambda: mock_client)

    resp = await stream_range_proxy(
        upstream_url=upstream_url,
        range_header=f"bytes=0-{len(mock_payload)-1}",
        total_size=len(mock_payload),
        content_type="video/x-matroska"
    )

    assert resp.status_code == 206
    assert resp.headers.get("X-Turbo-Prefetch") is None
    assert resp.headers.get("Content-Length") == str(len(mock_payload))

    # Consume stream chunks
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk)

    received = b"".join(chunks)
    assert received == mock_payload
    await mock_client.aclose()


@pytest.mark.asyncio
async def test_turbo_multiconnection_prebuffering(monkeypatch):
    """
    Verify multi-connection pre-buffering on a 5 MB Google CDN stream (3 segments).
    Checks:
      1. Correct segmentation into 2 MB chunks + 1 MB tail
      2. Upstream Range headers for each segment
      3. Turbo headers and exact byte integrity
      4. Decoupled prefetch telemetry
    """
    total_size = 5 * 1024 * 1024  # 5 MB
    # Generate deterministic 5 MB buffer
    synthetic_data = bytes((i % 256) for i in range(total_size))
    google_url = "https://rr2---sn-4g5edn6s.googlevideo.com/videoplayback?id=stream123"

    requested_ranges = []

    class MockGoogleTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            range_val = request.headers.get("Range", "")
            requested_ranges.append(range_val)
            # Parse requested slice
            if range_val.startswith("bytes="):
                spec = range_val[6:]
                s_str, e_str = spec.split("-")
                s, e = int(s_str), int(e_str)
                slice_data = synthetic_data[s:e+1]
            else:
                slice_data = synthetic_data

            return httpx.Response(
                206,
                headers={
                    "Content-Range": f"bytes {s}-{e}/{total_size}",
                    "Content-Length": str(len(slice_data))
                },
                content=slice_data
            )

    mock_client = httpx.AsyncClient(transport=MockGoogleTransport())
    monkeypatch.setattr("range_proxy.get_shared_client", lambda: mock_client)

    initial_turbo_requests = telemetry_stats.get("turbo_requests_served", 0)

    resp = await stream_range_proxy(
        upstream_url=google_url,
        range_header=f"bytes=0-{total_size-1}",
        total_size=total_size,
        content_type="video/mp4"
    )

    assert resp.status_code == 206
    assert resp.headers.get("X-Turbo-Prefetch") == "active"
    assert resp.headers.get("X-Streaming-Mode") == "turbo-pipelined"
    assert resp.headers.get("X-Accel-Buffering") == "no"
    assert resp.headers.get("Content-Length") == str(total_size)

    # Consume all chunks yielded by the turbo generator
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk)

    received = b"".join(chunks)
    assert len(received) == total_size
    assert received == synthetic_data
    assert telemetry_stats.get("turbo_requests_served", 0) == initial_turbo_requests + 1

    # Verify that multi-connection segmented ranges were requested:
    # Seg 0: bytes=0-2097151
    # Seg 1: bytes=2097152-4194303
    # Seg 2: bytes=4194304-5242879
    assert any("bytes=0-2097151" in r for r in requested_ranges)
    assert any("bytes=2097152-4194303" in r for r in requested_ranges)
    assert any("bytes=4194304-5242879" in r for r in requested_ranges)
    await mock_client.aclose()


@pytest.mark.asyncio
async def test_turbo_immediate_disconnect_trap(monkeypatch):
    """Verify sub-10ms task cancellation and clean active stream decrement on client abort."""
    total_size = 10 * 1024 * 1024  # 10 MB (5 segments)
    google_url = "https://storage.googleapis.com/bucket/bigvideo.mkv"
    in_flight_tasks = []

    class MockSlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            # Simulate streaming delay
            async def delayed_chunks():
                for _ in range(16):
                    await asyncio.sleep(0.01)
                    yield b"X" * (128 * 1024)
            return httpx.Response(206, content=delayed_chunks())

    mock_client = httpx.AsyncClient(transport=MockSlowTransport())
    monkeypatch.setattr("range_proxy.get_shared_client", lambda: mock_client)

    active_streams_before = telemetry_stats.get("active_streams", 0)

    resp = await stream_range_proxy(
        upstream_url=google_url,
        range_header=f"bytes=0-{total_size-1}",
        total_size=total_size,
        content_type="video/x-matroska"
    )

    iterator = resp.body_iterator
    # Read only 1 chunk, then abort/close iterator (simulates seek / timeline scrub)
    first_chunk = await iterator.__anext__()
    assert len(first_chunk) > 0

    # Abort generator
    await iterator.aclose()
    # Allow event loop tick for cleanup
    await asyncio.sleep(0.02)

    # Verify active_streams decremented cleanly back to original
    assert telemetry_stats.get("active_streams", 0) == active_streams_before
    await mock_client.aclose()


@pytest.mark.asyncio
async def test_fetch_segment_data_raises_value_error_on_200_for_offset():
    """Verify fetch_segment_data raises ValueError if s_start > 0 and origin returns 200 OK."""
    class MockOrigin200Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"fake data from byte 0")

    mock_client = httpx.AsyncClient(transport=MockOrigin200Transport())
    with pytest.raises(ValueError, match="origin does not support Range"):
        await fetch_segment_data(
            client=mock_client,
            url="https://video-downloads.googleusercontent.com/test",
            s_start=2097152,
            s_end=4194303,
            headers={},
            retries=0
        )
    await mock_client.aclose()


@pytest.mark.asyncio
async def test_standard_stream_slice_and_skip_on_200(monkeypatch):
    """Verify standard_chunk_generator slices and skips when upstream returns 200 OK for a range request."""
    full_payload = bytes(range(256)) * 4  # 1024 bytes (0..255 repeated 4 times)
    target_start = 100
    target_end = 299
    expected_slice = full_payload[target_start:target_end + 1]

    class MockOriginNoRangeTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            # Origin ignores Range header and returns 200 OK with full payload
            return httpx.Response(200, content=full_payload)

    mock_client = httpx.AsyncClient(transport=MockOriginNoRangeTransport())
    monkeypatch.setattr("range_proxy.get_shared_client", lambda: mock_client)

    resp = await stream_range_proxy(
        upstream_url="https://video-downloads.googleusercontent.com/stream.mp4",
        range_header=f"bytes={target_start}-{target_end}",
        total_size=len(full_payload),
        content_type="video/mp4"
    )

    assert resp.status_code == 206
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk)

    received = b"".join(chunks)
    assert len(received) == len(expected_slice)
    assert received == expected_slice
    await mock_client.aclose()

