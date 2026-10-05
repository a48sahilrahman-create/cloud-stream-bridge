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
    assert args.hub == "https://cloud-stream-bridge.onrender.com"
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


def test_is_valid_tunnel_url():
    """Verify validation logic for public Cloudflare tunnel URLs."""
    # Valid trycloudflare URLs
    assert csr.is_valid_tunnel_url("https://alpha-123.trycloudflare.com") is True
    assert csr.is_valid_tunnel_url("https://my-subdomain.trycloudflare.com/") is True
    assert csr.is_valid_tunnel_url("http://tunnel.trycloudflare.com:8080") is True

    # Localhost and loopback must be strictly rejected
    assert csr.is_valid_tunnel_url("http://localhost:7860") is False
    assert csr.is_valid_tunnel_url("http://localhost") is False
    assert csr.is_valid_tunnel_url("http://127.0.0.1:7860") is False
    assert csr.is_valid_tunnel_url("http://127.0.0.1") is False

    # Arbitrary non-trycloudflare domains must be rejected
    assert csr.is_valid_tunnel_url("https://example.com") is False
    assert csr.is_valid_tunnel_url("http://192.168.1.50:7860") is False
    assert csr.is_valid_tunnel_url("https://myhub.onrender.com") is False

    # Empty, None, or invalid types
    assert csr.is_valid_tunnel_url("") is False
    assert csr.is_valid_tunnel_url(None) is False
    assert csr.is_valid_tunnel_url(12345) is False


def test_register_with_hub_rejects_localhost(capsys):
    """Verify register_with_hub rejects localhost and does NOT make HTTP requests."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = csr.register_with_hub(
            "https://cloudstream-hub.onrender.com",
            "usr_local",
            "http://localhost:7860",
            verbose=True
        )
        assert result is False
        mock_urlopen.assert_not_called()

    captured = capsys.readouterr().out
    assert "Error" in captured
    assert "tunnel failed to establish" in captured.lower()


def test_register_with_hub_rejects_127_0_0_1(capsys):
    """Verify register_with_hub rejects 127.0.0.1 and does NOT make HTTP requests."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = csr.register_with_hub(
            "https://cloudstream-hub.onrender.com",
            "usr_local",
            "http://127.0.0.1:7860",
            verbose=True
        )
        assert result is False
        mock_urlopen.assert_not_called()

    captured = capsys.readouterr().out
    assert "Error" in captured
    assert "tunnel failed to establish" in captured.lower()


def test_register_with_hub_rejects_non_cloudflare_fallback(capsys):
    """Verify register_with_hub rejects URLs without trycloudflare.com."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = csr.register_with_hub(
            "https://cloudstream-hub.onrender.com",
            "usr_local",
            "https://arbitrary-fallback.com",
            verbose=True
        )
        assert result is False
        mock_urlopen.assert_not_called()

    captured = capsys.readouterr().out
    assert "Error" in captured
    assert "tunnel failed to establish" in captured.lower()


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


# ==============================================================================
# 6. Tunnel URL Extraction & ANSI Stripping Tests
# ==============================================================================

def test_extract_tunnel_url_with_ansi_sequences(tmp_path, monkeypatch, capsys):
    """Verify that extract_tunnel_url strips ANSI escape sequences before regex match."""
    test_log = tmp_path / "tunnel.log"
    # Write cloudflared output line embedded with ANSI escape sequences
    test_log.write_text(
        "\x1b[32m2026-10-04T12:00:00Z\x1b[0m \x1b[1;36mINF\x1b[0m |  "
        "Your quick Tunnel has been created: \x1b[4;34mhttps://alpha-bravo-123.trycloudflare.com\x1b[0m\n",
        encoding="utf-8"
    )

    monkeypatch.setattr(csr, "TUNNEL_LOG", str(test_log))
    monkeypatch.setattr(csr, "tunnel_proc", None)

    url = csr.extract_tunnel_url(timeout_secs=2)
    assert url == "https://alpha-bravo-123.trycloudflare.com"

    out = capsys.readouterr().out
    assert "Successfully extracted Cloudflare tunnel URL" in out


def test_extract_tunnel_url_cloudflared_fails(tmp_path, monkeypatch, capsys):
    """Verify robust error logging and premature exit when cloudflared process fails."""
    test_log = tmp_path / "tunnel.log"
    test_log.write_text("\x1b[31mError: cloudflared crashed with code 1\x1b[0m\n", encoding="utf-8")

    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1  # Process terminated with exit code 1

    monkeypatch.setattr(csr, "TUNNEL_LOG", str(test_log))
    monkeypatch.setattr(csr, "tunnel_proc", mock_proc)

    url = csr.extract_tunnel_url(timeout_secs=5)
    assert url == ""

    out = capsys.readouterr().out
    assert "Cloudflared failed: process exited prematurely with exit code 1" in out
    assert "cloudflared output:" in out


def test_extract_tunnel_url_timeout(tmp_path, monkeypatch, capsys):
    """Verify timeout handling and warning logging when tunnel URL cannot be extracted."""
    test_log = tmp_path / "tunnel.log"
    test_log.write_text("Waiting for tunnel connection...\n", encoding="utf-8")

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # Process still running

    monkeypatch.setattr(csr, "TUNNEL_LOG", str(test_log))
    monkeypatch.setattr(csr, "tunnel_proc", mock_proc)

    url = csr.extract_tunnel_url(timeout_secs=1)
    assert url == ""

    out = capsys.readouterr().out
    assert "Failed to extract trycloudflare URL within 1s timeout" in out


def test_register_with_hub_timeout_error(capsys):
    """Verify that timeout errors (e.g. Render cold boot) are caught and logged clearly."""
    timeout_err = urllib.error.URLError("timed out")

    with patch("urllib.request.urlopen", side_effect=timeout_err) as mock_urlopen:
        result = csr.register_with_hub(
            "https://cloudstream-hub.onrender.com",
            "usr_timeout",
            "https://test.trycloudflare.com",
            timeout=15.0,
            verbose=True
        )
        assert result is False
        assert mock_urlopen.call_count == 1
        assert mock_urlopen.call_args[1]["timeout"] == 15.0

    out = capsys.readouterr().out
    assert "Hub registration timed out after 15.0s (Render cold boot may be in progress)" in out


def test_register_with_hub_http_error_logging(capsys):
    """Verify that HTTP errors log detailed status code and error messages."""
    error = urllib.error.HTTPError(
        url="https://cloudstream-hub.onrender.com/api/register",
        code=502,
        msg="Bad Gateway",
        hdrs={},
        fp=io.BytesIO(b'{"detail": "upstream service unavailable"}')
    )

    with patch("urllib.request.urlopen", side_effect=error):
        result = csr.register_with_hub(
            "https://cloudstream-hub.onrender.com",
            "usr_err",
            "https://test.trycloudflare.com",
            verbose=False,
            log_errors=True
        )
        assert result is False

    out = capsys.readouterr().out
    assert "Hub registration HTTP error 502" in out
    assert "upstream service unavailable" in out


def test_extract_tunnel_url_immediate_exit_on_crash(monkeypatch):
    """Verify extract_tunnel_url exits immediately if cloudflared crashes, without waiting 45s."""
    import time
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1  # Process crashed on start

    monkeypatch.setattr(csr, "tunnel_proc", mock_proc)

    start = time.time()
    url = csr.extract_tunnel_url(timeout_secs=45)
    duration = time.time() - start

    assert url == ""
    assert duration < 2.0  # Must terminate immediately without waiting 45s


def test_run_heartbeat_loop_breaks_on_tunnel_exit(monkeypatch):
    """Verify heartbeat loop breaks immediately and does not ping hub when tunnel process exits."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1  # Tunnel process exited
    monkeypatch.setattr(csr, "tunnel_proc", mock_proc)

    with patch("cloud_shell_runner.register_with_hub") as mock_reg, \
         patch("cloud_shell_runner.get_live_status") as mock_status:
        csr.run_heartbeat_loop(
            hub_url="https://cloudstream-hub.onrender.com",
            user_id="test_user",
            tunnel_url="https://dead-tunnel.trycloudflare.com"
        )
        # Should break immediately without querying status or pinging hub
        assert mock_reg.call_count == 0
        assert mock_status.call_count == 0


def test_run_heartbeat_loop_detects_exit_during_interval(monkeypatch):
    """Verify heartbeat loop detects tunnel exit during 50s interval and stops loop."""
    mock_proc = MagicMock()
    # Pulse 1: process running (None), interval wait: process died (1)
    mock_proc.poll.side_effect = [None, 1, 1, 1]
    monkeypatch.setattr(csr, "tunnel_proc", mock_proc)

    with patch("cloud_shell_runner.register_with_hub", return_value=True) as mock_reg, \
         patch("cloud_shell_runner.get_live_status", return_value={"mounted_count": 1}), \
         patch("cloud_shell_runner.is_valid_tunnel_url", return_value=True):
        csr.run_heartbeat_loop(
            hub_url="https://cloudstream-hub.onrender.com",
            user_id="test_user",
            tunnel_url="https://active.trycloudflare.com"
        )
        assert mock_reg.call_count == 1


# ==============================================================================
# 7. Cloudflare Named Tunnel & Health Probing Tests
# ==============================================================================

def test_parse_args_named_tunnel_flags():
    """Verify CLI parsing for --tunnel-token and --tunnel-hostname."""
    argv = [
        "--user", "usr_named_1",
        "--tunnel-token", "eyJhIjoiZGF2LXRva2VuIn0=",
        "--tunnel-hostname", "dav.mydomain.com"
    ]
    args = csr.parse_args(argv)
    assert args.user == "usr_named_1"
    assert args.tunnel_token == "eyJhIjoiZGF2LXRva2VuIn0="
    assert args.tunnel_hostname == "dav.mydomain.com"


def test_parse_args_named_tunnel_env_fallbacks(monkeypatch):
    """Verify env variable fallbacks for TUNNEL_TOKEN and TUNNEL_HOSTNAME."""
    monkeypatch.setenv("TUNNEL_TOKEN", "env_tok_123")
    monkeypatch.setenv("TUNNEL_HOSTNAME", "dav.env.org")
    args = csr.parse_args([])
    assert args.tunnel_token == "env_tok_123"
    assert args.tunnel_hostname == "dav.env.org"


def test_is_valid_tunnel_url_custom_hostname():
    """Verify is_valid_tunnel_url accepts custom hostnames when specified."""
    assert csr.is_valid_tunnel_url("https://dav.mydomain.com", custom_hostname="dav.mydomain.com") is True
    assert csr.is_valid_tunnel_url("https://dav.mydomain.com/dav/", custom_hostname="dav.mydomain.com") is True
    assert csr.is_valid_tunnel_url("https://unrelated.com", custom_hostname="dav.mydomain.com") is False
    # Localhost/127.0.0.1 must be rejected even if custom_hostname is set to it
    assert csr.is_valid_tunnel_url("http://localhost:7860", custom_hostname="localhost") is False
    assert csr.is_valid_tunnel_url("http://127.0.0.1:7860", custom_hostname="127.0.0.1") is False


def test_launch_tunnel_named_token(monkeypatch):
    """Verify launch_tunnel issues named tunnel token command."""
    mock_popen = MagicMock()
    mock_popen.pid = 9999

    with patch("subprocess.Popen", return_value=mock_popen) as mock_p:
        proc = csr.launch_tunnel(
            cloudflared_path="cloudflared",
            port=7860,
            tunnel_token="eyJhIjoiZGF2LXRva2VuIn0="
        )
        assert proc.pid == 9999
        called_cmd = mock_p.call_args[0][0]
        assert called_cmd == [
            "cloudflared", "tunnel", "run", "--token", "eyJhIjoiZGF2LXRva2VuIn0="
        ]


def test_probe_tunnel_health_success():
    """Verify probe_tunnel_health returns True on HTTP 200."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        res = csr.probe_tunnel_health("https://active-tunnel.trycloudflare.com")
        assert res is True
        called_req = mock_urlopen.call_args[0][0]
        assert called_req.full_url == "https://active-tunnel.trycloudflare.com/health"


def test_probe_tunnel_health_530_error():
    """Verify probe_tunnel_health returns False on Cloudflare HTTP 530 error."""
    error = urllib.error.HTTPError(
        url="https://dead-tunnel.trycloudflare.com/health",
        code=530,
        msg="Origin DNS error",
        hdrs={},
        fp=io.BytesIO(b"Error 1033: Argo Tunnel error")
    )
    with patch("urllib.request.urlopen", side_effect=error):
        res = csr.probe_tunnel_health("https://dead-tunnel.trycloudflare.com")
        assert res is False


def test_probe_tunnel_health_error_1033_body():
    """Verify probe_tunnel_health detects Error 1033 in response body on other HTTP error codes."""
    error = urllib.error.HTTPError(
        url="https://recycled-tunnel.trycloudflare.com/health",
        code=500,
        msg="Internal Error",
        hdrs={},
        fp=io.BytesIO(b"<html><body><h1>Error 1033</h1><p>Cloudflare Tunnel error</p></body></html>")
    )
    with patch("urllib.request.urlopen", side_effect=error):
        res = csr.probe_tunnel_health("https://recycled-tunnel.trycloudflare.com")
        assert res is False



