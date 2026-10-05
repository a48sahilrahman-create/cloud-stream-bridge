"""
Unit and Integration Tests for Central Hub Durability, State Persistence & Anti-Sleep Daemon
Tests SQLite WAL persistence, Upstash Redis REST backend, touch debouncing,
cold start state re-hydration, and anti-sleep keep-alive loop.
"""

import os
import sys
import time
import tempfile
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

# Ensure parent directory is in Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from central_hub import (
    app,
    registry,
    InMemoryUserRegistry,
    SQLitePersistenceBackend,
    UpstashRedisPersistenceBackend,
    InMemoryPersistenceBackend,
    AntiSleepDaemon,
    keep_alive_daemon,
)

client = TestClient(app)


# ============================================================================
# 1. SQLite WAL Persistence & Cold Start Recovery
# ============================================================================

def test_sqlite_wal_persistence_and_restore():
    """Verify SQLite WAL backend saves records and restores across cold restart."""
    temp_dir = tempfile.mkdtemp()
    db_file = os.path.join(temp_dir, "test_hub_wal.db")

    try:
        sqlite_backend = SQLitePersistenceBackend(db_file)
        reg1 = InMemoryUserRegistry(default_ttl_sec=3600, backend=sqlite_backend)

        # Register user
        reg1.register(
            user_id="user_cold_boot",
            tunnel_url="https://persisted-tunnel.trycloudflare.com",
            token="auth_tok_123",
            ttl_sec=3600,
        )

        assert reg1.is_active("user_cold_boot") is True
        assert reg1.get_active_count() == 1

        # Simulate cold restart: instantiate fresh registry pointing to same SQLite database
        sqlite_backend2 = SQLitePersistenceBackend(db_file)
        reg2 = InMemoryUserRegistry(default_ttl_sec=3600, backend=sqlite_backend2)

        # Verify state was automatically restored
        assert reg2.get_total_count() == 1
        assert reg2.is_active("user_cold_boot") is True
        restored_entry = reg2.get("user_cold_boot")
        assert restored_entry is not None
        assert restored_entry["tunnel_url"] == "https://persisted-tunnel.trycloudflare.com"
        assert restored_entry["token"] == "auth_tok_123"

        # Test mark inactive
        reg2.mark_inactive("user_cold_boot")
        assert reg2.is_active("user_cold_boot") is False

        # Simulate another cold restart after mark_inactive
        sqlite_backend3 = SQLitePersistenceBackend(db_file)
        reg3 = InMemoryUserRegistry(default_ttl_sec=3600, backend=sqlite_backend3)
        assert reg3.get_total_count() == 1
        assert reg3.is_active("user_cold_boot") is False
        assert reg3.get("user_cold_boot")["last_seen"] == 0.0

        # Test removal
        reg3.remove("user_cold_boot")
        assert reg3.get_total_count() == 0

        # Verify removal persisted
        sqlite_backend4 = SQLitePersistenceBackend(db_file)
        reg4 = InMemoryUserRegistry(default_ttl_sec=3600, backend=sqlite_backend4)
        assert reg4.get_total_count() == 0

    finally:
        # Cleanup
        for ext in ["", "-wal", "-shm"]:
            f = db_file + ext
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass
        try:
            os.rmdir(temp_dir)
        except Exception:
            pass


def test_sqlite_prunes_expired_on_restore():
    """Verify that expired sessions are automatically pruned during cold start restore."""
    temp_dir = tempfile.mkdtemp()
    db_file = os.path.join(temp_dir, "test_hub_expire.db")

    try:
        backend1 = SQLitePersistenceBackend(db_file)
        reg1 = InMemoryUserRegistry(default_ttl_sec=3600, backend=backend1)

        # Register user with 2 second TTL
        reg1.register(
            user_id="user_short_ttl",
            tunnel_url="https://short-tunnel.trycloudflare.com",
            ttl_sec=1,
        )
        assert reg1.get_total_count() == 1

        # Simulate time advancing 10 seconds into the future
        future_time = time.time() + 10.0
        backend2 = SQLitePersistenceBackend(db_file)
        with patch("time.time", return_value=future_time):
            reg2 = InMemoryUserRegistry(default_ttl_sec=3600, backend=backend2)
            assert reg2.get_total_count() == 0
            assert reg2.get("user_short_ttl") is None

    finally:
        for ext in ["", "-wal", "-shm"]:
            f = db_file + ext
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass
        try:
            os.rmdir(temp_dir)
        except Exception:
            pass


# ============================================================================
# 2. Touch Debouncing
# ============================================================================

def test_touch_debouncing_conserves_writes():
    """Verify that runner pulses every 50s do not spam the durable storage backend."""
    mock_backend = MagicMock(spec=InMemoryPersistenceBackend)
    mock_backend.name = "mock_backend"
    mock_backend.load_all_valid.return_value = {}

    reg = InMemoryUserRegistry(default_ttl_sec=3600, backend=mock_backend)
    reg.register("user_debounce", "https://tunnel.trycloudflare.com")

    # Save user was called once on register
    assert mock_backend.save_user.call_count == 1
    assert mock_backend.update_touch.call_count == 0

    # Pulse 1: 5 seconds later (within 60s debounce window)
    with patch("time.time", return_value=time.time() + 5):
        reg.touch("user_debounce")
        assert mock_backend.update_touch.call_count == 0

    # Pulse 2: 30 seconds later (still within 60s window)
    with patch("time.time", return_value=time.time() + 30):
        reg.touch("user_debounce")
        assert mock_backend.update_touch.call_count == 0

    # Pulse 3: 65 seconds later (> 60s window) -> durable update triggered!
    with patch("time.time", return_value=time.time() + 65):
        reg.touch("user_debounce")
        assert mock_backend.update_touch.call_count == 1


# ============================================================================
# 3. Upstash Redis REST Backend
# ============================================================================

def test_upstash_redis_backend_operations():
    """Verify Upstash Redis REST API backend execution and mapping."""
    backend = UpstashRedisPersistenceBackend(
        rest_url="https://mock-upstash-redis.upstash.io",
        rest_token="fake_secret_token",
    )

    now = time.time()
    user_entry = {
        "user_id": "upstash_user_1",
        "tunnel_url": "https://upstash-tunnel.trycloudflare.com",
        "token": None,
        "registered_at": now,
        "last_seen": now,
        "ttl_sec": 3600,
        "heartbeat_timeout_sec": 120,
    }

    # Test save_user
    with patch.object(backend, "_execute", return_value="OK") as mock_exec:
        success = backend.save_user(user_entry)
        assert success is True
        assert mock_exec.call_count == 2
        # Command 1: SET key payload EX ttl
        set_call = mock_exec.call_args_list[0][0][0]
        assert set_call[0] == "SET"
        assert set_call[1] == "cloudstream:hub:user:upstash_user_1"
        assert set_call[3] == "EX"
        # Command 2: SADD active_uids user_id
        sadd_call = mock_exec.call_args_list[1][0][0]
        assert sadd_call == ["SADD", "cloudstream:hub:active_uids", "upstash_user_1"]

    # Test load_all_valid
    import json
    with patch.object(backend, "_execute") as mock_exec:
        def side_effect(cmd):
            if cmd[0] == "SMEMBERS":
                return ["upstash_user_1"]
            if cmd[0] == "MGET":
                return [json.dumps(user_entry)]
            return None
        mock_exec.side_effect = side_effect

        loaded = backend.load_all_valid(now)
        assert len(loaded) == 1
        assert "upstash_user_1" in loaded
        assert loaded["upstash_user_1"]["tunnel_url"] == "https://upstash-tunnel.trycloudflare.com"

    # Test delete_user
    with patch.object(backend, "_execute", return_value=1) as mock_exec:
        backend.delete_user("upstash_user_1")
        assert mock_exec.call_count == 2
        assert mock_exec.call_args_list[0][0][0] == ["DEL", "cloudstream:hub:user:upstash_user_1"]
        assert mock_exec.call_args_list[1][0][0] == ["SREM", "cloudstream:hub:active_uids", "upstash_user_1"]


# ============================================================================
# 4. Anti-Sleep Keep-Alive Daemon
# ============================================================================

def test_anti_sleep_daemon_lifecycle():
    """Verify AntiSleepDaemon start, ping, status tracking, and stop."""
    daemon = AntiSleepDaemon(
        target_url="https://cloud-stream-bridge.onrender.com",
        interval_sec=60,
        timeout_sec=5.0,
    )

    assert daemon.target_url == "https://cloud-stream-bridge.onrender.com"
    assert daemon.interval_sec == 60
    assert daemon.is_running() is False

    # Test ping_now with mocked HTTP 200 response
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("httpx.Client.get", return_value=mock_resp):
        code = daemon.ping_now()
        assert code == 200
        assert daemon.last_ping_status == 200
        assert daemon.successful_pings == 1
        assert daemon.last_ping_time is not None

    # Test daemon without target URL stays idle
    idle_daemon = AntiSleepDaemon(target_url=None)
    assert idle_daemon.start() is False
    assert idle_daemon.is_running() is False


# ============================================================================
# 5. Hub Persistence Endpoints
# ============================================================================

def test_hub_persistence_telemetry_endpoint():
    """Verify GET /api/hub/persistence returns detailed engine telemetry."""
    res = client.get("/api/hub/persistence")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "persistence_engine" in data
    assert "active_users" in data
    assert "total_registered" in data
    assert "keep_alive_active" in data


def test_trigger_keep_alive_ping_endpoint():
    """Verify POST /api/hub/keep-alive/ping triggers keep-alive ping."""
    with patch.object(keep_alive_daemon, "ping_now", return_value=200):
        res = client.post("/api/hub/keep-alive/ping")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["http_status"] == 200
