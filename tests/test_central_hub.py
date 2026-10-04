"""
Unit and Integration Test Suite for Central Pointer Hub (FastAPI)
Validates registration, active status, TTL expiration, HTTP 302 dynamic redirects,
dormant 503 XML responses, mount request forwarding, and thread-safety.
"""

import os
import sys
import time
import pytest
from unittest.mock import patch, AsyncMock
from concurrent.futures import ThreadPoolExecutor

# Ensure parent directory is in Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
import httpx

from central_hub import app, registry, InMemoryUserRegistry, DORMANT_XML_RESPONSE

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure a pristine registry before and after each test."""
    registry.clear()
    yield
    registry.clear()


# ============================================================================
# 1. Health & Root Endpoints
# ============================================================================

def test_health_and_root_endpoints():
    """Verify health endpoint returns healthy status and metrics."""
    res_health = client.get("/health")
    assert res_health.status_code == 200
    data_h = res_health.json()
    assert data_h["status"] == "healthy"
    assert data_h["service"] == "cloudstream-central-hub"
    assert data_h["active_users"] == 0
    assert data_h["total_registered"] == 0

    res_root = client.get("/")
    assert res_root.status_code == 200
    data_r = res_root.json()
    assert data_r["status"] == "healthy"


# ============================================================================
# 2. User Registration (/api/register)
# ============================================================================

def test_register_user_success():
    """Register a new Cloud Shell user tunnel."""
    payload = {
        "user_id": "usr_alpha_123",
        "tunnel_url": "https://fast-tunnel.trycloudflare.com",
        "token": "secret_token_abc",
        "ttl_sec": 3600,
    }
    response = client.post("/api/register", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "registered"
    assert data["user_id"] == "usr_alpha_123"
    assert data["tunnel_url"] == "https://fast-tunnel.trycloudflare.com"
    assert data["active"] is True

    # Check health reflects the new active user
    h_res = client.get("/health")
    assert h_res.json()["active_users"] == 1
    assert h_res.json()["total_registered"] == 1


def test_register_user_update_existing():
    """Updating an existing user updates tunnel and refreshes last_seen while keeping registered_at."""
    client.post("/api/register", json={
        "user_id": "usr_repeat",
        "tunnel_url": "https://old-tunnel.trycloudflare.com",
    })
    entry1 = registry.get("usr_repeat")
    orig_registered_at = entry1["registered_at"]

    # Short delay to test last_seen change
    time.sleep(0.01)

    # Re-register with new tunnel URL
    res2 = client.post("/api/register", json={
        "user_id": "usr_repeat",
        "tunnel_url": "https://new-tunnel.trycloudflare.com/",
    })
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["tunnel_url"] == "https://new-tunnel.trycloudflare.com"

    entry2 = registry.get("usr_repeat")
    assert entry2["registered_at"] == orig_registered_at
    assert entry2["tunnel_url"] == "https://new-tunnel.trycloudflare.com"
    assert entry2["last_seen"] >= entry1["last_seen"]


def test_register_user_validation_error():
    """Empty user_id or tunnel_url must fail validation with 422."""
    res1 = client.post("/api/register", json={"user_id": "", "tunnel_url": "https://tunnel.com"})
    assert res1.status_code == 422

    res2 = client.post("/api/register", json={"user_id": "usr_valid", "tunnel_url": ""})
    assert res2.status_code == 422


# ============================================================================
# 3. User Status (/api/status/{user_id})
# ============================================================================

def test_status_active_user():
    """Active user status reports active=True and valid remaining TTL."""
    client.post("/api/register", json={
        "user_id": "usr_active_99",
        "tunnel_url": "https://active-tunnel.trycloudflare.com",
        "ttl_sec": 3600,
    })

    res = client.get("/api/status/usr_active_99")
    assert res.status_code == 200
    data = res.json()
    assert data["active"] is True
    assert data["user_id"] == "usr_active_99"
    assert data["tunnel_url"] == "https://active-tunnel.trycloudflare.com"
    assert data["ttl_remaining_sec"] > 3500
    assert isinstance(data["last_seen"], float)


def test_status_unknown_user():
    """Non-existent user reports active=False and ttl_remaining_sec=0."""
    res = client.get("/api/status/ghost_user_404")
    assert res.status_code == 200
    data = res.json()
    assert data["active"] is False
    assert data["user_id"] == "ghost_user_404"
    assert data["tunnel_url"] is None
    assert data["last_seen"] is None
    assert data["ttl_remaining_sec"] == 0


def test_status_expired_user():
    """Expired user returns active=False and ttl_remaining_sec=0."""
    # Register with short TTL
    registry.register(
        user_id="usr_expiring",
        tunnel_url="https://expired-tunnel.trycloudflare.com",
        ttl_sec=1,
    )

    # Mock time.time() to simulate time advancing past TTL
    future_time = time.time() + 10.0
    with patch("time.time", return_value=future_time):
        res = client.get("/api/status/usr_expiring")
        assert res.status_code == 200
        data = res.json()
        assert data["active"] is False
        assert data["user_id"] == "usr_expiring"
        assert data["tunnel_url"] is None
        assert data["ttl_remaining_sec"] == 0


# ============================================================================
# 4. WebDAV Dynamic 302 Redirection (/dav/{user_id}/{path})
# ============================================================================

def test_dav_redirect_active_user_file():
    """Active user receives HTTP 302 redirect directly to Cloudflare tunnel with CORS."""
    client.post("/api/register", json={
        "user_id": "usr_streamer",
        "tunnel_url": "https://cloudstream-node1.trycloudflare.com",
    })

    response = client.get(
        "/dav/usr_streamer/movies/avatar_way_of_water.mkv",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "https://cloudstream-node1.trycloudflare.com/dav/movies/avatar_way_of_water.mkv"
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.headers["Access-Control-Allow-Methods"] == "*"


def test_dav_redirect_with_query_string():
    """Dynamic redirect preserves query string (e.g. streaming tokens or seek params)."""
    client.post("/api/register", json={
        "user_id": "usr_query_test",
        "tunnel_url": "https://cf-tunnel.trycloudflare.com",
    })

    response = client.get(
        "/dav/usr_query_test/video.mkv?token=auth_token_999&t=120",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "https://cf-tunnel.trycloudflare.com/dav/video.mkv?token=auth_token_999&t=120"


def test_dav_redirect_root_dav_folder():
    """Accessing root WebDAV path redirects to /dav/ on the user's tunnel."""
    client.post("/api/register", json={
        "user_id": "usr_root_check",
        "tunnel_url": "https://cf-tunnel.trycloudflare.com",
    })

    # Test /dav/usr_root_check
    res1 = client.get("/dav/usr_root_check", follow_redirects=False)
    assert res1.status_code == 302
    assert res1.headers["Location"] == "https://cf-tunnel.trycloudflare.com/dav/"

    # Test /dav/usr_root_check/
    res2 = client.get("/dav/usr_root_check/", follow_redirects=False)
    assert res2.status_code == 302
    assert res2.headers["Location"] == "https://cf-tunnel.trycloudflare.com/dav/"


def test_dav_redirect_http_methods():
    """Streaming methods (GET, HEAD) receive HTTP 302 Found redirect preserving zero video proxying."""
    client.post("/api/register", json={
        "user_id": "usr_methods",
        "tunnel_url": "https://cf-methods.trycloudflare.com",
    })

    for method in ["GET", "HEAD"]:
        res = client.request(
            method=method,
            url="/dav/usr_methods/library",
            follow_redirects=False,
        )
        assert res.status_code == 302
        assert res.headers["Location"] == "https://cf-methods.trycloudflare.com/dav/library"
        assert res.headers["DAV"] == "1"


def test_dav_proxy_metadata_methods():
    """WebDAV metadata and directory queries (PROPFIND, OPTIONS, PROPPATCH, MKCOL, DELETE) are reverse-proxied."""
    client.post("/api/register", json={
        "user_id": "usr_proxy_methods",
        "tunnel_url": "https://cf-proxy.trycloudflare.com",
    })

    mock_resp = httpx.Response(
        status_code=207,
        content=b'<?xml version="1.0"?><multistatus></multistatus>',
        headers={"content-type": "application/xml; charset=utf-8"},
    )

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp

        for method in ["PROPFIND", "OPTIONS", "PROPPATCH", "MKCOL", "DELETE"]:
            res = client.request(
                method=method,
                url="/dav/usr_proxy_methods/folder/subfolder",
                headers={"Depth": "1", "Authorization": "Basic test", "Host": "centralhub.local"},
                content=b"<propfind/>",
                follow_redirects=False,
            )
            assert res.status_code == 207
            assert res.headers["DAV"] == "1"
            assert res.headers["Access-Control-Allow-Origin"] == "*"
            assert res.content == b'<?xml version="1.0"?><multistatus></multistatus>'

            mock_req.assert_called()
            call_kwargs = mock_req.call_args.kwargs
            assert call_kwargs["method"] == method
            assert call_kwargs["url"] == "https://cf-proxy.trycloudflare.com/dav/folder/subfolder"
            assert "host" not in [k.lower() for k in call_kwargs["headers"].keys()]
            assert call_kwargs["headers"].get("depth") == "1"
            assert call_kwargs["content"] == b"<propfind/>"


# ============================================================================
# 5. Dormant 503 XML Handling
# ============================================================================

def test_dav_dormant_user_returns_503_xml():
    """Unknown or offline user receives HTTP 503 with standardized CX File Explorer XML body."""
    response = client.get("/dav/dormant_user_123/movie.mp4", follow_redirects=False)
    assert response.status_code == 503
    assert "application/xml" in response.headers.get("content-type", "")
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.text == DORMANT_XML_RESPONSE
    assert "<status>dormant</status>" in response.text
    assert "Cloud Shell is dormant" in response.text


def test_dav_dormant_user_propfind_returns_503_xml():
    """WebDAV PROPFIND request for dormant user returns HTTP 503 XML body."""
    response = client.request(
        "PROPFIND",
        "/dav/dormant_user_123/",
        headers={"Depth": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 503
    assert "application/xml" in response.headers.get("content-type", "")
    assert response.text == DORMANT_XML_RESPONSE


def test_dav_expired_user_returns_503_xml():
    """Expired user receives HTTP 503 XML response when session has timed out."""
    registry.register(
        user_id="usr_timed_out",
        tunnel_url="https://old-tunnel.trycloudflare.com",
        ttl_sec=1,
    )

    future_time = time.time() + 100.0
    with patch("time.time", return_value=future_time):
        res = client.get("/dav/usr_timed_out/stream.mkv", follow_redirects=False)
        assert res.status_code == 503
        assert res.text == DORMANT_XML_RESPONSE


# ============================================================================
# 6. Stream Mount Forwarding (/api/mount/{user_id})
# ============================================================================

def test_mount_forward_dormant_user():
    """Mounting a stream for a dormant user returns HTTP 503 JSON."""
    payload = {"url": "https://archive.org/download/test/test.mp4"}
    res = client.post("/api/mount/usr_dormant", json=payload)
    assert res.status_code == 503
    data = res.json()
    assert data["status"] == "dormant"
    assert "Cloud Shell is dormant" in data["error"]


@pytest.mark.asyncio
async def test_mount_forward_active_user_success():
    """Active user mount request is forwarded to {tunnel_url}/api/mount with 10s timeout."""
    client.post("/api/register", json={
        "user_id": "usr_mounter",
        "tunnel_url": "https://cloud-tunnel.trycloudflare.com",
    })

    mock_response = httpx.Response(
        status_code=200,
        content=b'{"status": "success", "mount": {"movie_id": "m_12345"}}',
        headers={"content-type": "application/json"},
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response

        res = client.post(
            "/api/mount/usr_mounter",
            json={"url": "https://example.com/movie.mkv", "title": "My Movie"},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "success"

        # Verify httpx was called with target url and payload
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://cloud-tunnel.trycloudflare.com/api/mount"
        assert kwargs["json"]["url"] == "https://example.com/movie.mkv"
        assert kwargs["json"]["title"] == "My Movie"


@pytest.mark.asyncio
async def test_mount_forward_timeout():
    """Timeout forwarding to Cloud Shell returns HTTP 502 Bad Gateway."""
    client.post("/api/register", json={
        "user_id": "usr_timeout",
        "tunnel_url": "https://slow-tunnel.trycloudflare.com",
    })

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.TimeoutException("Upstream timed out")

        res = client.post(
            "/api/mount/usr_timeout",
            json={"url": "https://example.com/movie.mkv"},
        )
        assert res.status_code == 502
        data = res.json()
        assert data["status"] == "forward_timeout"
        assert "timed out" in data["error"]


@pytest.mark.asyncio
async def test_get_user_mounts_forwarding():
    """Verify GET /api/mounts/{user_id} forwards to Cloud Shell."""
    # Dormant user returns empty mounts
    res_dormant = client.get("/api/mounts/usr_dormant_list")
    assert res_dormant.status_code == 200
    assert res_dormant.json()["status"] == "dormant"
    assert res_dormant.json()["mounts"] == []

    # Active user
    client.post("/api/register", json={
        "user_id": "usr_active_lister",
        "tunnel_url": "https://active-tunnel.trycloudflare.com",
    })

    mock_resp = httpx.Response(
        status_code=200,
        content=b'{"status": "success", "mounts": [{"filename": "Stream1.mkv"}]}',
        headers={"content-type": "application/json"}
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        res = client.get("/api/mounts/usr_active_lister")
        assert res.status_code == 200
        assert len(res.json()["mounts"]) == 1
        assert res.json()["mounts"][0]["filename"] == "Stream1.mkv"


@pytest.mark.asyncio
async def test_unmount_forwarding():
    """Verify DELETE /api/mounts/{user_id}/{filename} and unmount-all forward to Cloud Shell."""
    client.post("/api/register", json={
        "user_id": "usr_unmounter",
        "tunnel_url": "https://unmount-tunnel.trycloudflare.com",
    })

    mock_del_resp = httpx.Response(
        status_code=200,
        content=b'{"status": "success", "message": "Unmounted Video.mkv"}',
        headers={"content-type": "application/json"}
    )
    with patch("httpx.AsyncClient.delete", new_callable=AsyncMock) as mock_del:
        mock_del.return_value = mock_del_resp
        res = client.delete("/api/mounts/usr_unmounter/Video.mkv")
        assert res.status_code == 200
        assert res.json()["status"] == "success"

    # Test unmount-all
    mock_all_resp = httpx.Response(
        status_code=200,
        content=b'{"status": "success", "cleared_count": 5}',
        headers={"content-type": "application/json"}
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_all_resp
        res_all = client.post("/api/unmount-all/usr_unmounter")
        assert res_all.status_code == 200
        assert res_all.json()["cleared_count"] == 5


@pytest.mark.asyncio
async def test_mount_forward_network_error():
    """Connection error forwarding to Cloud Shell returns HTTP 502 Bad Gateway."""
    client.post("/api/register", json={
        "user_id": "usr_conn_err",
        "tunnel_url": "https://broken-tunnel.trycloudflare.com",
    })

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectError("Connection refused")

        res = client.post(
            "/api/mount/usr_conn_err",
            json={"url": "https://example.com/movie.mkv"},
        )
        assert res.status_code == 502
        data = res.json()
        assert data["status"] == "forward_failed"


# ============================================================================
# 7. Thread-Safety & Concurrency
# ============================================================================

def test_thread_safety_concurrent_access():
    """Verify InMemoryUserRegistry is thread-safe under concurrent operations."""
    reg = InMemoryUserRegistry(default_ttl_sec=3600)

    def worker(idx: int):
        user_id = f"user_{idx % 10}"
        tunnel = f"https://tunnel-{idx}.trycloudflare.com"
        reg.register(user_id=user_id, tunnel_url=tunnel)
        reg.is_active(user_id)
        reg.get_status(user_id)
        reg.touch(user_id)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker, i) for i in range(100)]
        for f in futures:
            f.result()

    assert reg.get_active_count() == 10
    assert reg.get_total_count() == 10
