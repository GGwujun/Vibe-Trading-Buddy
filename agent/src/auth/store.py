"""SQLite-backed user store for registration/login + disclaimer state.

Database: ``~/.vibe-trading/users.db`` (WAL mode, check_same_thread=False —
mirrors the pattern in ``src/session/search.py``). The store is process-local;
FastAPI runs workers in threads (not separate processes for uvicorn default),
so a single shared connection with a write lock is sufficient.

Schema::

    users(
      id TEXT PRIMARY KEY,
      email TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      disclaimer_accepted_at TEXT NULL,
      created_at TEXT NOT NULL
    )
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.auth.password import hash_password, verify_password

logger = logging.getLogger(__name__)

_DB_PATH = Path.home() / ".vibe-trading" / "users.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UserStore:
    """Thread-safe SQLite user store."""

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # WAL for concurrent reads while a write is in flight.
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
        return self._conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._init_conn_locked()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    disclaimer_accepted_at TEXT,
                    created_at TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Migration: add is_admin column if upgrading from an older schema.
            try:
                conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.commit()
        # Ensure the seeded admin account exists.
        self._ensure_admin()

    def _init_conn_locked(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
        return self._conn

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "email": row["email"],
            "disclaimer_accepted_at": row["disclaimer_accepted_at"],
            "created_at": row["created_at"],
            "is_admin": bool(row["is_admin"]) if "is_admin" in row.keys() else False,
        }

    def _ensure_admin(self) -> None:
        """Seed the admin account from env vars on startup. Idempotent.

        ADMIN_EMAIL / ADMIN_PASSWORD override the defaults; the admin always
        has is_admin=1 and disclaimer pre-accepted (so the modal doesn't gate
        the operator).
        """
        import os
        email = os.getenv("ADMIN_EMAIL", "admin@sigmx.local").strip().lower()
        password = os.getenv("ADMIN_PASSWORD", "admin123")
        with self._lock:
            conn = self._init_conn_locked()
            row = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
            if row:
                # Promote to admin if it somehow isn't.
                conn.execute("UPDATE users SET is_admin = 1 WHERE email = ?", (email,))
                conn.commit()
                return
            user_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO users (id, email, password_hash, disclaimer_accepted_at, created_at, is_admin) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (user_id, email, hash_password(password), _now_iso(), _now_iso()),
            )
            conn.commit()
        logger.info("Seeded admin account: %s", email)

    def create_user(self, email: str, password: str) -> dict[str, Any]:
        """Insert a new user. Raises ValueError if email already exists."""
        email = email.strip().lower()
        with self._lock:
            conn = self._init_conn_locked()
            existing = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                raise ValueError("该邮箱已注册")
            user_id = uuid.uuid4().hex
            created_at = _now_iso()
            conn.execute(
                "INSERT INTO users (id, email, password_hash, disclaimer_accepted_at, created_at) "
                "VALUES (?, ?, ?, NULL, ?)",
                (user_id, email, hash_password(password), created_at),
            )
            conn.commit()
        return {"id": user_id, "email": email, "disclaimer_accepted_at": None, "created_at": created_at}

    def verify_credentials(self, email: str, password: str) -> dict[str, Any] | None:
        """Return the user dict if email+password match, else None."""
        email = email.strip().lower()
        with self._lock:
            conn = self._init_conn_locked()
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email,)
            ).fetchone()
        if row is None:
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return self._row_to_user(row)

    def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._init_conn_locked()
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def set_disclaimer_accepted(self, user_id: str) -> bool:
        """Mark the disclaimer as accepted. Returns True if a row was updated."""
        with self._lock:
            conn = self._init_conn_locked()
            cur = conn.execute(
                "UPDATE users SET disclaimer_accepted_at = ? WHERE id = ?",
                (_now_iso(), user_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def set_password_hash(self, user_id: str, password_hash: str) -> bool:
        """Replace a user's password hash (for change-password). Returns True if updated."""
        with self._lock:
            conn = self._init_conn_locked()
            cur = conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, user_id),
            )
            conn.commit()
            return cur.rowcount > 0

    # Backwards-compat alias used by credits_routes.
    _set_password_hash = set_password_hash
