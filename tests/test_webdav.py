"""
Unit & Integration Test Suite for CloudStream WebDAV Bridge
Verifies RFC 4918 WebDAV Compliance, RFC 7233 Range Handling,
Magic Bytes Container Probing, and FastAPI Endpoints.
"""

import os
import sys
import pytest
import xml.etree.ElementTree as ET

# Ensure parent directory is in Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.requests import Request
from starlette.testclient import TestClient

from range_proxy import parse_byte_range
from webdav_engine import mount_manager, build_propfind_xml
from main import app

client = TestClient(app)


def test_parse_byte_range():
    """Verify Range header parsing across all RFC 7233 variants."""
    total_size = 100 * 1024 * 1024 * 1024  # 100 GB

    # Standard range: bytes=0-1048575 (first 1 MB)
    start, end = parse_byte_range("bytes=0-1048575", total_size)
    assert start == 0
    assert end == 1048575

    # Seek range: bytes=52428800000- (seek to 50 GB)
    start, end = parse_byte_range("bytes=52428800000-", total_size)
    assert start == 52428800000
    assert end == total_size - 1

    # Suffix range: bytes=-1048576 (last 1 MB)
    start, end = parse_byte_range("bytes=-1048576", total_size)
    assert start == total_size - 1048576
    assert end == total_size - 1

    # Boundary overflow
    start, end = parse_byte_range(f"bytes=0-{total_size + 1000}", total_size)
    assert end == total_size - 1

    # Empty / None header
    start, end = parse_byte_range(None, total_size)
    assert start == 0
    assert end == total_size - 1


def test_webdav_options():
    """Verify WebDAV OPTIONS discovery for CX File Explorer and Windows Explorer."""
    response = client.options("/dav/")
    assert response.status_code == 200
    assert response.headers.get("DAV") == "1"
    assert "PROPFIND" in response.headers.get("Allow", "")
    assert response.headers.get("Accept-Ranges") == "bytes"
    assert response.headers.get("Content-Length") == "0"


def test_root_webdav_options_and_propfind():
    """Verify Root '/' WebDAV OPTIONS and PROPFIND compliance for CX File Explorer."""
    # 1. Root OPTIONS
    resp_opt = client.options("/")
    assert resp_opt.status_code == 200
    assert resp_opt.headers.get("DAV") == "1"
    assert "PROPFIND" in resp_opt.headers.get("Allow", "")
    assert resp_opt.headers.get("Content-Length") == "0"

    # 2. Root PROPFIND Depth: 0
    resp_p0 = client.request("PROPFIND", "/", headers={"Depth": "0"})
    assert resp_p0.status_code == 207
    assert "<D:multistatus" in resp_p0.text
    assert "Content-Length" in resp_p0.headers
    assert int(resp_p0.headers["Content-Length"]) == len(resp_p0.content)

    # 3. Root PROPFIND Depth: 1
    resp_p1 = client.request("PROPFIND", "/", headers={"Depth": "1"})
    assert resp_p1.status_code == 207
    assert "<D:multistatus" in resp_p1.text
    assert "Content-Length" in resp_p1.headers
    assert int(resp_p1.headers["Content-Length"]) == len(resp_p1.content)

    # 4. XML compliance with RFC 4918 properties
    root = ET.fromstring(resp_p1.content)
    tags = [elem.tag for elem in root.iter()]
    assert any("getetag" in t for t in tags)
    assert any("creationdate" in t for t in tags)
    assert any("getlastmodified" in t for t in tags)


def test_dav_without_trailing_slash_no_redirect():
    """Verify /dav without trailing slash returns 200/207 without 307/308 redirect."""
    # OPTIONS /dav
    resp_opt = client.options("/dav", follow_redirects=False)
    assert resp_opt.status_code == 200
    assert resp_opt.status_code not in (301, 302, 307, 308)
    assert resp_opt.headers.get("DAV") == "1"
    assert "PROPFIND" in resp_opt.headers.get("Allow", "")

    # PROPFIND /dav
    resp_pf = client.request("PROPFIND", "/dav", headers={"Depth": "0"}, follow_redirects=False)
    assert resp_pf.status_code == 207
    assert resp_pf.status_code not in (301, 302, 307, 308)
    assert "<D:multistatus" in resp_pf.text
    assert "Content-Length" in resp_pf.headers


def test_webdav_content_length_on_multistatus():
    """Verify Content-Length header is present and exact on all 207 Multi-Status responses."""
    endpoints = ["/", "/dav", "/dav/"]
    for ep in endpoints:
        for depth in ["0", "1"]:
            resp = client.request("PROPFIND", ep, headers={"Depth": depth})
            assert resp.status_code == 207
            cl = resp.headers.get("Content-Length")
            assert cl is not None, f"Missing Content-Length on PROPFIND {ep} (Depth: {depth})"
            assert cl.isdigit()
            assert int(cl) == len(resp.content)


def test_presigned_r2_query_param_filename_extraction():
    """Verify authentic filename extraction from presigned Cloudflare R2 / AWS S3 URLs."""
    from stream_probe import extract_filename

    # 1. Presigned R2 URL with response-content-disposition query parameter
    r2_url = (
        "https://pub-abc123xyz.r2.dev/stream"
        "?response-content-disposition=attachment%3B%20filename%3D%22Oppenheimer.2023.2160p.UHD.Remux.mkv%22"
        "&X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=test&X-Amz-Date=20261002T000000Z"
    )
    fn = extract_filename(r2_url)
    assert fn == "Oppenheimer.2023.2160p.UHD.Remux.mkv"

    # 2. Presigned R2/S3 URL with RFC 5987 encoded filename*
    r2_rfc5987 = (
        "https://bucket.r2.cloudflarestorage.com/media/blob"
        "?response-content-disposition=attachment%3B%20filename%2A%3DUTF-8%27%27Dune.Part.Two.2024.mkv"
        "&X-Amz-Expires=3600"
    )
    fn2 = extract_filename(r2_rfc5987)
    assert fn2 == "Dune.Part.Two.2024.mkv"

    # 3. Presigned URL with content-disposition response header fallback
    s3_hdr_url = "https://s3.us-east-1.amazonaws.com/media/stream_blob_xyz?sig=123"
    fn3 = extract_filename(s3_hdr_url, resp_headers={"content-disposition": 'attachment; filename="Interstellar.2014.2160p.mkv"'})
    assert fn3 == "Interstellar.2014.2160p.mkv"

    # 4. Standard clean URL path
    clean_url = "https://cdn.example.com/movies/Avatar.2009.Remux.mkv"
    fn4 = extract_filename(clean_url)
    assert fn4 == "Avatar.2009.Remux.mkv"

    # 5. URL with sanitized special characters
    dirty_url = "https://pub.r2.dev/stream?response-content-disposition=attachment%3B%20filename%3D%22Movie%3A%20The%20Sequel%20%282024%29.mkv%22"
    fn5 = extract_filename(dirty_url)
    assert ":" not in fn5
    assert fn5.endswith(".mkv")


def test_single_file_propfind():
    """Verify single file PROPFIND /dav/<filename> returns 207 with 1 response."""
    test_mount = mount_manager.add_mount(
        movie_id="single_file_test",
        filename="SingleFileTest.mkv",
        upstream_url="https://example.com/single.mkv",
        total_bytes=10000000000,
        content_type="video/x-matroska",
        formatted_size="9.31 GB"
    )
    try:
        resp = client.request("PROPFIND", "/dav/SingleFileTest.mkv")
        assert resp.status_code == 207
        assert "Content-Length" in resp.headers
        assert int(resp.headers["Content-Length"]) == len(resp.content)
        root = ET.fromstring(resp.content)
        responses = [elem for elem in root.iter() if elem.tag.endswith("response")]
        assert len(responses) == 1
        assert "SingleFileTest.mkv" in resp.text
    finally:
        mount_manager.remove_mount("SingleFileTest.mkv")


def test_webdav_propfind_xml_generation():
    """Verify RFC 4918 multistatus XML schema parsing."""
    # Register test movie mount
    test_mount = mount_manager.add_mount(
        movie_id="test_movie_1",
        filename="Test_4K_Remux.mkv",
        upstream_url="https://example.com/stream.mkv",
        total_bytes=75000000000,  # 75 GB
        content_type="video/x-matroska",
        formatted_size="69.85 GB",
        title="Test 4K Remux Movie"
    )

    xml_content = build_propfind_xml("/dav/", depth="1")
    assert "<?xml" in xml_content
    assert "<D:multistatus" in xml_content
    assert "Test_4K_Remux.mkv" in xml_content
    assert "75000000000" in xml_content
    assert "video/x-matroska" in xml_content

    # Validate XML parser can load without syntax errors
    root = ET.fromstring(xml_content)
    assert root is not None

    # Clean up test mount
    mount_manager.remove_mount("Test_4K_Remux.mkv")


def test_fastapi_endpoints():
    """Verify Web UI, status telemetry, and mount APIs."""
    # 1. Web UI Index
    resp = client.get("/")
    assert resp.status_code == 200
    assert "CloudStream WebDAV Bridge" in resp.text

    # 2. Telemetry Status
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "online"
    assert "home_bandwidth_saved_gb" in data

    # 3. List Mounts
    resp = client.get("/api/mounts")
    assert resp.status_code == 200
    assert "mounts" in resp.json()


def test_webdav_head_request():
    """Verify HEAD returns accurate file size and byte range support."""
    mount_manager.add_mount(
        movie_id="head_test",
        filename="HeadTest.mkv",
        upstream_url="https://example.com/head.mkv",
        total_bytes=50000000000,
        content_type="video/x-matroska",
        formatted_size="46.57 GB"
    )

    resp = client.head("/dav/HeadTest.mkv")
    assert resp.status_code == 200
    assert resp.headers.get("Accept-Ranges") == "bytes"
    assert resp.headers.get("Content-Length") == "50000000000"
    assert resp.headers.get("Content-Type") == "video/x-matroska"

    mount_manager.remove_mount("HeadTest.mkv")


@pytest.mark.asyncio
async def test_stream_probe_container_detection(monkeypatch):
    """Verify stream probe accurately detects MKV EBML magic bytes and Content-Range total size."""
    from stream_probe import probe_stream

    class MockStreamContext:
        def __init__(self, status_code, headers, data):
            self.status_code = status_code
            self.headers = headers
            self.data = data
            self.url = "https://cdn.example.com/movies/sample_remux.mkv"

        async def aiter_bytes(self):
            yield self.data

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class MockAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        def stream(self, method, url, headers=None):
            # EBML magic bytes: 0x1A 0x45 0xDF 0xA3 followed by dummy data
            mkv_data = b"\x1a\x45\xdf\xa3" + b"\x00" * 8188
            resp_headers = {
                "content-range": "bytes 0-8191/85899345920",  # 80 GB
                "content-type": "video/x-matroska",
                "accept-ranges": "bytes"
            }
            return MockStreamContext(206, resp_headers, mkv_data)

    monkeypatch.setattr("httpx.AsyncClient", MockAsyncClient)

    result = await probe_stream("https://cdn.example.com/movies/sample_remux.mkv")
    assert result["valid"] is True
    assert result["range_supported"] is True
    assert "MKV (Matroska" in result["container_format"]
    assert result["total_bytes"] == 85899345920
    assert "80.0 GB" in result["formatted_size"]


if __name__ == "__main__":
    pytest.main(["-v", __file__])

