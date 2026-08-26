"""Idempotency layer: at-most-once recovery execution.

Three belt-and-braces guarantees, all enforced by SQLite:

1. CLAIM LOCK   - atomic INSERT of a PENDING claim before any work;
                  concurrent duplicates read the cached state instead.
2. DECISION CACHE - duplicates receive the original decision verbatim.
3. EXECUTION PK - the physical execution record has a PRIMARY KEY on the
                  idempotency key; even a buggy loop cannot record (or
                  therefore perform) a second execution.

A stale-claim sweeper converts abandoned PENDING claims (crash mid-
processing) into ESCALATE_STALE so workflows never hang forever.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(slots=True)
class ClaimResult:
    won: bool
    key: str
    cached_state: str | None = None
    cached_decision: str | None = None
    cached_reason: str | None = None


class IdempotencyRepository:
    """Durable at-most-once registry backed by SQLite (WAL)."""

    def __init__(self, db_path: str | Path = "idempotency.db"):
        self.db_path = str(db_path)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recovery_claims (
                    idempotency_key TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    decision TEXT,
                    reason TEXT,
                    claimed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    idempotency_key TEXT PRIMARY KEY,
                    razorpay_ref TEXT NOT NULL,
                    executed_at TEXT NOT NULL
                )
                """
            )
        conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def claim(self, key: str) -> ClaimResult:
        """Atomically claim the right to process `key` exactly once."""
        conn = self._get_conn()
        now = self._now()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO recovery_claims
                        (idempotency_key, state, decision, reason,
                         claimed_at, updated_at)
                    VALUES (?, 'PENDING', NULL, NULL, ?, ?)
                    """,
                    (key, now, now),
                )
            return ClaimResult(won=True, key=key)
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT state, decision, reason FROM recovery_claims WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            return ClaimResult(
                won=False,
                key=key,
                cached_state=row[0] if row else None,
                cached_decision=row[1] if row else None,
                cached_reason=row[2] if row else None,
            )

    def record_decision(self, key: str, decision: str, reason: str) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                UPDATE recovery_claims
                SET state = 'DECIDED', decision = ?, reason = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (decision, reason, self._now(), key),
            )

    def record_execution(self, key: str, razorpay_ref: str) -> bool:
        """Record the physical execution. Returns False if this key has
        ALREADY been executed — the caller MUST NOT execute again."""
        conn = self._get_conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO executions
                        (idempotency_key, razorpay_ref, executed_at)
                    VALUES (?, ?, ?)
                    """,
                    (key, razorpay_ref, self._now()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_execution(self, key: str) -> tuple[str, str] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT razorpay_ref, executed_at FROM executions WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def sweep_stale(self, max_pending_minutes: float = 10.0) -> int:
        """Convert abandoned PENDING claims to ESCALATE_STALE."""
        cutoff = (datetime.now(UTC) - timedelta(minutes=max_pending_minutes)).isoformat()
        conn = self._get_conn()
        with conn:
            cur = conn.execute(
                """
                UPDATE recovery_claims
                SET state = 'DECIDED', decision = 'ESCALATE_STALE',
                    reason = 'claim abandoned (crash during processing)',
                    updated_at = ?
                WHERE state = 'PENDING' AND claimed_at < ?
                """,
                (self._now(), cutoff),
            )
        return cur.rowcount

    def stats(self) -> dict[str, int]:
        conn = self._get_conn()
        claims = conn.execute("SELECT COUNT(*) FROM recovery_claims").fetchone()[0]
        executed = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        deduped = claims - executed
        return {
            "claims": claims,
            "executed": executed,
            "deduplicated": max(0, deduped),
        }
