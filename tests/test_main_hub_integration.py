"""
Comprehensive Test Suite for Multi-User Central Pointer Hub routes integrated into main.py
Validates /api/register, /api/heartbeat/{user_id}, /api/status/{user_id},
/api/mount/{user_id}, /api/mounts/{user_id}, /api/unmount/{user_id}/{filename},
/api/unmount-all/{user_id}, /api/hub/users, /api/hub/status,
/dav/{user_id}, /dav/{user_id}/, /dav/{user_id}/{path:path},
and standalone /health, /api/mount, and WebDAV routes on main:app.
"""

import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
import httpx

from main import app
from central_hub import registry
from webdav_engine import mount_manager

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure pristine registry and mount_manager before and after each test."""
    registry.clear()
    mount_manager.clear_all()
    yield
    registry.clear()
    mount_manager.clear_all()


def test_main_health_endpoint():
    """Verify /health endpoint returns online status, mounted_count, and active_users."""
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "online"
    assert data["service"] == "cloud-stream-bridge"
    assert "active_users" in data
    assert "total_registered" in data
    assert data["mounted_count"] == 0


def test_main_api_register():
    """Verify /api/register works seamlessly on main:app."""
    payload = {
        "user_id": "device_user_99",
        "tunnel_url": "https://device-99.trycloudflare.com",
        "ttl_sec": 3600,
    }
    res = client.post("/api/register", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "registered"
    assert data["user_id"] == "device_user_99"
    assert data["tunnel_url"] == "https://device-99.trycloudflare.com"
    assert data["active"] is True

    # Check status endpoint with active probe returning 200
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(200, json={"status": "online"})
        res_status = client.get("/api/status/device_user_99")
        assert res_status.status_code == 200
        assert res_status.json()["active"] is True


def test_main_api_heartbeat():
    """Verify /api/heartbeat/{user_id} handles GET and POST."""
    client.post("/api/register", json={
        "user_id": "hb_user_1",
        "tunnel_url": "https://hb-1.trycloudflare.com",
    })

    # GET heartbeat
    res_get = client.get("/api/heartbeat/hb_user_1")
    assert res_get.status_code == 200
    assert res_get.json()["status"] == "alive"
    assert res_get.json()["active"] is True

    # POST heartbeat
    res_post = client.post("/api/heartbeat/hb_user_1")
    assert res_post.status_code == 200
    assert res_post.json()["status"] == "alive"

    # Dormant heartbeat
    res_dormant = client.get("/api/heartbeat/dormant_hb_user")
    assert res_dormant.status_code == 200
    assert res_dormant.json()["status"] == "dormant"
    assert res_dormant.json()["active"] is False


def test_main_api_hub_endpoints():
    """Verify /api/hub/users and /api/hub/status on main:app."""
    client.post("/api/register", json={
        "user_id": "hub_usr_a",
        "tunnel_url": "https://hub-a.trycloudflare.com",
    })

    # /api/hub/status
    res_hub_status = client.get("/api/hub/status")
    assert res_hub_status.status_code == 200
    data_st = res_hub_status.json()
    assert data_st["status"] == "healthy"
    assert data_st["active_users"] == 1

    # /api/hub/users
    res_hub_users = client.get("/api/hub/users")
    assert res_hub_users.status_code == 200
    data_u = res_hub_users.json()
    assert data_u["status"] == "success"
    assert "hub_usr_a" in data_u["users"]
    assert data_u["users"]["hub_usr_a"]["active"] is True


@pytest.mark.asyncio
async def test_main_api_mount_user_forwarding():
    """Verify /api/mount/{user_id} forwards to user's Cloud Shell tunnel."""
    client.post("/api/register", json={
        "user_id": "mounter_usr",
        "tunnel_url": "https://remote-shell.trycloudflare.com",
    })

    mock_resp = httpx.Response(
        status_code=200,
        content=b'{"status": "success", "mount": {"id": "m_1"}}',
        headers={"content-type": "application/json"}
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = client.post(
            "/api/mount/mounter_usr",
            json={"url": "https://cdn.example.com/video.mkv", "title": "Test Title"}
        )
        assert res.status_code == 200
        assert res.json()["status"] == "success"
        mock_post.assert_called_once()
        assert mock_post.call_args.args[0] == "https://remote-shell.trycloudflare.com/api/mount"


@pytest.mark.asyncio
async def test_main_api_unmount_forwarding():
    """Verify /api/unmount/{user_id}/{filename} (DELETE/POST) and unmount-all forward properly."""
    client.post("/api/register", json={
        "user_id": "unmounter_usr",
        "tunnel_url": "https://remote-shell.trycloudflare.com",
    })

    mock_resp = httpx.Response(
        status_code=200,
        content=b'{"status": "success", "message": "unmounted"}',
        headers={"content-type": "application/json"}
    )
    with patch("httpx.AsyncClient.delete", new_callable=AsyncMock) as mock_del:
        mock_del.return_value = mock_resp
        # DELETE /api/unmount/{user_id}/{filename}
        res_del = client.delete("/api/unmount/unmounter_usr/test_movie.mkv")
        assert res_del.status_code == 200

    with patch("httpx.AsyncClient.delete", new_callable=AsyncMock) as mock_del:
        mock_del.return_value = mock_resp
        # POST /api/unmount/{user_id}/{filename}
        res_post = client.post("/api/unmount/unmounter_usr/test_movie.mkv")
        assert res_post.status_code == 200

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        # POST /api/unmount-all/{user_id}
        res_all_post = client.post("/api/unmount-all/unmounter_usr")
        assert res_all_post.status_code == 200
        # DELETE /api/unmount-all/{user_id}
        res_all_del = client.delete("/api/unmount-all/unmounter_usr")
        assert res_all_del.status_code == 200


def test_main_dav_user_redirect():
    """Verify /dav/{user_id}/ and /dav/{user_id}/{path} redirect to tunnel with 302."""
    client.post("/api/register", json={
        "user_id": "dav_usr_1",
        "tunnel_url": "https://dav-tunnel.trycloudflare.com",
    })

    # 1. /dav/{user_id}/
    res_root = client.get("/dav/dav_usr_1/", follow_redirects=False)
    assert res_root.status_code == 302
    assert res_root.headers["Location"] == "https://dav-tunnel.trycloudflare.com/dav/"

    # 2. /dav/{user_id} (no slash)
    res_no_slash = client.get("/dav/dav_usr_1", follow_redirects=False)
    assert res_no_slash.status_code == 302
    assert res_no_slash.headers["Location"] == "https://dav-tunnel.trycloudflare.com/dav/"

    # 3. /dav/{user_id}/{path:path}
    res_file = client.get("/dav/dav_usr_1/movies/sample.mkv", follow_redirects=False)
    assert res_file.status_code == 302
    assert res_file.headers["Location"] == "https://dav-tunnel.trycloudflare.com/dav/movies/sample.mkv"
    assert res_file.headers["DAV"] == "1"


def test_main_dav_dormant_user():
    """Verify /dav/{user_id}/ returns 503 XML when user is dormant."""
    res = client.get("/dav/dormant_device_xyz/", follow_redirects=False)
    assert res.status_code == 503
    assert "<status>dormant</status>" in res.text


def test_main_standalone_and_hub_coexistence():
    """Verify standalone local mount and multi-user hub routes coexist cleanly."""
    # 1. Standalone local mount
    mount_manager.add_mount(
        movie_id="local_m_1",
        filename="LocalVideo.mkv",
        upstream_url="https://example.com/local.mkv",
        total_bytes=1024,
        content_type="video/x-matroska",
        formatted_size="1 KB"
    )

    # Standalone PROPFIND /dav/LocalVideo.mkv
    res_prop = client.request("PROPFIND", "/dav/LocalVideo.mkv")
    assert res_prop.status_code == 207
    assert "LocalVideo.mkv" in res_prop.text

    # Standalone HEAD /dav/LocalVideo.mkv
    res_head = client.head("/dav/LocalVideo.mkv")
    assert res_head.status_code == 200

    # 2. Register hub user
    client.post("/api/register", json={
        "user_id": "hub_client_42",
        "tunnel_url": "https://tunnel-42.trycloudflare.com",
    })

    # Hub WebDAV GET /dav/hub_client_42/remote.mkv
    res_hub = client.get("/dav/hub_client_42/remote.mkv", follow_redirects=False)
    assert res_hub.status_code == 302
    assert res_hub.headers["Location"] == "https://tunnel-42.trycloudflare.com/dav/remote.mkv"


def test_main_standalone_api_mount():
    """Verify standalone POST /api/mount mounts stream and returns valid response."""
    with patch("main.probe_stream", new_callable=AsyncMock) as mock_probe:
        mock_probe.return_value = {
            "valid": True,
            "status_code": 200,
            "range_supported": True,
            "container_format": "Matroska / WebM",
            "content_type": "video/x-matroska",
            "total_bytes": 1000000000,
            "formatted_size": "953.67 MB",
            "default_filename": "Gladiator.2000.mkv",
            "final_url": "https://example.com/gladiator.mkv",
            "elapsed_ms": 42.0,
            "error": None,
        }

        res = client.post(
            "/api/mount",
            json={"url": "https://example.com/gladiator.mkv", "title": "Gladiator"}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert "stream_endpoints" in data
        assert data["mount"]["filename"] == "Gladiator.2000.mkv"
        assert mount_manager.get_mount("Gladiator.2000.mkv") is not None
