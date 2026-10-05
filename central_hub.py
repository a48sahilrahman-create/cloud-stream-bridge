"""
Central Pointer Hub - CloudStream Multi-User Suite
FastAPI Production Central Router Hub

Provides dynamic 302 Found WebDAV redirection, multi-user tunnel registration,
heartbeat telemetry, and stream mounting forwarding to Google Cloud Shell backbones.
Zero video byte proxying; zero bandwidth consumption on the hub.
"""

import os
import re
import time
import json
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

import httpx
from fastapi import FastAPI, Request, Response, HTTPException, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("central_hub")

# XML payload returned to CX File Explorer when Cloud Shell VM is dormant
DORMANT_XML_RESPONSE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<error>'
    '<message>Cloud Shell is dormant. Run your command in Google Cloud Shell to activate.</message>'
    '<status>dormant</status>'
    '</error>'
)


class BasePersistenceBackend:
    """Abstract base class for durable Central Hub persistence backends."""
    name: str = "base"

    def save_user(self, entry: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def update_touch(self, user_id: str, last_seen: float) -> bool:
        raise NotImplementedError

    def mark_inactive(self, user_id: str) -> bool:
        raise NotImplementedError

    def delete_user(self, user_id: str) -> bool:
        raise NotImplementedError

    def load_all_valid(self, now: float) -> Dict[str, Dict[str, Any]]:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError


class InMemoryPersistenceBackend(BasePersistenceBackend):
    """Fallback in-memory persistence backend."""
    name: str = "in_memory"

    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def save_user(self, entry: Dict[str, Any]) -> bool:
        with self._lock:
            self._store[entry["user_id"]] = dict(entry)
            return True

    def update_touch(self, user_id: str, last_seen: float) -> bool:
        with self._lock:
            if user_id in self._store:
                self._store[user_id]["last_seen"] = last_seen
                return True
            return False

    def mark_inactive(self, user_id: str) -> bool:
        return self.update_touch(user_id, 0.0)

    def delete_user(self, user_id: str) -> bool:
        with self._lock:
            return self._store.pop(user_id, None) is not None

    def load_all_valid(self, now: float) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            valid = {}
            for uid, entry in list(self._store.items()):
                if (entry["registered_at"] + entry["ttl_sec"]) > now:
                    valid[uid] = dict(entry)
                else:
                    self._store.pop(uid, None)
            return valid

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


class SQLitePersistenceBackend(BasePersistenceBackend):
    """
    SQLite persistence backend with Write-Ahead Logging (WAL) mode.
    Ensures zero data loss, ACID durability, and fast concurrency on persistent disk.
    """
    name: str = "sqlite"

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            parent = os.path.dirname(self.db_path)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            with self._get_connection() as conn:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id TEXT PRIMARY KEY,
                        tunnel_url TEXT NOT NULL,
                        token TEXT,
                        registered_at REAL NOT NULL,
                        last_seen REAL NOT NULL,
                        ttl_sec REAL NOT NULL,
                        heartbeat_timeout_sec REAL NOT NULL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen)")
                conn.commit()

    def save_user(self, entry: Dict[str, Any]) -> bool:
        with self._lock:
            try:
                with self._get_connection() as conn:
                    conn.execute("""
                        INSERT OR REPLACE INTO users (
                            user_id, tunnel_url, token, registered_at, last_seen, ttl_sec, heartbeat_timeout_sec
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        entry["user_id"],
                        entry["tunnel_url"],
                        entry.get("token"),
                        float(entry["registered_at"]),
                        float(entry["last_seen"]),
                        float(entry["ttl_sec"]),
                        float(entry.get("heartbeat_timeout_sec", 120)),
                    ))
                    conn.commit()
                return True
            except Exception as exc:
                logger.error("SQLite save_user failed: %s", exc)
                return False

    def update_touch(self, user_id: str, last_seen: float) -> bool:
        with self._lock:
            try:
                with self._get_connection() as conn:
                    conn.execute("UPDATE users SET last_seen = ? WHERE user_id = ?", (float(last_seen), user_id))
                    conn.commit()
                return True
            except Exception as exc:
                logger.error("SQLite update_touch failed: %s", exc)
                return False

    def mark_inactive(self, user_id: str) -> bool:
        return self.update_touch(user_id, 0.0)

    def delete_user(self, user_id: str) -> bool:
        with self._lock:
            try:
                with self._get_connection() as conn:
                    conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
                    conn.commit()
                return True
            except Exception as exc:
                logger.error("SQLite delete_user failed: %s", exc)
                return False

    def load_all_valid(self, now: float) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            try:
                with self._get_connection() as conn:
                    conn.execute("DELETE FROM users WHERE (registered_at + ttl_sec) <= ?", (now,))
                    conn.commit()
                    cursor = conn.execute("SELECT * FROM users WHERE (registered_at + ttl_sec) > ?", (now,))
                    rows = cursor.fetchall()
                    result = {}
                    for row in rows:
                        result[row["user_id"]] = {
                            "user_id": row["user_id"],
                            "tunnel_url": row["tunnel_url"],
                            "token": row["token"],
                            "registered_at": float(row["registered_at"]),
                            "last_seen": float(row["last_seen"]),
                            "ttl_sec": float(row["ttl_sec"]),
                            "heartbeat_timeout_sec": float(row["heartbeat_timeout_sec"]),
                        }
                    return result
            except Exception as exc:
                logger.error("SQLite load_all_valid failed: %s", exc)
                return {}

    def clear(self) -> None:
        with self._lock:
            try:
                with self._get_connection() as conn:
                    conn.execute("DELETE FROM users")
                    conn.commit()
            except Exception as exc:
                logger.error("SQLite clear failed: %s", exc)


class UpstashRedisPersistenceBackend(BasePersistenceBackend):
    """
    Upstash Serverless Redis REST API persistence backend.
    Survives Render spin-downs, cold starts, and container reboots with zero external dependencies.
    Free tier allows 10,000 commands/day.
    """
    name: str = "upstash_redis"

    def __init__(self, rest_url: str, rest_token: str):
        self.rest_url = rest_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {rest_token}",
            "Content-Type": "application/json",
        }
        self.prefix = "cloudstream:hub:user:"
        self.set_key = "cloudstream:hub:active_uids"

    def _execute(self, command: list) -> Any:
        try:
            with httpx.Client(timeout=4.0) as client:
                resp = client.post(self.rest_url, json=command, headers=self.headers)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("result")
                else:
                    logger.warning("Upstash Redis HTTP %s: %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("Upstash Redis command %s error: %s", command[0] if command else "UNKNOWN", exc)
        return None

    def save_user(self, entry: Dict[str, Any]) -> bool:
        user_id = entry["user_id"]
        key = f"{self.prefix}{user_id}"
        now = time.time()
        ttl_remaining = max(60, int(entry["registered_at"] + entry["ttl_sec"] - now))
        payload_str = json.dumps(entry)

        res = self._execute(["SET", key, payload_str, "EX", ttl_remaining])
        self._execute(["SADD", self.set_key, user_id])
        return res == "OK"

    def update_touch(self, user_id: str, last_seen: float) -> bool:
        key = f"{self.prefix}{user_id}"
        raw = self._execute(["GET", key])
        if not raw:
            return False
        try:
            entry = json.loads(raw)
            entry["last_seen"] = last_seen
            now = time.time()
            ttl_remaining = max(60, int(entry["registered_at"] + entry["ttl_sec"] - now))
            self._execute(["SET", key, json.dumps(entry), "EX", ttl_remaining])
            return True
        except Exception as exc:
            logger.warning("Upstash update_touch failed: %s", exc)
            return False

    def mark_inactive(self, user_id: str) -> bool:
        return self.update_touch(user_id, 0.0)

    def delete_user(self, user_id: str) -> bool:
        key = f"{self.prefix}{user_id}"
        self._execute(["DEL", key])
        self._execute(["SREM", self.set_key, user_id])
        return True

    def load_all_valid(self, now: float) -> Dict[str, Dict[str, Any]]:
        uids = self._execute(["SMEMBERS", self.set_key])
        if not uids or not isinstance(uids, list):
            return {}

        keys = [f"{self.prefix}{uid}" for uid in uids]
        if not keys:
            return {}

        raw_items = self._execute(["MGET"] + keys)
        if not raw_items or not isinstance(raw_items, list):
            return {}

        result = {}
        stale_uids = []
        for uid, raw_str in zip(uids, raw_items):
            if not raw_str:
                stale_uids.append(uid)
                continue
            try:
                entry = json.loads(raw_str)
                if (entry["registered_at"] + entry["ttl_sec"]) > now:
                    result[entry["user_id"]] = entry
                else:
                    stale_uids.append(uid)
            except Exception:
                stale_uids.append(uid)

        if stale_uids:
            try:
                self._execute(["SREM", self.set_key] + stale_uids)
            except Exception:
                pass

        return result

    def clear(self) -> None:
        uids = self._execute(["SMEMBERS", self.set_key])
        if uids and isinstance(uids, list):
            keys = [f"{self.prefix}{uid}" for uid in uids]
            if keys:
                self._execute(["DEL"] + keys)
        self._execute(["DEL", self.set_key])


def create_persistence_backend(
    backend_type: Optional[str] = None,
    db_path: Optional[str] = None,
) -> BasePersistenceBackend:
    """
    Factory function to initialize durable persistence backend.
    Priority:
    1. Upstash Redis REST API (UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN)
    2. SQLite with WAL mode (HUB_DB_PATH or PERSIST_SQLITE=1 or explicit db_path)
    3. In-memory fallback
    """
    chosen = (backend_type or os.environ.get("HUB_PERSISTENCE_BACKEND", "auto")).lower()

    # 1. Upstash Redis REST API
    upstash_url = os.environ.get("UPSTASH_REDIS_REST_URL")
    upstash_token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if (chosen in ("upstash", "redis", "auto")) and upstash_url and upstash_token:
        logger.info("Initializing Upstash Redis persistence backend (%s)", upstash_url)
        return UpstashRedisPersistenceBackend(upstash_url, upstash_token)

    # 2. SQLite with WAL mode
    configured_db_path = db_path or os.environ.get("HUB_DB_PATH")
    if (chosen in ("sqlite", "wal") or configured_db_path or os.environ.get("PERSIST_SQLITE") == "1"):
        sqlite_file = configured_db_path or os.path.join(os.getcwd(), "central_hub.db")
        logger.info("Initializing SQLite WAL persistence backend at %s", sqlite_file)
        return SQLitePersistenceBackend(sqlite_file)

    # 3. In-memory fallback
    logger.info("Initializing In-Memory persistence backend")
    return InMemoryPersistenceBackend()


class InMemoryUserRegistry:
    """
    Thread-safe registry mapping user_id to active tunnel endpoints.
    Combines sub-millisecond in-memory cache with pluggable durable persistence (Upstash / SQLite WAL).
    Enforces a 120s heartbeat timeout (runner pulses every 50s) and default 12h TTL.
    """

    HEARTBEAT_TIMEOUT_SEC = 120
    TOUCH_DEBOUNCE_SEC = 60.0  # Limit durable touch writes to at most once per 60s per user

    def __init__(
        self,
        default_ttl_sec: int = 43200,
        backend: Optional[BasePersistenceBackend] = None,
        db_path: Optional[str] = None,
        auto_restore: bool = True,
    ):
        self._lock = threading.RLock()
        self._users: Dict[str, Dict[str, Any]] = {}
        self.default_ttl_sec = default_ttl_sec
        self.backend = backend or create_persistence_backend(db_path=db_path)
        self._last_persisted_touch: Dict[str, float] = {}
        if auto_restore:
            self.restore_state()

    def restore_state(self) -> int:
        """
        Restore non-expired user registrations from durable storage on cold boots / restarts.
        Returns count of restored records.
        """
        with self._lock:
            now = time.time()
            try:
                persisted = self.backend.load_all_valid(now)
                restored_count = 0
                for uid, entry in persisted.items():
                    if uid not in self._users or self._users[uid].get("last_seen", 0) < entry.get("last_seen", 0):
                        self._users[uid] = dict(entry)
                        restored_count += 1
                if restored_count > 0:
                    logger.info("Restored %d active user registrations from %s backend", restored_count, self.backend.name)
                return restored_count
            except Exception as exc:
                logger.error("Failed to restore state from %s: %s", self.backend.name, exc)
                return 0

    def register(
        self,
        user_id: str,
        tunnel_url: str,
        token: Optional[str] = None,
        ttl_sec: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Register or update an active Cloud Shell tunnel for a user with durable write-through."""
        with self._lock:
            now = time.time()
            ttl = ttl_sec if ttl_sec is not None and ttl_sec > 0 else self.default_ttl_sec
            existing = self._users.get(user_id)

            clean_tunnel = tunnel_url.strip().rstrip("/")
            entry = {
                "user_id": user_id,
                "tunnel_url": clean_tunnel,
                "token": token if token is not None else (existing.get("token") if existing else None),
                "registered_at": now,
                "last_seen": now,
                "ttl_sec": ttl,
                "heartbeat_timeout_sec": self.HEARTBEAT_TIMEOUT_SEC,
            }
            self._users[user_id] = entry
            self._last_persisted_touch[user_id] = now
            try:
                self.backend.save_user(entry)
            except Exception as exc:
                logger.error("Failed to persist user %s: %s", user_id, exc)

            logger.info("User registered/updated: user_id=%s, tunnel=%s, ttl=%ss (persisted via %s)", user_id, clean_tunnel, ttl, self.backend.name)
            return dict(entry)

    def get(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve user record if present."""
        with self._lock:
            entry = self._users.get(user_id)
            return dict(entry) if entry else None

    def is_active(self, user_id: str) -> bool:
        """Check both session TTL and heartbeat timeout for an active user."""
        with self._lock:
            entry = self._users.get(user_id)
            if not entry:
                return False
            now = time.time()
            return (now - entry["last_seen"]) < entry.get("heartbeat_timeout_sec", self.HEARTBEAT_TIMEOUT_SEC) and (now - entry["registered_at"]) < entry["ttl_sec"]

    def mark_inactive(self, user_id: str) -> bool:
        """Set entry['last_seen'] = 0 so it immediately shows as inactive/dormant."""
        with self._lock:
            entry = self._users.get(user_id)
            if entry:
                entry["last_seen"] = 0
                self._last_persisted_touch[user_id] = 0
                try:
                    self.backend.mark_inactive(user_id)
                except Exception as exc:
                    logger.warning("Failed to persist mark_inactive for %s: %s", user_id, exc)
                logger.info("Marked user %s as inactive (last_seen=0)", user_id)
                return True
            return False

    def touch(self, user_id: str) -> bool:
        """Refresh last_seen heartbeat for an active user session with debounced persistence."""
        with self._lock:
            entry = self._users.get(user_id)
            if not entry:
                return False
            now = time.time()
            registered_at = entry.get("registered_at", entry["last_seen"])
            if (now - registered_at) < entry["ttl_sec"]:
                entry["last_seen"] = now
                last_saved = self._last_persisted_touch.get(user_id, 0.0)
                if (now - last_saved) >= self.TOUCH_DEBOUNCE_SEC:
                    self._last_persisted_touch[user_id] = now
                    try:
                        self.backend.update_touch(user_id, now)
                    except Exception as exc:
                        logger.warning("Failed to persist touch for %s: %s", user_id, exc)
                return True
            return False

    def get_status(self, user_id: str) -> Dict[str, Any]:
        """Return status dictionary conforming to specification."""
        with self._lock:
            entry = self._users.get(user_id)
            now = time.time()
            if not entry:
                return {
                    "active": False,
                    "user_id": user_id,
                    "tunnel_url": None,
                    "last_seen": None,
                    "ttl_remaining_sec": 0,
                }

            active = self.is_active(user_id)
            registered_at = entry.get("registered_at", entry["last_seen"])
            ttl_remaining = max(0, int(registered_at + entry["ttl_sec"] - now)) if active else 0

            return {
                "active": active,
                "user_id": user_id,
                "tunnel_url": entry["tunnel_url"] if active else None,
                "last_seen": entry["last_seen"],
                "ttl_remaining_sec": ttl_remaining,
            }

    def get_active_count(self) -> int:
        """Return count of users with valid, unexpired sessions."""
        with self._lock:
            return sum(1 for uid in self._users if self.is_active(uid))

    def get_total_count(self) -> int:
        """Return total number of registered records."""
        with self._lock:
            return len(self._users)

    def get_users(self) -> Dict[str, Dict[str, Any]]:
        """Return all user records with computed active and TTL status."""
        with self._lock:
            now = time.time()
            res = {}
            for uid, entry in self._users.items():
                active = self.is_active(uid)
                registered_at = entry.get("registered_at", entry["last_seen"])
                ttl_remaining = max(0, int(registered_at + entry["ttl_sec"] - now)) if active else 0
                res[uid] = {
                    "user_id": uid,
                    "active": active,
                    "tunnel_url": entry["tunnel_url"] if active else None,
                    "last_seen": entry["last_seen"],
                    "ttl_remaining_sec": ttl_remaining,
                    "registered_at": registered_at,
                }
            return res

    def remove(self, user_id: str) -> bool:
        """Remove user from registry and durable store."""
        with self._lock:
            self._last_persisted_touch.pop(user_id, None)
            try:
                self.backend.delete_user(user_id)
            except Exception as exc:
                logger.warning("Failed to delete user %s from backend: %s", user_id, exc)
            return self._users.pop(user_id, None) is not None

    def clear(self) -> None:
        """Clear all entries from memory and durable store."""
        with self._lock:
            self._users.clear()
            self._last_persisted_touch.clear()
            try:
                self.backend.clear()
            except Exception as exc:
                logger.warning("Failed to clear backend: %s", exc)


# Durable alias
DurableUserRegistry = InMemoryUserRegistry


class AntiSleepDaemon:
    """
    Background keep-alive daemon that periodically pings the hub's public URL
    to prevent Render free tier web services from spinning down after 15m idle.
    """

    def __init__(
        self,
        target_url: Optional[str] = None,
        interval_sec: int = 600,  # 10 minutes (well before 15m idle cutoff)
        timeout_sec: float = 15.0,
    ):
        self.target_url = target_url or os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("HUB_PUBLIC_URL")
        self.interval_sec = max(60, int(os.environ.get("KEEP_ALIVE_INTERVAL_SEC", str(interval_sec))))
        self.timeout_sec = timeout_sec
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_ping_time: Optional[float] = None
        self.last_ping_status: Optional[int] = None
        self.successful_pings: int = 0

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if not self.target_url:
            logger.info("AntiSleepDaemon: No target public URL configured (set RENDER_EXTERNAL_URL or HUB_PUBLIC_URL). Daemon idle.")
            return False

        if self.is_running():
            return True

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="AntiSleepKeepAlive", daemon=True)
        self._thread.start()
        logger.info("AntiSleepDaemon started: pinging %s/health every %ss", self.target_url, self.interval_sec)
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            logger.info("AntiSleepDaemon stopped.")

    def ping_now(self) -> Optional[int]:
        """Perform a single immediate health ping."""
        if not self.target_url:
            return None
        ping_url = f"{self.target_url.rstrip('/')}/health"
        try:
            with httpx.Client(timeout=self.timeout_sec) as client:
                resp = client.get(
                    ping_url,
                    headers={"User-Agent": "CloudStream-CentralHub-AntiSleep/1.0"}
                )
                self.last_ping_time = time.time()
                self.last_ping_status = resp.status_code
                if resp.status_code == 200:
                    self.successful_pings += 1
                    logger.info("Anti-sleep keep-alive ping succeeded (HTTP 200) to %s", ping_url)
                else:
                    logger.warning("Anti-sleep keep-alive ping returned HTTP %s from %s", resp.status_code, ping_url)
                return resp.status_code
        except Exception as exc:
            self.last_ping_time = time.time()
            self.last_ping_status = 0
            logger.warning("Anti-sleep keep-alive ping failed for %s: %s", ping_url, exc)
            return None

    def _run_loop(self) -> None:
        self._stop_event.wait(min(30, self.interval_sec))
        while not self._stop_event.is_set():
            self.ping_now()
            self._stop_event.wait(self.interval_sec)


# Global singleton registry and keep-alive daemon
registry = InMemoryUserRegistry(default_ttl_sec=43200)
keep_alive_daemon = AntiSleepDaemon()

# Request Models
class RegisterRequest(BaseModel):
    user_id: str = Field(..., min_length=1, description="Unique permanent user identifier")
    tunnel_url: str = Field(..., min_length=1, description="Active Cloudflare or reverse tunnel URL")
    token: Optional[str] = Field(default=None, description="Optional authentication token")
    ttl_sec: Optional[int] = Field(default=None, description="Custom TTL in seconds (default 43200 = 12h)")


class MountRequest(BaseModel):
    url: str = Field(..., min_length=1, description="Direct video stream upstream URL")
    title: Optional[str] = Field(default=None, description="Optional display title")
    custom_headers: Optional[Dict[str, str]] = Field(default=None, description="Optional custom HTTP headers")


class UnmountRequest(BaseModel):
    filename: str = Field(..., min_length=1, description="Filename of virtual stream to unmount")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Cold start: restore durable state
    restored = registry.restore_state()
    if restored > 0:
        logger.info("Restored %d active registrations from durable storage during startup", restored)
    # Start anti-sleep daemon if enabled
    if os.environ.get("ENABLE_KEEP_ALIVE", "1") != "0":
        keep_alive_daemon.start()
    yield
    keep_alive_daemon.stop()


# FastAPI Application Setup
app = FastAPI(
    title="CloudStream Central Pointer Hub",
    description="Zero-bandwidth central router providing dynamic 302 Found WebDAV redirection for Google Cloud Shell backbones",
    version="1.0.0",
    lifespan=lifespan,
)

# Enable CORS for all origins (Phone, Android TV, Browser, WebDAV clients)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Multi-User API Router for Central Pointer Hub
hub_router = APIRouter()


@app.get("/health")
@app.get("/")
async def health_check():
    """Health check endpoint reporting hub status, active user count, and persistence engine."""
    return {
        "status": "healthy",
        "service": "cloudstream-central-hub",
        "active_users": registry.get_active_count(),
        "total_registered": registry.get_total_count(),
        "persistence_engine": getattr(registry.backend, "name", "in_memory"),
        "keep_alive_active": keep_alive_daemon.is_running(),
        "timestamp": time.time(),
    }


@hub_router.get("/api/hub/persistence")
async def get_hub_persistence():
    """Returns telemetry regarding durable storage and keep-alive status."""
    return {
        "status": "success",
        "persistence_engine": getattr(registry.backend, "name", "in_memory"),
        "active_users": registry.get_active_count(),
        "total_registered": registry.get_total_count(),
        "keep_alive_active": keep_alive_daemon.is_running(),
        "keep_alive_target": keep_alive_daemon.target_url,
        "keep_alive_interval_sec": keep_alive_daemon.interval_sec,
        "last_ping_time": keep_alive_daemon.last_ping_time,
        "last_ping_status": keep_alive_daemon.last_ping_status,
        "successful_pings": keep_alive_daemon.successful_pings,
    }


@hub_router.post("/api/hub/keep-alive/ping")
async def trigger_keep_alive_ping():
    """Manually triggers an immediate anti-sleep ping to public hub URL."""
    status = keep_alive_daemon.ping_now()
    return {
        "status": "success" if status == 200 else "failed",
        "http_status": status,
        "target_url": keep_alive_daemon.target_url,
    }


@hub_router.post("/api/register")
async def register_user(payload: RegisterRequest):
    """
    Register or refresh a user's active Cloud Shell tunnel.
    Called by cloud_shell_init.sh / cloud_shell_runner.py upon tunnel establishment and heartbeat.
    """
    entry = registry.register(
        user_id=payload.user_id.strip(),
        tunnel_url=payload.tunnel_url.strip(),
        token=payload.token,
        ttl_sec=payload.ttl_sec,
    )
    return {
        "status": "registered",
        "user_id": entry["user_id"],
        "tunnel_url": entry["tunnel_url"],
        "active": True,
    }


@hub_router.api_route("/api/heartbeat/{user_id}", methods=["GET", "POST"])
async def api_heartbeat(user_id: str):
    """
    Heartbeat keep-alive endpoint refreshing last_seen timestamp for active user session.
    """
    uid = user_id.strip()
    is_active = registry.touch(uid)
    status_info = registry.get_status(uid)
    return {
        "status": "alive" if is_active else "dormant",
        **status_info,
    }


@hub_router.get("/api/status/{user_id}")
async def get_user_status(user_id: str):
    """
    Get active status and remaining TTL for a user.
    If candidate active is True, performs a fast probe to verify the tunnel is alive.
    If probe fails (HTTP 530, 502, connection refused, timeout), marks user inactive and returns active=False.
    If probe returns 200, returns active=True with real metrics.
    """
    uid = user_id.strip()
    status_info = registry.get_status(uid)
    if not status_info.get("active"):
        return status_info

    entry = registry.get(uid)
    tunnel_url = (entry.get("tunnel_url") if entry else None) or status_info.get("tunnel_url")
    if not tunnel_url:
        registry.mark_inactive(uid)
        return {
            "active": False,
            "user_id": uid,
            "tunnel_url": None,
            "last_seen": 0,
            "ttl_remaining_sec": 0,
            "reason": "tunnel_unreachable",
        }

    clean_tunnel = tunnel_url.rstrip("/")
    probe_success = False
    metrics: Dict[str, Any] = {}

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = None
            try:
                resp = await client.get(f"{clean_tunnel}/health")
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
                resp = None

            if resp is not None and resp.status_code == 200:
                probe_success = True
                try:
                    metrics = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
                except Exception:
                    metrics = {}
            elif resp is not None and resp.status_code in (502, 503, 504, 530):
                probe_success = False
            else:
                # If /health did not return 200 (e.g. 404), probe /api/status
                try:
                    resp_status = await client.get(f"{clean_tunnel}/api/status")
                    if resp_status.status_code == 200:
                        probe_success = True
                        try:
                            metrics = resp_status.json() if "application/json" in resp_status.headers.get("content-type", "") else {}
                        except Exception:
                            metrics = {}
                except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
                    probe_success = False

            # If probe succeeded, attempt to enrich with /api/status telemetry if not already present
            if probe_success and "streamed_gb" not in metrics:
                try:
                    stat_resp = await client.get(f"{clean_tunnel}/api/status")
                    if stat_resp.status_code == 200:
                        stat_json = stat_resp.json()
                        if isinstance(stat_json, dict):
                            metrics.update(stat_json)
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("Active probe exception for user %s tunnel %s: %s", uid, clean_tunnel, exc)
        probe_success = False

    if not probe_success:
        registry.mark_inactive(uid)
        return {
            "active": False,
            "user_id": uid,
            "tunnel_url": None,
            "last_seen": 0,
            "ttl_remaining_sec": 0,
            "reason": "tunnel_unreachable",
        }

    registry.touch(uid)
    res = {
        "active": True,
        "user_id": uid,
        "tunnel_url": clean_tunnel,
        "last_seen": time.time(),
        "ttl_remaining_sec": status_info.get("ttl_remaining_sec", 0),
        "metrics": metrics,
    }
    if isinstance(metrics, dict):
        for k, v in metrics.items():
            if k not in res:
                res[k] = v
    return res


@hub_router.get("/api/hub/users")
async def get_hub_users():
    """Returns list of registered users and their status."""
    return {
        "status": "success",
        "active_users": registry.get_active_count(),
        "total_registered": registry.get_total_count(),
        "users": registry.get_users(),
    }


@hub_router.get("/api/hub/status")
async def get_hub_status():
    """Returns central pointer hub telemetry and health status."""
    return {
        "status": "healthy",
        "service": "cloudstream-central-hub",
        "active_users": registry.get_active_count(),
        "total_registered": registry.get_total_count(),
        "timestamp": time.time(),
    }


@hub_router.post("/api/mount/{user_id}")
async def forward_mount_request(user_id: str, payload: MountRequest):
    """
    Forwards a stream mount request to the user's active Google Cloud Shell backend.
    If the user's session is dormant, returns HTTP 503.
    """
    uid = user_id.strip()
    if not registry.is_active(uid):
        logger.warning("Mount requested for dormant user: %s", uid)
        return JSONResponse(
            status_code=503,
            content={
                "error": "Cloud Shell is dormant. Run your command in Google Cloud Shell to activate.",
                "status": "dormant",
                "user_id": uid,
            },
        )

    entry = registry.get(uid)
    tunnel_url = entry["tunnel_url"].rstrip("/")
    target_url = f"{tunnel_url}/api/mount"

    logger.info("Forwarding mount request for %s to %s", uid, target_url)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                target_url,
                json=payload.model_dump(exclude_none=True),
                headers={"Content-Type": "application/json"},
            )
            media_type = resp.headers.get("content-type", "application/json")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=media_type,
            )
    except httpx.TimeoutException:
        logger.error("Timeout connecting to Cloud Shell tunnel for user %s at %s", uid, target_url)
        return JSONResponse(
            status_code=502,
            content={
                "error": f"Connection timed out forwarding mount to Cloud Shell at {tunnel_url}",
                "status": "forward_timeout",
                "user_id": uid,
            },
        )
    except httpx.RequestError as exc:
        logger.error("Network error forwarding mount for user %s: %s", uid, exc)
        return JSONResponse(
            status_code=502,
            content={
                "error": f"Failed to forward mount to Cloud Shell: {str(exc)}",
                "status": "forward_failed",
                "user_id": uid,
            },
        )


@hub_router.get("/api/mounts/{user_id}")
async def get_user_mounts(user_id: str):
    """
    Retrieves the list of active mounted streams from user's Cloud Shell instance.
    """
    uid = user_id.strip()
    if not registry.is_active(uid):
        return {"status": "dormant", "user_id": uid, "mounts": []}

    entry = registry.get(uid)
    tunnel_url = entry["tunnel_url"].rstrip("/")
    target_url = f"{tunnel_url}/api/mounts"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(target_url)
            media_type = resp.headers.get("content-type", "application/json")
            return Response(content=resp.content, status_code=resp.status_code, media_type=media_type)
    except Exception as exc:
        logger.error("Failed to fetch mounts for %s from %s: %s", uid, target_url, exc)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "message": f"Failed to fetch mounts: {str(exc)}", "user_id": uid, "mounts": []}
        )


@hub_router.delete("/api/mounts/{user_id}/{filename:path}")
@hub_router.delete("/api/unmount/{user_id}/{filename:path}")
@hub_router.post("/api/unmount/{user_id}/{filename:path}")
async def forward_unmount_request(user_id: str, filename: str):
    """
    Forwards an unmount request for a specific file to user's Cloud Shell.
    """
    uid = user_id.strip()
    if not registry.is_active(uid):
        return JSONResponse(status_code=503, content={"error": "Cloud Shell is dormant", "status": "dormant"})

    entry = registry.get(uid)
    tunnel_url = entry["tunnel_url"].rstrip("/")
    clean_filename = filename.strip()
    target_url = f"{tunnel_url}/api/mounts/{clean_filename}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(target_url)
            media_type = resp.headers.get("content-type", "application/json")
            return Response(content=resp.content, status_code=resp.status_code, media_type=media_type)
    except Exception as exc:
        logger.error("Failed to unmount %s for %s: %s", clean_filename, uid, exc)
        return JSONResponse(status_code=502, content={"error": f"Failed to unmount: {str(exc)}", "status": "error"})


@hub_router.post("/api/unmount/{user_id}")
async def forward_unmount_post_request(user_id: str, payload: UnmountRequest):
    """
    Forwards a JSON-based unmount request for a specific file to user's Cloud Shell.
    """
    return await forward_unmount_request(user_id=user_id, filename=payload.filename)


@hub_router.delete("/api/mounts/{user_id}")
@hub_router.delete("/api/unmount-all/{user_id}")
@hub_router.post("/api/unmount-all/{user_id}")
async def forward_unmount_all_request(user_id: str):
    """
    Forwards a batch unmount-all request to wipe all mounted files in user's Cloud Shell.
    """
    uid = user_id.strip()
    if not registry.is_active(uid):
        return JSONResponse(status_code=503, content={"error": "Cloud Shell is dormant", "status": "dormant"})

    entry = registry.get(uid)
    tunnel_url = entry["tunnel_url"].rstrip("/")
    target_url = f"{tunnel_url}/api/unmount-all"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(target_url)
            media_type = resp.headers.get("content-type", "application/json")
            return Response(content=resp.content, status_code=resp.status_code, media_type=media_type)
    except Exception as exc:
        logger.error("Failed to unmount all for %s: %s", uid, exc)
        return JSONResponse(status_code=502, content={"error": f"Failed to unmount all: {str(exc)}", "status": "error"})


# Include multi-user API router in central_hub app
app.include_router(hub_router)


def rewrite_single_href(url: str, tunnel_url: str, user_id: str) -> str:
    """
    Rewrite a single URL from an upstream WebDAV XML href.
    - If URL points to temporary tunnel_url, rewrite to permanent /dav/{user_id}/...
    - If URL is relative to /dav/, prefix with user_id (/dav/{user_id}/...)
    - Preserves URLs that already contain /dav/{user_id}/ or external URLs.
    """
    url_stripped = url.strip()
    clean_tunnel = tunnel_url.strip().rstrip("/")
    hub_prefix = f"/dav/{user_id}"

    # Candidate tunnel base URLs (support https and http)
    tunnels = [clean_tunnel]
    if clean_tunnel.startswith("https://"):
        tunnels.append("http://" + clean_tunnel[8:])
    elif clean_tunnel.startswith("http://"):
        tunnels.append("https://" + clean_tunnel[7:])

    for t in tunnels:
        if url_stripped.startswith(t):
            remainder = url_stripped[len(t):]
            if remainder.startswith("/dav"):
                remainder = remainder[4:]
            if not remainder.startswith("/"):
                remainder = "/" + remainder if remainder else "/"
            return f"{hub_prefix}{remainder}"

    # Handle upstream relative paths starting with /dav/
    if url_stripped.startswith("/dav/") and not url_stripped.startswith(f"/dav/{user_id}/"):
        remainder = url_stripped[len("/dav/"):]
        return f"{hub_prefix}/{remainder}"
    elif url_stripped == "/dav":
        return f"{hub_prefix}/"

    return url


def rewrite_webdav_hrefs(xml_content: str, tunnel_url: str, user_id: str) -> str:
    """
    Scan XML payload and rewrite any <D:href> (or <href>) URLs containing
    tunnel_url or relative /dav/ paths to point to permanent hub URLs (/dav/{user_id}/...).
    """
    pattern = re.compile(
        r"(<(?P<tag>(?:[A-Za-z0-9_-]+:)?href)\b[^>]*>)(.*?)(</(?P=tag)>)",
        re.IGNORECASE | re.DOTALL,
    )

    def _replace_href(match: re.Match) -> str:
        open_tag = match.group(1)
        raw_url = match.group(3)
        close_tag = match.group(4)
        rewritten = rewrite_single_href(raw_url, tunnel_url, user_id)
        return f"{open_tag}{rewritten}{close_tag}"

    return pattern.sub(_replace_href, xml_content)


def make_offline_error_response(
    user_id: str,
    exc: Exception,
    request: Request,
    tunnel_url: Optional[str] = None,
) -> Response:
    """
    Return robust HTTP 503 with informative JSON, text, or XML when Cloud Shell tunnel is offline or expired.
    """
    error_msg = f"Cloud Shell tunnel is offline or unreachable: {str(exc)}"
    accept = request.headers.get("accept", "").lower()

    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "*",
        "Access-Control-Allow-Headers": "*",
        "DAV": "1",
        "Retry-After": "10",
    }

    if "application/json" in accept:
        return JSONResponse(
            status_code=503,
            content={
                "error": error_msg,
                "status": "offline",
                "user_id": user_id,
                "tunnel_url": tunnel_url,
            },
            headers=headers,
        )

    if "text/plain" in accept:
        return Response(
            content=error_msg,
            status_code=503,
            media_type="text/plain; charset=utf-8",
            headers=headers,
        )

    # Standard WebDAV XML error response for CX File Explorer & other DAV clients
    escaped_msg = xml_escape(error_msg)
    escaped_uid = xml_escape(user_id)
    xml_body = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<error>\n'
        f'  <message>{escaped_msg}</message>\n'
        '  <status>offline</status>\n'
        f'  <user_id>{escaped_uid}</user_id>\n'
        '</error>'
    )
    return Response(
        content=xml_body,
        status_code=503,
        media_type="application/xml; charset=utf-8",
        headers=headers,
    )


@app.api_route(
    "/dav/{user_id}",
    methods=["GET", "HEAD", "PROPFIND", "OPTIONS", "PROPPATCH", "MKCOL", "DELETE", "POST", "PUT"],
)
@app.api_route(
    "/dav/{user_id}/",
    methods=["GET", "HEAD", "PROPFIND", "OPTIONS", "PROPPATCH", "MKCOL", "DELETE", "POST", "PUT"],
)
@app.api_route(
    "/dav/{user_id}/{path:path}",
    methods=["GET", "HEAD", "PROPFIND", "OPTIONS", "PROPPATCH", "MKCOL", "DELETE", "POST", "PUT"],
)
async def dav_redirect_router(user_id: str, request: Request, path: str = ""):
    """
    Central WebDAV entry point for CX File Explorer and media players.
    - If user is inactive or expired: returns HTTP 503 with informative XML/JSON/text body.
    - WebDAV metadata/directory queries (PROPFIND, OPTIONS, PROPPATCH, MKCOL, DELETE):
      Reverse-proxies directly to active Cloud Shell tunnel to avoid client redirect failure.
      Rewrites any <D:href> (or <href>) URLs in XML responses that contain tunnel_url so they
      point to permanent hub URLs (/dav/{user_id}/...).
    - Media streaming (GET, HEAD):
      * Direct 302 redirect to tunnel preserves Range headers (Accept-Ranges: bytes, Range in response,
        Access-Control-Expose-Headers) and all query parameters (token, seek, range, proxy flags).
      * Direct CDN Resolution: If tunnel can directly return upstream CDN 302 (requested via resolve_cdn=1,
        resolve=1, direct_cdn=1, x-resolve-cdn header, or RESOLVE_CDN_REDIRECT env), the hub queries
        the tunnel and redirects the client cleanly to the upstream CDN without unnecessary intermediate hops.
      * Robust error handling: Returns HTTP 503 with informative JSON/text/XML when tunnel is offline.
    """
    uid = user_id.strip()
    if not registry.is_active(uid):
        logger.info("WebDAV access attempted for dormant/expired user %s (%s)", uid, request.method)
        entry = registry.get(uid)
        is_expired = entry is not None and not registry.is_active(uid)
        accept = request.headers.get("accept", "").lower()

        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*",
            "DAV": "1",
        }

        if "application/json" in accept:
            return JSONResponse(
                status_code=503,
                content={
                    "error": (
                        f"Cloud Shell session has expired for user '{uid}'. Please re-run your Cloud Shell runner to reactivate."
                        if is_expired
                        else f"Cloud Shell is dormant for user '{uid}'. Run your command in Google Cloud Shell to activate."
                    ),
                    "status": "expired" if is_expired else "dormant",
                    "user_id": uid,
                    "active": False,
                },
                headers=headers,
            )

        if "text/plain" in accept:
            msg = (
                f"Cloud Shell session expired for user '{uid}'."
                if is_expired
                else f"Cloud Shell is dormant for user '{uid}'."
            )
            return Response(
                content=msg,
                status_code=503,
                media_type="text/plain; charset=utf-8",
                headers=headers,
            )

        return Response(
            content=DORMANT_XML_RESPONSE,
            status_code=503,
            media_type="application/xml; charset=utf-8",
            headers=headers,
        )

    entry = registry.get(uid)

    clean_path = path.lstrip("/")
    encoded_path = quote(clean_path, safe="/:@?=&") if clean_path else ""
    tunnel_url = entry["tunnel_url"].rstrip("/")
    target_url = f"{tunnel_url}/dav/{encoded_path}" if encoded_path else f"{tunnel_url}/dav/"

    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    # WebDAV metadata or directory query: reverse-proxy directly to upstream tunnel
    if request.method.upper() in {"PROPFIND", "OPTIONS", "PROPPATCH", "MKCOL", "DELETE"}:
        body = await request.body()
        fwd_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length")
        }
        logger.debug("Proxying WebDAV %s %s -> %s", request.method, request.url.path, target_url)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.request(
                    method=request.method,
                    url=target_url,
                    headers=fwd_headers,
                    content=body,
                )

                # Check if tunnel returned a gateway failure or dead Cloudflare tunnel HTML page
                if (resp.status_code in (502, 503, 504, 530)) or (
                    resp.status_code >= 500 and (b"<html" in resp.content.lower() or "text/html" in resp.headers.get("content-type", ""))
                ):
                    logger.warning("Upstream tunnel %s returned HTTP %s (offline)", target_url, resp.status_code)
                    registry.mark_inactive(uid)
                    return make_offline_error_response(
                        uid,
                        Exception(f"Upstream Cloud Shell tunnel is offline (HTTP {resp.status_code})"),
                        request,
                        tunnel_url=tunnel_url,
                    )

                # Successful upstream response: only touch if status_code < 500
                if resp.status_code < 500:
                    registry.touch(uid)

                media_type = resp.headers.get("content-type", "application/xml; charset=utf-8")
                content = resp.content

                # Rewrite <D:href> URLs in XML responses that contain tunnel_url or relative /dav/ paths
                if content and ("xml" in media_type.lower() or content.strip().startswith(b"<?xml") or b"<" in content):
                    try:
                        decoded = content.decode("utf-8")
                        rewritten = rewrite_webdav_hrefs(decoded, tunnel_url, uid)
                        content = rewritten.encode("utf-8")
                    except Exception as rewrite_err:
                        logger.warning("Error rewriting XML hrefs for %s: %s", uid, rewrite_err)

                response_headers = {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "*",
                    "Access-Control-Allow-Headers": "*",
                    "DAV": "1",
                }
                for header_name in ("Allow", "DAV", "MS-Author-Via", "ETag"):
                    if header_name in resp.headers:
                        response_headers[header_name] = resp.headers[header_name]

                if "location" in resp.headers:
                    response_headers["Location"] = rewrite_single_href(resp.headers["location"], tunnel_url, uid)

                return Response(
                    content=content,
                    status_code=resp.status_code,
                    media_type=media_type,
                    headers=response_headers,
                )
        except Exception as exc:
            logger.error("WebDAV proxy %s to %s failed: %s", request.method, target_url, exc)
            registry.mark_inactive(uid)
            return make_offline_error_response(uid, exc, request, tunnel_url=tunnel_url)

    # Media streaming (GET, HEAD): HTTP 302 Found redirect preserving zero video proxying
    client_range = request.headers.get("range")
    redirect_headers = {
        "Location": target_url,
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "*",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Expose-Headers": "Location, Range, Content-Range, Accept-Ranges",
        "DAV": "1",
    }
    if client_range:
        redirect_headers["Range"] = client_range

    # Clean CDN Direct Mode: If requested, query tunnel to return upstream CDN 302 directly without unnecessary hops
    should_resolve_cdn = (
        request.query_params.get("resolve_cdn") == "1"
        or request.query_params.get("resolve") == "1"
        or request.query_params.get("direct_cdn") == "1"
        or request.headers.get("x-resolve-cdn") == "1"
        or os.environ.get("RESOLVE_CDN_REDIRECT") == "1"
    )
    if should_resolve_cdn and clean_path:
        fwd_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length")
        }
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                tunnel_probe = await client.request(
                    method=request.method,
                    url=target_url,
                    headers=fwd_headers,
                    follow_redirects=False,
                )
                if (tunnel_probe.status_code in (502, 503, 504, 530)) or (
                    tunnel_probe.status_code >= 500 and (b"<html" in tunnel_probe.content.lower() or "text/html" in tunnel_probe.headers.get("content-type", ""))
                ):
                    registry.mark_inactive(uid)
                    return make_offline_error_response(
                        uid,
                        Exception(f"Upstream Cloud Shell tunnel is offline (HTTP {tunnel_probe.status_code})"),
                        request,
                        tunnel_url=tunnel_url,
                    )

                if tunnel_probe.status_code < 500:
                    registry.touch(uid)

                if tunnel_probe.status_code in (301, 302, 307, 308) and "location" in tunnel_probe.headers:
                    cdn_url = tunnel_probe.headers["location"]
                    logger.info("Direct CDN 302 resolution: user %s -> %s", uid, cdn_url)
                    cdn_headers = dict(redirect_headers)
                    cdn_headers["Location"] = cdn_url
                    return Response(status_code=302, headers=cdn_headers)
        except Exception as exc:
            logger.error("Failed to query tunnel for CDN 302 (%s): %s", target_url, exc)
            registry.mark_inactive(uid)
            return make_offline_error_response(uid, exc, request, tunnel_url=tunnel_url)

    logger.debug("Redirecting %s %s -> %s", request.method, request.url.path, target_url)
    registry.touch(uid)
    return Response(
        status_code=302,
        headers=redirect_headers,
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("central_hub:app", host="0.0.0.0", port=port, reload=False)
