"""
End-to-End Integration Test Suite for CloudStream Multi-User Suite
Validates:
1. Multi-user registration, independent tunnel routing, and room isolation.
2. Dormant user lifecycle (503 XML error -> Cloud Shell boot -> 302 redirect).
3. 50-second anti-idle heartbeat telemetry and remote stream mount forwarding.
4. CX File Explorer WebDAV compliance headers (CORS, Location, DAV: 1).
"""

import os
import sys
import time
import xml.etree.ElementTree as ET
import pytest
from unittest.mock import patch, AsyncMock
from concurrent.futures import ThreadPoolExecutor

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
import httpx

from central_hub import app, registry, DORMANT_XML_RESPONSE

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure a pristine registry before and after each test."""
    registry.clear()
    yield
    registry.clear()


@pytest.fixture(autouse=True)
def mock_tunnel_probe():
    """Mock active tunnel reachability probe for dummy integration test tunnels."""
    mock_resp = httpx.Response(200, json={"status": "online", "streamed_gb": 0.0})
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        yield mock_get


# ==============================================================================
# 1. Multi-User Registration & Strict Isolation
# ==============================================================================

def test_e2e_multi_user_registration_and_isolation():
    """
    Register User A (usr_alpha) and User B (usr_beta) with different trycloudflare tunnels.
    Verify /api/status/usr_alpha and /api/status/usr_beta.
    Verify WebDAV GET and PROPFIND /dav/usr_alpha/Avatar.mkv redirects to User A's tunnel with 302.
    Verify WebDAV GET and PROPFIND /dav/usr_beta/Gladiator.mkv redirects to User B's tunnel with 302.
    Verify strict multi-user room separation.
    """
    tunnel_alpha = "https://user-alpha-987.trycloudflare.com"
    tunnel_beta = "https://user-beta-654.trycloudflare.com"

    # 1. Register User A (usr_alpha)
    res_reg_a = client.post("/api/register", json={
        "user_id": "usr_alpha",
        "tunnel_url": tunnel_alpha,
        "token": "tok_alpha_sec",
        "ttl_sec": 43200,
    })
    assert res_reg_a.status_code == 200
    data_reg_a = res_reg_a.json()
    assert data_reg_a["status"] == "registered"
    assert data_reg_a["user_id"] == "usr_alpha"
    assert data_reg_a["tunnel_url"] == tunnel_alpha
    assert data_reg_a["active"] is True

    # 2. Register User B (usr_beta)
    res_reg_b = client.post("/api/register", json={
        "user_id": "usr_beta",
        "tunnel_url": tunnel_beta,
        "token": "tok_beta_sec",
        "ttl_sec": 43200,
    })
    assert res_reg_b.status_code == 200
    data_reg_b = res_reg_b.json()
    assert data_reg_b["status"] == "registered"
    assert data_reg_b["user_id"] == "usr_beta"
    assert data_reg_b["tunnel_url"] == tunnel_beta
    assert data_reg_b["active"] is True

    # 3. Verify status endpoints for both users
    res_status_a = client.get("/api/status/usr_alpha")
    assert res_status_a.status_code == 200
    data_status_a = res_status_a.json()
    assert data_status_a["active"] is True
    assert data_status_a["user_id"] == "usr_alpha"
    assert data_status_a["tunnel_url"] == tunnel_alpha
    assert data_status_a["ttl_remaining_sec"] > 43000

    res_status_b = client.get("/api/status/usr_beta")
    assert res_status_b.status_code == 200
    data_status_b = res_status_b.json()
    assert data_status_b["active"] is True
    assert data_status_b["user_id"] == "usr_beta"
    assert data_status_b["tunnel_url"] == tunnel_beta
    assert data_status_b["ttl_remaining_sec"] > 43000

    # 4. Verify WebDAV GET (302 redirect) and PROPFIND (reverse-proxied) for usr_alpha/Avatar.mkv
    res_get_a = client.get("/dav/usr_alpha/Avatar.mkv", follow_redirects=False)
    assert res_get_a.status_code == 302
    assert res_get_a.headers["Location"] == f"{tunnel_alpha}/dav/Avatar.mkv"

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = httpx.Response(207, content=b"<multistatus/>", headers={"content-type": "application/xml"})
        res_pf_a = client.request("PROPFIND", "/dav/usr_alpha/Avatar.mkv", follow_redirects=False)
        assert res_pf_a.status_code == 207
        assert mock_req.call_args.kwargs["url"] == f"{tunnel_alpha}/dav/Avatar.mkv"

    # 5. Verify WebDAV GET (302 redirect) and PROPFIND (reverse-proxied) for usr_beta/Gladiator.mkv
    res_get_b = client.get("/dav/usr_beta/Gladiator.mkv", follow_redirects=False)
    assert res_get_b.status_code == 302
    assert res_get_b.headers["Location"] == f"{tunnel_beta}/dav/Gladiator.mkv"

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = httpx.Response(207, content=b"<multistatus/>", headers={"content-type": "application/xml"})
        res_pf_b = client.request("PROPFIND", "/dav/usr_beta/Gladiator.mkv", follow_redirects=False)
        assert res_pf_b.status_code == 207
        assert mock_req.call_args.kwargs["url"] == f"{tunnel_beta}/dav/Gladiator.mkv"

    # 6. Verify Strict Multi-User Room Separation
    # usr_alpha cannot leak into User B's tunnel even if accessing Gladiator.mkv
    res_leak_check_a = client.get("/dav/usr_alpha/Gladiator.mkv", follow_redirects=False)
    assert res_leak_check_a.status_code == 302
    assert res_leak_check_a.headers["Location"] == f"{tunnel_alpha}/dav/Gladiator.mkv"
    assert tunnel_beta not in res_leak_check_a.headers["Location"]

    # usr_beta cannot leak into User A's tunnel even if accessing Avatar.mkv
    res_leak_check_b = client.get("/dav/usr_beta/Avatar.mkv", follow_redirects=False)
    assert res_leak_check_b.status_code == 302
    assert res_leak_check_b.headers["Location"] == f"{tunnel_beta}/dav/Avatar.mkv"
    assert tunnel_alpha not in res_leak_check_b.headers["Location"]

    # Root WebDAV paths isolation
    res_root_a = client.get("/dav/usr_alpha/", follow_redirects=False)
    assert res_root_a.status_code == 302
    assert res_root_a.headers["Location"] == f"{tunnel_alpha}/dav/"

    res_root_b = client.get("/dav/usr_beta/", follow_redirects=False)
    assert res_root_b.status_code == 302
    assert res_root_b.headers["Location"] == f"{tunnel_beta}/dav/"

    # Global health reflects 2 distinct active users
    res_health = client.get("/health")
    assert res_health.status_code == 200
    assert res_health.json()["active_users"] == 2
    assert res_health.json()["total_registered"] == 2


# ==============================================================================
# 2. Dormant User Workflow & Transition
# ==============================================================================

def test_e2e_dormant_user_workflow():
    """
    Verify dormant user receives 503 XML.
    Simulate user launching Cloud Shell, sending registration.
    Verify user transition from dormant 503 to active 302 redirect.
    """
    dormant_uid = "usr_dormant_pilot"
    cloud_shell_tunnel = "https://pilot-cloudshell-session.trycloudflare.com"

    # Phase 1: Dormant State (Before Cloud Shell activation)
    # Check status endpoint reports dormant
    status_pre = client.get(f"/api/status/{dormant_uid}")
    assert status_pre.status_code == 200
    assert status_pre.json()["active"] is False
    assert status_pre.json()["tunnel_url"] is None
    assert status_pre.json()["ttl_remaining_sec"] == 0

    # WebDAV GET request returns 503 XML with standard CX File Explorer message
    res_get_dormant = client.get(f"/dav/{dormant_uid}/Interstellar.mkv", follow_redirects=False)
    assert res_get_dormant.status_code == 503
    assert "application/xml" in res_get_dormant.headers.get("content-type", "")
    assert res_get_dormant.text == DORMANT_XML_RESPONSE
    assert "<status>dormant</status>" in res_get_dormant.text
    assert "Cloud Shell is dormant" in res_get_dormant.text

    # WebDAV PROPFIND request returns 503 XML
    res_pf_dormant = client.request("PROPFIND", f"/dav/{dormant_uid}/", headers={"Depth": "1"}, follow_redirects=False)
    assert res_pf_dormant.status_code == 503
    assert "application/xml" in res_pf_dormant.headers.get("content-type", "")
    assert res_pf_dormant.text == DORMANT_XML_RESPONSE

    # Mount attempt during dormant phase returns 503 JSON
    res_mount_dormant = client.post(f"/api/mount/{dormant_uid}", json={
        "url": "https://streams.example.org/remux.mkv"
    })
    assert res_mount_dormant.status_code == 503
    assert res_mount_dormant.json()["status"] == "dormant"

    # Phase 2: User Launches Google Cloud Shell & Registers Tunnel
    res_reg = client.post("/api/register", json={
        "user_id": dormant_uid,
        "tunnel_url": cloud_shell_tunnel,
        "ttl_sec": 43200,
    })
    assert res_reg.status_code == 200
    assert res_reg.json()["active"] is True
    assert res_reg.json()["tunnel_url"] == cloud_shell_tunnel

    # Check status endpoint now reports active
    status_post = client.get(f"/api/status/{dormant_uid}")
    assert status_post.status_code == 200
    assert status_post.json()["active"] is True
    assert status_post.json()["tunnel_url"] == cloud_shell_tunnel
    assert status_post.json()["ttl_remaining_sec"] > 40000

    # Phase 3: Transition Verified - Immediate 302 Redirection for GET, Reverse-Proxy for PROPFIND
    res_get_active = client.get(f"/dav/{dormant_uid}/Interstellar.mkv", follow_redirects=False)
    assert res_get_active.status_code == 302
    assert res_get_active.headers["Location"] == f"{cloud_shell_tunnel}/dav/Interstellar.mkv"

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = httpx.Response(207, content=b"<multistatus/>", headers={"content-type": "application/xml"})
        res_pf_active = client.request("PROPFIND", f"/dav/{dormant_uid}/", follow_redirects=False)
        assert res_pf_active.status_code == 207
        assert mock_req.call_args.kwargs["url"] == f"{cloud_shell_tunnel}/dav/"

    # Phase 4: Verification of Re-entry into Dormant State upon TTL Expiration
    future_time = time.time() + 45000.0
    with patch("time.time", return_value=future_time):
        res_expired = client.get(f"/dav/{dormant_uid}/Interstellar.mkv", follow_redirects=False)
        assert res_expired.status_code == 503
        assert res_expired.text == DORMANT_XML_RESPONSE
        assert client.get(f"/api/status/{dormant_uid}").json()["active"] is False


# ==============================================================================
# 3. Cloud Shell Heartbeat & Remote Stream Mount Forwarding
# ==============================================================================

@pytest.mark.asyncio
async def test_e2e_cloud_shell_heartbeat_and_mount_forwarding():
    """
    Simulate 50s heartbeat refreshing last_seen timestamp.
    Simulate remote stream mount forwarding with mock httpx.
    """
    user_id = "usr_telemetry_runner"
    tunnel_url = "https://cloudshell-node-42.trycloudflare.com"

    # 1. Initial registration
    client.post("/api/register", json={
        "user_id": user_id,
        "tunnel_url": tunnel_url,
        "ttl_sec": 43200,
    })

    entry_t0 = registry.get(user_id)
    initial_last_seen = entry_t0["last_seen"]
    orig_registered_at = entry_t0["registered_at"]

    # 2. Simulate 50s heartbeat pulse from cloud_shell_runner
    time.sleep(0.05)  # small delta to ensure monotonic clock increase
    simulated_t1 = initial_last_seen + 50.0

    with patch("time.time", return_value=simulated_t1):
        # cloud_shell_runner sends heartbeat pulse via register_with_hub / /api/register
        pulse_res = client.post("/api/register", json={
            "user_id": user_id,
            "tunnel_url": tunnel_url,
        })
        assert pulse_res.status_code == 200

        entry_t1 = registry.get(user_id)
        assert entry_t1["last_seen"] == simulated_t1
        assert entry_t1["last_seen"] > initial_last_seen
        # Verify registration timestamp
        assert entry_t1["registered_at"] >= orig_registered_at

        # Verify WebDAV request also touches and refreshes last_seen
        simulated_t2 = simulated_t1 + 25.0
        with patch("time.time", return_value=simulated_t2):
            client.get(f"/dav/{user_id}/sample.mkv", follow_redirects=False)
            entry_t2 = registry.get(user_id)
            assert entry_t2["last_seen"] == simulated_t2

    # 3. Simulate Remote Stream Mount Forwarding with Mock HTTPX
    mount_payload = {
        "url": "https://debrid.example/dl/Oppenheimer.2023.2160p.UHD.Remux.mkv",
        "title": "Oppenheimer (2023) 4K Remux",
        "custom_headers": {
            "User-Agent": "ExoPlayer/2.19.1",
            "Authorization": "Bearer debrid_test_key"
        }
    }

    mock_backend_response = httpx.Response(
        status_code=200,
        content=b'{"status": "success", "mount": {"id": "m_oppenheimer_01", "filename": "Oppenheimer (2023) 4K Remux.mkv", "size": "78.4 GB"}}',
        headers={"content-type": "application/json"},
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_backend_response

        mount_res = client.post(f"/api/mount/{user_id}", json=mount_payload)
        assert mount_res.status_code == 200
        mount_data = mount_res.json()
        assert mount_data["status"] == "success"
        assert mount_data["mount"]["id"] == "m_oppenheimer_01"
        assert mount_data["mount"]["filename"] == "Oppenheimer (2023) 4K Remux.mkv"

        # Assert correct forwarding target and payload
        mock_post.assert_called_once()
        called_url, called_kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        assert called_url == f"{tunnel_url}/api/mount"
        assert called_kwargs["json"]["url"] == mount_payload["url"]
        assert called_kwargs["json"]["title"] == mount_payload["title"]
        assert called_kwargs["json"]["custom_headers"] == mount_payload["custom_headers"]
        assert called_kwargs["headers"]["Content-Type"] == "application/json"

    # 4. Verify upstream timeout resilience (502 Bad Gateway)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post_err:
        mock_post_err.side_effect = httpx.TimeoutException("Cloud Shell bridge timed out")
        res_timeout = client.post(f"/api/mount/{user_id}", json=mount_payload)
        assert res_timeout.status_code == 502
        assert res_timeout.json()["status"] == "forward_timeout"
        assert "timed out" in res_timeout.json()["error"]


# ==============================================================================
# 4. CX File Explorer WebDAV Response Headers & Compliance
# ==============================================================================

def test_e2e_cx_file_explorer_headers():
    """
    Verify all required WebDAV response headers (CORS, Location, DAV compliance).
    Validates standards required by CX File Explorer and VLC on Android TV.
    """
    user_id = "usr_cx_tester"
    tunnel_url = "https://cx-tester-tunnel.trycloudflare.com"

    # Phase 1: Dormant Header Verification
    res_dormant = client.get(f"/dav/{user_id}/movie.mkv", follow_redirects=False)
    assert res_dormant.status_code == 503
    # Verify XML content type
    assert "application/xml" in res_dormant.headers.get("content-type", "").lower()
    # Verify Permissive CORS
    assert res_dormant.headers.get("Access-Control-Allow-Origin") == "*"
    assert res_dormant.headers.get("Access-Control-Allow-Methods") == "*"
    assert res_dormant.headers.get("Access-Control-Allow-Headers") == "*"
    # Verify DAV compliance header
    assert res_dormant.headers.get("DAV") == "1"

    # Phase 2: Active User Header Verification
    client.post("/api/register", json={
        "user_id": user_id,
        "tunnel_url": tunnel_url,
    })

    # Test GET on file path
    res_active_get = client.get(f"/dav/{user_id}/movies/action/Gladiator.2000.mkv", follow_redirects=False)
    assert res_active_get.status_code == 302
    assert res_active_get.headers.get("Location") == f"{tunnel_url}/dav/movies/action/Gladiator.2000.mkv"
    assert res_active_get.headers.get("Access-Control-Allow-Origin") == "*"
    assert res_active_get.headers.get("Access-Control-Allow-Methods") == "*"
    assert res_active_get.headers.get("Access-Control-Allow-Headers") == "*"
    assert res_active_get.headers.get("DAV") == "1"

    # Test PROPFIND on folder path (standard CX File Explorer directory discovery - reverse proxied)
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = httpx.Response(207, content=b"<multistatus/>", headers={"content-type": "application/xml"})
        res_active_pf = client.request("PROPFIND", f"/dav/{user_id}/", headers={"Depth": "1"}, follow_redirects=False)
        assert res_active_pf.status_code == 207
        assert res_active_pf.headers.get("Access-Control-Allow-Origin") == "*"
        assert res_active_pf.headers.get("Access-Control-Allow-Methods") == "*"
        assert res_active_pf.headers.get("Access-Control-Allow-Headers") == "*"
        assert res_active_pf.headers.get("DAV") == "1"

        # Test OPTIONS discovery (RFC 4918 discovery - reverse proxied)
        res_active_opt = client.options(f"/dav/{user_id}/", follow_redirects=False)
        assert res_active_opt.status_code == 207
        assert res_active_opt.headers.get("Access-Control-Allow-Origin") == "*"
        assert res_active_opt.headers.get("Access-Control-Allow-Methods") == "*"
        assert res_active_opt.headers.get("Access-Control-Allow-Headers") == "*"
        assert res_active_opt.headers.get("DAV") == "1"

    # Test Query Parameter Preservation (Token and Seek query strings)
    res_query = client.get(f"/dav/{user_id}/video.mp4?token=stream_token_123&seek=3600", follow_redirects=False)
    assert res_query.status_code == 302
    assert res_query.headers.get("Location") == f"{tunnel_url}/dav/video.mp4?token=stream_token_123&seek=3600"
    assert res_query.headers.get("DAV") == "1"


# ==============================================================================
# 5. Full End-to-End Multi-User Concurrency & Runner Integration
# ==============================================================================

def test_e2e_runner_registration_and_status_roundtrip():
    """
    Simulate runner registration directly interacting with Central Pointer Hub,
    verifying end-to-end telemetry sync and status querying.
    """
    test_user = "usr_roundtrip_tester"
    test_tunnel = "https://roundtrip-tunnel.trycloudflare.com"

    reg_res = client.post("/api/register", json={
        "user_id": test_user,
        "tunnel_url": test_tunnel,
        "ttl_sec": 43200,
    })
    assert reg_res.status_code == 200
    assert reg_res.json()["status"] == "registered"

    # Check status endpoint confirms registration
    status = client.get(f"/api/status/{test_user}").json()
    assert status["active"] is True
    assert status["tunnel_url"] == test_tunnel

    # Check WebDAV redirect matches
    dav_res = client.get(f"/dav/{test_user}/test_stream.mkv", follow_redirects=False)
    assert dav_res.status_code == 302
    assert dav_res.headers["Location"] == f"{test_tunnel}/dav/test_stream.mkv"


def test_e2e_concurrent_multi_user_traffic():
    """
    Ensure 20 concurrent users registering and querying routes simultaneously
    maintain complete isolation and thread safety.
    """
    user_count = 20

    def user_lifecycle(idx: int):
        uid = f"usr_stress_{idx:03d}"
        tunnel = f"https://tunnel-{idx:03d}.trycloudflare.com"

        # 1. Check dormant
        r1 = client.get(f"/dav/{uid}/stream.mkv", follow_redirects=False)
        assert r1.status_code == 503

        # 2. Register
        r2 = client.post("/api/register", json={"user_id": uid, "tunnel_url": tunnel})
        assert r2.status_code == 200

        # 3. Status check
        r3 = client.get(f"/api/status/{uid}")
        assert r3.status_code == 200
        assert r3.json()["active"] is True
        assert r3.json()["tunnel_url"] == tunnel

        # 4. WebDAV redirect check
        r4 = client.get(f"/dav/{uid}/stream.mkv", follow_redirects=False)
        assert r4.status_code == 302
        assert r4.headers["Location"] == f"{tunnel}/dav/stream.mkv"

        # 5. PROPFIND reverse-proxy check
        r5 = client.request("PROPFIND", f"/dav/{uid}/", follow_redirects=False)
        assert r5.status_code == 207
        assert r5.headers["DAV"] == "1"

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = httpx.Response(207, content=b"<multistatus/>", headers={"content-type": "application/xml"})
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(user_lifecycle, i) for i in range(user_count)]
            for f in futures:
                f.result()

    # Verify overall hub telemetry
    health = client.get("/health").json()
    assert health["active_users"] == user_count
    assert health["total_registered"] == user_count
