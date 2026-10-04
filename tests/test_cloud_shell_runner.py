"""
Tests for CloudStream Google Cloud Shell Persistent Runner and Multi-User Routing.
Verifies CLI argument parsing, Central Pointer Hub registration, banner formatting,
persistent storage behavior, and live status querying.
"""

import os
import sys
import json
import pytest
from unittest.mock import patch, MagicMock
import urllib.error
import io
import httpx

# Ensure workspace root is in sys.path
WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

import cloud_shell_runner as csr


# ==============================================================================
# 1. CLI Argument Parsing Tests
# ==============================================================================

def test_parse_args_defaults(monkeypatch):
    """Test argument parsing defaults when no CLI flags or env vars are set."""
    monkeypatch.delenv("CLOUDSTREAM_USER_ID", raising=False)
    monkeypatch.delenv("USER_ID", raising=False)
    monkeypatch.delenv("CLOUDSTREAM_HUB_URL", raising=False)
    monkeypatch.delenv("HUB_URL", raising=False)
    monkeypatch.delenv("PORT", raising=False)

    args = csr.parse_args([])
    assert args.user == "default"
    assert args.hub == "https://cloudstream-hub.onrender.com"
    assert args.port == 7860


def test_parse_args_custom_flags():
    """Test argument parsing with explicit CLI flags."""
    custom_argv = [
        "--user", "usr_vip_777",
        "--hub", "https://custom-hub.example.org",
        "--port", "8888"
    ]
    args = csr.parse_args(custom_argv)
    assert args.user == "usr_vip_777"
    assert args.hub == "https://custom-hub.example.org"
    assert args.port == 8888


def test_parse_args_env_fallbacks(monkeypatch):
    """Test environment variable fallbacks when CLI flags are omitted."""
    monkeypatch.setenv("CLOUDSTREAM_USER_ID", "env_user_abc")
    monkeypatch.setenv("CLOUDSTREAM_HUB_URL", "https://env-hub.example.com")
    monkeypatch.setenv("PORT", "9100")

    args = csr.parse_args([])
    assert args.user == "env_user_abc"
    assert args.hub == "https://env-hub.example.com"
    assert args.port == 9100


def test_parse_args_secondary_env_fallbacks(monkeypatch):
    """Test secondary environment variable fallbacks (USER_ID and HUB_URL)."""
    monkeypatch.delenv("CLOUDSTREAM_USER_ID", raising=False)
    monkeypatch.delenv("CLOUDSTREAM_HUB_URL", raising=False)
    monkeypatch.setenv("USER_ID", "fallback_user")
    monkeypatch.setenv("HUB_URL", "https://secondary-hub.example.com")

    args = csr.parse_args([])
    assert args.user == "fallback_user"
    assert args.hub == "https://secondary-hub.example.com"


# ==============================================================================
# 2. Hub Registration Tests
# ==============================================================================

def test_register_with_hub_success():
    """Test successful hub registration with verified payload and headers."""
    hub_url = "https://cloudstream-hub.onrender.com"
    user_id = "usr_test_101"
    tunnel_url = "https://alpha-bravo-123.trycloudflare.com"

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        result = csr.register_with_hub(hub_url, user_id, tunnel_url, verbose=True)

        assert result is True
        assert mock_urlopen.call_count == 1

        # Inspect the Request object passed to urlopen
        called_req = mock_urlopen.call_args[0][0]
        assert called_req.full_url == f"{hub_url}/api/register"
        assert called_req.get_method() == "POST"
        assert called_req.headers.get("Content-type") == "application/json"

        # Verify decoded JSON payload
        sent_data = json.loads(called_req.data.decode("utf-8"))
        assert sent_data == {
            "user_id": user_id,
            "tunnel_url": tunnel_url
        }


def test_register_with_hub_trailing_slash_normalization():
    """Verify hub URL trailing slash is stripped before appending /api/register."""
    hub_url = "https://cloudstream-hub.onrender.com/"
    user_id = "usr_norm"
    tunnel_url = "https://my-tunnel.trycloudflare.com"

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        result = csr.register_with_hub(hub_url, user_id, tunnel_url, verbose=False)
        assert result is True

        called_req = mock_urlopen.call_args[0][0]
        assert called_req.full_url == "https://cloudstream-hub.onrender.com/api/register"


def test_register_with_hub_http_error():
    """Verify HTTP error (e.g. 500) returns False gracefully without crash."""
    error = urllib.error.HTTPError(
        url="https://cloudstream-hub.onrender.com/api/register",
        code=500,
        msg="Internal Server Error",
        hdrs={},
        fp=io.BytesIO(b'{"error": "database down"}')
    )

    with patch("urllib.request.urlopen", side_effect=error):
        result = csr.register_with_hub(
            "https://cloudstream-hub.onrender.com",
            "usr_err",
            "https://tunnel.trycloudflare.com",
            verbose=True
        )
        assert result is False


def test_register_with_hub_network_error():
    """Verify URLError / connection failure returns False gracefully."""
    error = urllib.error.URLError("Connection refused [Errno 111]")

    with patch("urllib.request.urlopen", side_effect=error):
        result = csr.register_with_hub(
            "https://cloudstream-hub.onrender.com",
            "usr_err",
            "https://tunnel.trycloudflare.com",
            verbose=False
        )
        assert result is False


def test_register_with_hub_missing_fields():
    """Verify that missing parameters return False immediately."""
    assert csr.register_with_hub("", "usr_test", "https://t.com") is False
    assert csr.register_with_hub("https://hub.com", "", "https://t.com") is False
    assert csr.register_with_hub("https://hub.com", "usr_test", "") is False


def test_register_payload_httpx_schema_validation():
    """Verify registration payload compatibility using httpx Request."""
    user_id = "usr_httpx_check"
    tunnel_url = "https://check.trycloudflare.com"
    hub_url = "https://cloudstream-hub.onrender.com"

    # Construct request via httpx
    req = httpx.Request(
        "POST",
        f"{hub_url}/api/register",
        json={"user_id": user_id, "tunnel_url": tunnel_url}
    )
    payload = json.loads(req.content.decode("utf-8"))
    assert payload["user_id"] == user_id
    assert payload["tunnel_url"] == tunnel_url


# ==============================================================================
# 3. ANSI Box & Banner Formatting Tests
# ==============================================================================

def test_banner_formatting(capsys):
    """Verify print_banner includes user ID, Permanent WebDAV, and Direct Tunnel."""
    user_id = "usr_banner_123"
    hub_url = "https://cloudstream-hub.onrender.com"
    tunnel_url = "https://stream-xyz.trycloudflare.com"
    mount_count = 8

    csr.print_banner(tunnel_url, mount_count, user_id=user_id, hub_url=hub_url)

    captured = capsys.readouterr().out
    clean_out = csr._strip_ansi(captured)

    # Required endpoints
    assert f"Permanent WebDAV: https://cloudstream-hub.onrender.com/dav/{user_id}/" in clean_out
    assert f"Direct Tunnel:    https://stream-xyz.trycloudflare.com/dav/" in clean_out
    assert f"User ID:          {user_id}" in clean_out
    assert "8 mounted streams loaded from disk" in clean_out
    assert "cloudstream-hub.onrender.com" in clean_out
    assert f"/dav/{user_id}/" in clean_out


def test_strip_ansi_and_box_row():
    """Test helper functions for clean ANSI text stripping and border alignment."""
    ansi_text = "\033[1;32mTest Message\033[0m"
    plain = csr._strip_ansi(ansi_text)
    assert plain == "Test Message"

    row = csr._box_row("Hello World", border_len=76)
    plain_row = csr._strip_ansi(row)
    # Total width with borders: 76 + 2 = 78 chars
    assert len(plain_row) == 78
    assert plain_row.startswith("║ ")
    assert plain_row.endswith(" ║") or plain_row.endswith("║")


# ==============================================================================
# 4. Persistent Storage Tests
# ==============================================================================

def test_setup_persistent_storage(tmp_path, monkeypatch):
    """Test directory creation and mounts.json initialization."""
    test_cs_dir = tmp_path / ".cloudstream-bridge"
    test_bin_dir = test_cs_dir / "bin"
    test_mounts = test_cs_dir / "mounts.json"

    monkeypatch.setattr(csr, "CS_DIR", str(test_cs_dir))
    monkeypatch.setattr(csr, "BIN_DIR", str(test_bin_dir))
    monkeypatch.setattr(csr, "MOUNTS_DB_PATH", str(test_mounts))

    count = csr.setup_persistent_storage()

    assert test_cs_dir.exists()
    assert test_bin_dir.exists()
    assert test_mounts.exists()
    assert count >= 0
    assert os.environ.get("MOUNTS_DB_PATH") == str(test_mounts)

    # Seed with custom sample data and verify mount count detection
    sample_data = {
        "mkv_sample_1": {"filename": "Sample1.mkv", "size_bytes": 1000},
        "mkv_sample_2": {"filename": "Sample2.mkv", "size_bytes": 2000},
        "mkv_sample_3": {"filename": "Sample3.mkv", "size_bytes": 3000}
    }
    with open(test_mounts, "w", encoding="utf-8") as f:
        json.dump(sample_data, f)

    count_after = csr.setup_persistent_storage()
    assert count_after == 3


# ==============================================================================
# 5. Live Telemetry & Status Tests
# ==============================================================================

def test_get_live_status_success():
    """Test get_live_status retrieves telemetry from local FastAPI server."""
    mock_data = {
        "active_streams": 3,
        "total_virtual_library_gb": 45.2,
        "streamed_mb": 512.0,
        "streamed_gb": 0.5,
        "mounted_count": 10
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(mock_data).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        status = csr.get_live_status(port=7860)
        assert status == mock_data
        assert mock_urlopen.call_args[0][0].full_url == "http://127.0.0.1:7860/api/status"


def test_get_live_status_offline():
    """Test get_live_status returns None when server is unreachable."""
    with patch("urllib.request.urlopen", side_effect=Exception("Server not running")):
        status = csr.get_live_status(port=7860)
        assert status is None
