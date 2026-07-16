from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_EVENT_TEXT_LENGTH = 500
MAX_EVENT_DETAILS_LENGTH = 4000


@dataclass(frozen=True)
class QuotaResult:
    allowed: bool
    message: str = ""


class RuntimeStore:
    """SQLite-backed usage, domain health, metrics and structured event store."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self):
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rate_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rate_events_user_time
                    ON rate_events(user_id, created_at);

                CREATE TABLE IF NOT EXISTS daily_usage (
                    usage_day TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    downloads INTEGER NOT NULL DEFAULT 0,
                    bytes_sent INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (usage_day, user_id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    level TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    success INTEGER,
                    jm_id TEXT,
                    user_id TEXT,
                    session TEXT,
                    reason TEXT,
                    duration_ms INTEGER,
                    bytes_count INTEGER,
                    details_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_time
                    ON events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_type_time
                    ON events(event_type, created_at DESC);

                CREATE TABLE IF NOT EXISTS domain_health (
                    domain TEXT PRIMARY KEY,
                    healthy INTEGER NOT NULL,
                    latency_ms INTEGER,
                    status_code INTEGER,
                    checked_at REAL NOT NULL,
                    error TEXT,
                    final_domain TEXT,
                    redirect_count INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(domain_health)")
            }
            if "final_domain" not in columns:
                connection.execute(
                    "ALTER TABLE domain_health ADD COLUMN final_domain TEXT"
                )
            if "redirect_count" not in columns:
                connection.execute(
                    "ALTER TABLE domain_health ADD COLUMN "
                    "redirect_count INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                "UPDATE domain_health SET redirect_count = 0 "
                "WHERE redirect_count IS NULL"
            )

    @staticmethod
    def current_day() -> str:
        return datetime.now().astimezone().date().isoformat()

    def check_and_record_rate(
        self, user_id: str, request_limit: int, window_seconds: int
    ) -> QuotaResult:
        if request_limit <= 0 or window_seconds <= 0:
            return QuotaResult(True)
        now = time.time()
        cutoff = now - window_seconds
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM rate_events WHERE created_at < ?", (cutoff,)
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM rate_events "
                "WHERE user_id = ? AND created_at >= ?",
                (user_id, cutoff),
            ).fetchone()[0]
            if count >= request_limit:
                return QuotaResult(
                    False,
                    f"请求过于频繁：每 {window_seconds} 秒最多提交 "
                    f"{request_limit} 次，请稍后再试。",
                )
            connection.execute(
                "INSERT INTO rate_events(user_id, created_at) VALUES (?, ?)",
                (user_id, now),
            )
        return QuotaResult(True)

    def usage_for(self, user_id: str, usage_day: str | None = None) -> dict[str, int]:
        day = usage_day or self.current_day()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT downloads, bytes_sent FROM daily_usage "
                "WHERE usage_day = ? AND user_id = ?",
                (day, user_id),
            ).fetchone()
        if row is None:
            return {"downloads": 0, "bytes_sent": 0}
        return {"downloads": int(row["downloads"]), "bytes_sent": int(row["bytes_sent"])}

    def reserve_delivery(
        self,
        user_id: str,
        bytes_count: int,
        daily_download_limit: int,
        daily_bytes_limit: int,
    ) -> QuotaResult:
        day = self.current_day()
        size = max(0, int(bytes_count))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT downloads, bytes_sent FROM daily_usage "
                "WHERE usage_day = ? AND user_id = ?",
                (day, user_id),
            ).fetchone()
            downloads = int(row["downloads"]) if row else 0
            bytes_sent = int(row["bytes_sent"]) if row else 0
            if daily_download_limit > 0 and downloads + 1 > daily_download_limit:
                return QuotaResult(
                    False,
                    f"今天的下载次数已达到上限（{daily_download_limit} 次）。",
                )
            if daily_bytes_limit > 0 and bytes_sent + size > daily_bytes_limit:
                remaining = max(0, daily_bytes_limit - bytes_sent)
                return QuotaResult(
                    False,
                    "今天的流量配额不足，剩余 "
                    f"{_format_bytes(remaining)}，当前文件为 {_format_bytes(size)}。",
                )
            connection.execute(
                """
                INSERT INTO daily_usage(usage_day, user_id, downloads, bytes_sent)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(usage_day, user_id) DO UPDATE SET
                    downloads = downloads + 1,
                    bytes_sent = bytes_sent + excluded.bytes_sent
                """,
                (day, user_id, size),
            )
        return QuotaResult(True)

    def release_delivery(self, user_id: str, bytes_count: int):
        day = self.current_day()
        size = max(0, int(bytes_count))
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE daily_usage SET
                    downloads = MAX(0, downloads - 1),
                    bytes_sent = MAX(0, bytes_sent - ?)
                WHERE usage_day = ? AND user_id = ?
                """,
                (size, day, user_id),
            )
            connection.execute(
                "DELETE FROM daily_usage WHERE usage_day = ? AND user_id = ? "
                "AND downloads = 0 AND bytes_sent = 0",
                (day, user_id),
            )

    def record_event(
        self,
        event_type: str,
        *,
        level: str = "info",
        success: bool | None = None,
        jm_id: str | None = None,
        user_id: str | None = None,
        session: str | None = None,
        reason: str | None = None,
        duration_ms: int | None = None,
        bytes_count: int | None = None,
        details: dict[str, Any] | None = None,
    ):
        safe_reason = _safe_text(reason)
        details_json = (
            json.dumps(details, ensure_ascii=False, separators=(",", ":"))
            if details
            else None
        )
        if details_json and len(details_json) > MAX_EVENT_DETAILS_LENGTH:
            details_json = json.dumps(
                {
                    "truncated": True,
                    "preview": details_json[: MAX_EVENT_DETAILS_LENGTH - 100],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events(
                    created_at, level, event_type, success, jm_id, user_id,
                    session, reason, duration_ms, bytes_count, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    _safe_text(level, 20) or "info",
                    _safe_text(event_type, 80) or "unknown",
                    None if success is None else int(success),
                    _safe_text(jm_id, 32),
                    _safe_text(user_id, 80),
                    _safe_text(session, 160),
                    safe_reason,
                    duration_ms,
                    bytes_count,
                    details_json,
                ),
            )

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 200)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (safe_limit,)
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def metrics(self, days: int = 7) -> dict[str, Any]:
        now = time.time()
        cutoff = now - max(1, days) * 86400
        today = self.current_day()
        with self._lock, self._connect() as connection:
            usage = connection.execute(
                "SELECT COALESCE(SUM(downloads), 0) AS downloads, "
                "COALESCE(SUM(bytes_sent), 0) AS bytes_sent, "
                "COUNT(*) AS users FROM daily_usage WHERE usage_day = ?",
                (today,),
            ).fetchone()
            top_users = connection.execute(
                """
                SELECT user_id, downloads, bytes_sent
                FROM daily_usage WHERE usage_day = ?
                ORDER BY downloads DESC, bytes_sent DESC LIMIT 20
                """,
                (today,),
            ).fetchall()
            outcomes = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) AS success,
                    COALESCE(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END), 0) AS failure
                FROM events
                WHERE created_at >= ?
                  AND event_type IN ('delivery_succeeded', 'delivery_failed', 'job_failed')
                  AND success IS NOT NULL
                """,
                (cutoff,),
            ).fetchone()
            reasons = connection.execute(
                """
                SELECT COALESCE(reason, '未知原因') AS reason, COUNT(*) AS count
                FROM events
                WHERE created_at >= ? AND success = 0
                  AND event_type IN ('delivery_failed', 'job_failed')
                GROUP BY COALESCE(reason, '未知原因')
                ORDER BY count DESC LIMIT 10
                """,
                (cutoff,),
            ).fetchall()
            daily = connection.execute(
                """
                SELECT usage_day, SUM(downloads) AS downloads,
                       SUM(bytes_sent) AS bytes_sent
                FROM daily_usage
                GROUP BY usage_day ORDER BY usage_day DESC LIMIT ?
                """,
                (min(max(days, 1), 31),),
            ).fetchall()
        return {
            "today": {
                "downloads": int(usage["downloads"]),
                "bytes_sent": int(usage["bytes_sent"]),
                "users": int(usage["users"]),
            },
            "outcomes": {
                "success": int(outcomes["success"]),
                "failure": int(outcomes["failure"]),
            },
            "failure_reasons": [dict(row) for row in reasons],
            "daily": [dict(row) for row in reversed(daily)],
            "top_users": [dict(row) for row in top_users],
        }

    def record_domain_health(
        self,
        domain: str,
        healthy: bool,
        latency_ms: int | None,
        status_code: int | None,
        error: str | None,
        final_domain: str | None = None,
        redirect_count: int | None = None,
    ):
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO domain_health(
                    domain, healthy, latency_ms, status_code, checked_at, error,
                    final_domain, redirect_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    healthy = excluded.healthy,
                    latency_ms = excluded.latency_ms,
                    status_code = excluded.status_code,
                    checked_at = excluded.checked_at,
                    error = excluded.error,
                    final_domain = excluded.final_domain,
                    redirect_count = excluded.redirect_count
                """,
                (
                    domain,
                    int(healthy),
                    latency_ms,
                    status_code,
                    time.time(),
                    _safe_text(error),
                    _safe_text(final_domain, 253),
                    max(0, int(redirect_count or 0)),
                ),
            )

    def domain_health(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM domain_health ORDER BY healthy DESC, latency_ms ASC"
            ).fetchall()
        return [
            {
                "domain": row["domain"],
                "healthy": bool(row["healthy"]),
                "latency_ms": row["latency_ms"],
                "status_code": row["status_code"],
                "checked_at": row["checked_at"],
                "error": row["error"],
                "final_domain": row["final_domain"],
                "redirect_count": row["redirect_count"],
            }
            for row in rows
        ]

    def prune(self, retention_days: int):
        cutoff = time.time() - max(1, retention_days) * 86400
        usage_cutoff = datetime.fromtimestamp(cutoff).astimezone().date().isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM rate_events WHERE created_at < ?", (cutoff,))
            connection.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
            connection.execute(
                "DELETE FROM daily_usage WHERE usage_day < ?", (usage_cutoff,)
            )

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        details = None
        if row["details_json"]:
            try:
                details = json.loads(row["details_json"])
            except ValueError:
                details = None
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "level": row["level"],
            "event_type": row["event_type"],
            "success": None if row["success"] is None else bool(row["success"]),
            "jm_id": row["jm_id"],
            "user_id": row["user_id"],
            "session": row["session"],
            "reason": row["reason"],
            "duration_ms": row["duration_ms"],
            "bytes_count": row["bytes_count"],
            "details": details,
        }


def _safe_text(value: Any, limit: int = MAX_EVENT_TEXT_LENGTH) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
