"""SQLite persistence and single-instance locking for the collector."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from data_model import (
    deduplicate_items,
    equipment_fingerprint,
    equipment_identity_key,
    sanitize_sensitive_data,
)

PROJECT_ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = 2
TRACKING_QUERY_KEYS = {"refer_sn", "_", "timestamp", "request_id", "trace_id"}
TERMINAL_AUTH_STATES = {
    "access_denied",
    "business_error",
    "login_required",
    "mobile_verification_required",
    "rate_limited",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_project_path(path: str | os.PathLike[str]) -> str:
    """Resolve relative runtime paths against the repository, never the process CWD."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return str(candidate.resolve())


def safe_account_key(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return cleaned or "account"


def canonical_target_url(url: str) -> str:
    """Remove request-level tracking parameters while retaining real filters."""
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
    ]
    query.sort()
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), "")
    )


def target_key_for_url(url: str) -> str:
    digest = hashlib.sha256(canonical_target_url(url).encode("utf-8")).hexdigest()
    return digest[:20]


class SQLiteStore:
    """Transactional, append-safe listing snapshot store."""

    def __init__(self, database_path: str = "data/cbg.sqlite3") -> None:
        self.database_path = resolve_project_path(database_path)
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit/rollback as needed and always release the Windows file handle."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            for attempt in range(1, 6):
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 5:
                        raise
                    time.sleep(0.1 * attempt)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info (
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS listings (
                    account_key TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    item_id TEXT NOT NULL DEFAULT '',
                    id_kind TEXT NOT NULL DEFAULT '',
                    identity_stable INTEGER NOT NULL DEFAULT 0,
                    name TEXT NOT NULL DEFAULT '',
                    price TEXT NOT NULL DEFAULT '',
                    level TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    content_hash TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'api',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_changed_at TEXT NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (account_key, target_key, identity)
                );

                CREATE INDEX IF NOT EXISTS idx_listings_last_changed
                    ON listings(last_changed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_listings_name
                    ON listings(name);

                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_key TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    cycle INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    scan_mode TEXT NOT NULL,
                    run_status TEXT NOT NULL DEFAULT 'finished',
                    success INTEGER NOT NULL,
                    auth_state TEXT NOT NULL DEFAULT '',
                    termination_reason TEXT NOT NULL DEFAULT '',
                    coverage_complete INTEGER NOT NULL DEFAULT 0,
                    pages_scanned INTEGER NOT NULL DEFAULT 0,
                    observed_count INTEGER NOT NULL DEFAULT 0,
                    inserted_count INTEGER NOT NULL DEFAULT 0,
                    updated_count INTEGER NOT NULL DEFAULT 0,
                    unchanged_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_scan_runs_scope
                    ON scan_runs(account_key, target_key, id DESC);

                CREATE TABLE IF NOT EXISTS checkpoints (
                    account_key TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    last_cycle INTEGER NOT NULL DEFAULT 0,
                    last_success_at TEXT,
                    last_full_scan_at TEXT,
                    last_full_scan_complete INTEGER NOT NULL DEFAULT 0,
                    last_termination_reason TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (account_key, target_key)
                );
                """
            )
            # Serialize the version check/migration across preview and collector processes.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
            if row is None:
                connection.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
            else:
                version = int(row["version"])
                if version == 1:
                    columns = {
                        item["name"]
                        for item in connection.execute("PRAGMA table_info(scan_runs)").fetchall()
                    }
                    if "cycle" not in columns:
                        connection.execute(
                            "ALTER TABLE scan_runs ADD COLUMN cycle INTEGER NOT NULL DEFAULT 0"
                        )
                    if "run_status" not in columns:
                        connection.execute(
                            "ALTER TABLE scan_runs ADD COLUMN "
                            "run_status TEXT NOT NULL DEFAULT 'finished'"
                        )
                    connection.execute("UPDATE schema_info SET version = ?", (SCHEMA_VERSION,))
                    version = SCHEMA_VERSION
                if version != SCHEMA_VERSION:
                    raise RuntimeError(
                        f"不支持的数据库版本 {version}，当前程序需要 {SCHEMA_VERSION}"
                    )

    def start_scan(
        self,
        account_key: str,
        target_key: str,
        target_url: str,
        cycle: int,
        scan_mode: str,
        started_at: str | None = None,
    ) -> int:
        """Persist a run boundary before the browser starts requesting scan data."""
        started_at = str(started_at or utc_now())
        cycle = max(1, int(cycle))
        target_url = canonical_target_url(target_url)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints (
                    account_key, target_key, target_url, last_cycle,
                    last_success_at, last_full_scan_at, last_full_scan_complete,
                    last_termination_reason, metadata_json
                ) VALUES (?, ?, ?, ?, NULL, NULL, 0, '', '{}')
                ON CONFLICT(account_key, target_key) DO UPDATE SET
                    target_url = excluded.target_url,
                    last_cycle = MAX(checkpoints.last_cycle, excluded.last_cycle)
                """,
                (account_key, target_key, target_url, cycle),
            )
            cursor = connection.execute(
                """
                INSERT INTO scan_runs (
                    account_key, target_key, target_url, cycle, started_at, finished_at,
                    scan_mode, run_status, success, auth_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', 0, 'running')
                """,
                (
                    account_key,
                    target_key,
                    target_url,
                    cycle,
                    started_at,
                    started_at,
                    str(scan_mode or "full"),
                ),
            )
            return int(cursor.lastrowid)

    def recover_interrupted_runs(
        self,
        account_key: str,
        target_key: str,
        recovered_at: str | None = None,
    ) -> int:
        """Finalize runs left open by a crash or forced service stop."""
        recovered_at = str(recovered_at or utc_now())
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE scan_runs
                SET finished_at = ?, run_status = 'interrupted', success = 0,
                    auth_state = 'interrupted',
                    termination_reason = 'process_interrupted',
                    error = '上次进程在本轮完成前退出'
                WHERE account_key = ? AND target_key = ? AND run_status = 'running'
                """,
                (recovered_at, account_key, target_key),
            )
            recovered = int(cursor.rowcount)
            if recovered:
                connection.execute(
                    """
                    UPDATE checkpoints
                    SET last_termination_reason = 'process_interrupted'
                    WHERE account_key = ? AND target_key = ?
                    """,
                    (account_key, target_key),
                )
            return recovered

    def record_manual_verification(
        self,
        account_key: str,
        target_key: str,
        target_url: str,
        verified_at: str | None = None,
    ) -> int:
        """Record that the manual helper verified this exact Profile and target."""
        verified_at = str(verified_at or utc_now())
        target_url = canonical_target_url(target_url)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints (
                    account_key, target_key, target_url, last_cycle,
                    last_success_at, last_full_scan_at, last_full_scan_complete,
                    last_termination_reason, metadata_json
                ) VALUES (?, ?, ?, 0, NULL, NULL, 0, '', '{}')
                ON CONFLICT(account_key, target_key) DO UPDATE SET
                    target_url = excluded.target_url
                """,
                (account_key, target_key, target_url),
            )
            row = connection.execute(
                """
                SELECT last_cycle FROM checkpoints
                WHERE account_key = ? AND target_key = ?
                """,
                (account_key, target_key),
            ).fetchone()
            cursor = connection.execute(
                """
                INSERT INTO scan_runs (
                    account_key, target_key, target_url, cycle, started_at, finished_at,
                    scan_mode, run_status, success, auth_state, termination_reason
                ) VALUES (?, ?, ?, ?, ?, ?, 'manual_verification', 'finished', 1,
                          'manual_verified', 'manual_verification_ok')
                """,
                (
                    account_key,
                    target_key,
                    target_url,
                    int(row["last_cycle"] or 0),
                    verified_at,
                    verified_at,
                ),
            )
            return int(cursor.lastrowid)

    def get_restart_delay(
        self,
        account_key: str,
        target_key: str,
        *,
        poll_interval_seconds: int,
        terminal_cooldown_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return remaining delay so process restarts cannot accelerate requests."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM scan_runs
                WHERE account_key = ? AND target_key = ?
                ORDER BY id DESC LIMIT 1
                """,
                (account_key, target_key),
            ).fetchone()
        if row is None:
            return {"delay_seconds": 0.0, "reason": "first_run", "last_run": None}

        last_run = dict(row)
        auth_state = str(last_run.get("auth_state") or "")
        terminal = auth_state in TERMINAL_AUTH_STATES
        configured_delay = max(
            0,
            int(terminal_cooldown_seconds if terminal else poll_interval_seconds),
        )
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        try:
            finished = datetime.fromisoformat(
                str(last_run.get("finished_at") or "").replace("Z", "+00:00")
            )
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            elapsed = max(0.0, (current - finished).total_seconds())
            remaining = max(0.0, configured_delay - elapsed)
            reason = "terminal_cooldown" if terminal else "poll_interval"
        except ValueError:
            remaining = float(configured_delay)
            reason = "invalid_last_run_time"
        return {
            "delay_seconds": remaining,
            "reason": reason,
            "last_run": last_run,
        }

    def record_result(
        self,
        account_key: str,
        target_key: str,
        target_url: str,
        result: dict[str, Any],
        *,
        run_id: int | None = None,
    ) -> dict[str, int]:
        finished_at = str(result.get("fetched_at") or utc_now())
        started_at = str(result.get("started_at") or finished_at)
        observed = deduplicate_items(
            result.get("observed_equip_list", result.get("equip_list", []))
            if result.get("success")
            else []
        )
        stats = {"inserted": 0, "updated": 0, "unchanged": 0}

        with self._connection() as connection:
            for item in observed:
                identity = equipment_identity_key(item)
                content_hash = equipment_fingerprint(item)
                existing = connection.execute(
                    """
                    SELECT content_hash
                    FROM listings
                    WHERE account_key = ? AND target_key = ? AND identity = ?
                    """,
                    (account_key, target_key, identity),
                ).fetchone()

                detail_json = json.dumps(
                    sanitize_sensitive_data(item.get("detail", {})),
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                values = (
                    str(item.get("id") or ""),
                    str(item.get("id_kind") or ""),
                    1 if item.get("identity_stable") else 0,
                    str(item.get("name") or ""),
                    str(item.get("price") or ""),
                    str(item.get("level") or ""),
                    detail_json,
                    content_hash,
                    str(item.get("source") or "api"),
                )

                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO listings (
                            account_key, target_key, identity, item_id, id_kind,
                            identity_stable, name, price, level, detail_json,
                            content_hash, source, first_seen_at, last_seen_at,
                            last_changed_at, seen_count
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            account_key,
                            target_key,
                            identity,
                            *values,
                            finished_at,
                            finished_at,
                            finished_at,
                        ),
                    )
                    stats["inserted"] += 1
                elif existing["content_hash"] != content_hash:
                    connection.execute(
                        """
                        UPDATE listings
                        SET item_id = ?, id_kind = ?, identity_stable = ?, name = ?,
                            price = ?, level = ?, detail_json = ?, content_hash = ?,
                            source = ?, last_seen_at = ?, last_changed_at = ?,
                            seen_count = seen_count + 1
                        WHERE account_key = ? AND target_key = ? AND identity = ?
                        """,
                        (*values, finished_at, finished_at, account_key, target_key, identity),
                    )
                    stats["updated"] += 1
                else:
                    connection.execute(
                        """
                        UPDATE listings
                        SET last_seen_at = ?, seen_count = seen_count + 1
                        WHERE account_key = ? AND target_key = ? AND identity = ?
                        """,
                        (finished_at, account_key, target_key, identity),
                    )
                    stats["unchanged"] += 1

            success = bool(result.get("success"))
            scan_mode = str(result.get("scan_mode") or "full")
            full_scan_at = finished_at if success and scan_mode == "full" else None
            metadata = {
                "captured_api_count": int(result.get("captured_api_count") or 0),
                "scan_complete": bool(result.get("scan_complete")),
            }
            connection.execute(
                """
                INSERT INTO checkpoints (
                    account_key, target_key, target_url, last_cycle,
                    last_success_at, last_full_scan_at, last_full_scan_complete,
                    last_termination_reason, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_key, target_key) DO UPDATE SET
                    target_url = excluded.target_url,
                    last_cycle = MAX(checkpoints.last_cycle, excluded.last_cycle),
                    last_success_at = COALESCE(excluded.last_success_at,
                                               checkpoints.last_success_at),
                    last_full_scan_at = COALESCE(excluded.last_full_scan_at,
                                                 checkpoints.last_full_scan_at),
                    last_full_scan_complete = CASE
                        WHEN excluded.last_full_scan_at IS NOT NULL
                        THEN excluded.last_full_scan_complete
                        ELSE checkpoints.last_full_scan_complete
                    END,
                    last_termination_reason = excluded.last_termination_reason,
                    metadata_json = excluded.metadata_json
                """,
                (
                    account_key,
                    target_key,
                    canonical_target_url(target_url),
                    int(result.get("cycle") or 0),
                    finished_at if success else None,
                    full_scan_at,
                    1 if full_scan_at and result.get("scan_complete") else 0,
                    str(result.get("termination_reason") or ""),
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            run_values = (
                int(result.get("cycle") or 0),
                started_at,
                finished_at,
                scan_mode,
                1 if success else 0,
                str(result.get("auth_state") or ""),
                str(result.get("termination_reason") or ""),
                1 if result.get("scan_complete") else 0,
                int(result.get("pages_scanned") or 0),
                len(observed),
                stats["inserted"],
                stats["updated"],
                stats["unchanged"],
                str(result.get("error") or ""),
            )
            if run_id is None:
                connection.execute(
                    """
                    INSERT INTO scan_runs (
                        account_key, target_key, target_url, cycle, started_at, finished_at,
                        scan_mode, run_status, success, auth_state, termination_reason,
                        coverage_complete, pages_scanned, observed_count,
                        inserted_count, updated_count, unchanged_count, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'finished', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_key,
                        target_key,
                        canonical_target_url(target_url),
                        *run_values,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE scan_runs
                    SET cycle = ?, started_at = ?, finished_at = ?, scan_mode = ?,
                        run_status = 'finished', success = ?, auth_state = ?,
                        termination_reason = ?, coverage_complete = ?, pages_scanned = ?,
                        observed_count = ?, inserted_count = ?, updated_count = ?,
                        unchanged_count = ?, error = ?
                    WHERE id = ? AND account_key = ? AND target_key = ?
                      AND run_status = 'running'
                    """,
                    (*run_values, int(run_id), account_key, target_key),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("采集运行记录不存在或已经结束")
        return stats

    def get_checkpoint(self, account_key: str, target_key: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM checkpoints
                WHERE account_key = ? AND target_key = ?
                """,
                (account_key, target_key),
            ).fetchone()
        return dict(row) if row else {}

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
        try:
            detail = json.loads(row["detail_json"])
        except (TypeError, ValueError):
            detail = {}
        return {
            "identity": row["identity"],
            "identity_stable": bool(row["identity_stable"]),
            "id": row["item_id"],
            "id_kind": row["id_kind"],
            "name": row["name"],
            "price": row["price"],
            "level": row["level"],
            "detail": detail,
            "source": row["source"],
            "content_hash": row["content_hash"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "last_changed_at": row["last_changed_at"],
            "seen_count": row["seen_count"],
        }

    def load_items(self, account_key: str, target_key: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM listings
                WHERE account_key = ? AND target_key = ?
                ORDER BY last_changed_at DESC, identity ASC
                """,
                (account_key, target_key),
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def list_items(
        self,
        *,
        account_key: str = "",
        target_key: str = "",
        query: str = "",
        limit: int = 100,
        offset: int = 0,
        sort: str = "last_changed_at",
        order: str = "desc",
    ) -> dict[str, Any]:
        allowed_sort = {
            "name",
            "price",
            "level",
            "first_seen_at",
            "last_seen_at",
            "last_changed_at",
            "seen_count",
        }
        sort = sort if sort in allowed_sort else "last_changed_at"
        order = "ASC" if order.lower() == "asc" else "DESC"
        limit = min(500, max(1, int(limit)))
        offset = max(0, int(offset))
        clauses: list[str] = []
        params: list[Any] = []
        if account_key:
            clauses.append("account_key = ?")
            params.append(account_key)
        if target_key:
            clauses.append("target_key = ?")
            params.append(target_key)
        if query:
            clauses.append("(name LIKE ? OR item_id LIKE ? OR identity LIKE ?)")
            pattern = f"%{query}%"
            params.extend([pattern, pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM listings {where}", params
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT * FROM listings {where}
                ORDER BY {sort} {order}, identity ASC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "items": [self._row_to_item(row) for row in rows],
        }

    def get_options(self) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT checkpoints.account_key,
                       checkpoints.target_key,
                       checkpoints.target_url,
                       COUNT(listings.identity) AS item_count
                FROM checkpoints
                LEFT JOIN listings
                  ON listings.account_key = checkpoints.account_key
                 AND listings.target_key = checkpoints.target_key
                GROUP BY checkpoints.account_key,
                         checkpoints.target_key,
                         checkpoints.target_url
                ORDER BY checkpoints.account_key, checkpoints.target_key
                """
            ).fetchall()
        return {
            "scopes": [dict(row) for row in rows],
        }

    def get_summary(self, account_key: str = "", target_key: str = "") -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if account_key:
            clauses.append("account_key = ?")
            params.append(account_key)
        if target_key:
            clauses.append("target_key = ?")
            params.append(target_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(identity_stable), 0) AS stable,
                       COALESCE(SUM(CASE WHEN identity_stable = 0 THEN 1 ELSE 0 END), 0)
                           AS fallback,
                       COALESCE(SUM(CASE WHEN last_changed_at >= ? THEN 1 ELSE 0 END), 0)
                           AS changed_24h,
                       MAX(last_seen_at) AS latest_seen_at
                FROM listings {where}
                """,
                [cutoff, *params],
            ).fetchone()

            run_clauses = list(clauses)
            run_where = f"WHERE {' AND '.join(run_clauses)}" if run_clauses else ""
            last_run = connection.execute(
                f"""
                SELECT * FROM scan_runs {run_where}
                ORDER BY id DESC LIMIT 1
                """,
                params,
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "stable": int(row["stable"] or 0),
            "fallback": int(row["fallback"] or 0),
            "changed_24h": int(row["changed_24h"] or 0),
            "latest_seen_at": row["latest_seen_at"],
            "last_run": dict(last_run) if last_run else None,
        }

    def list_runs(
        self,
        *,
        account_key: str = "",
        target_key: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if account_key:
            clauses.append("account_key = ?")
            params.append(account_key)
        if target_key:
            clauses.append("target_key = ?")
            params.append(target_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = min(200, max(1, int(limit)))
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM scan_runs {where} ORDER BY id DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [dict(row) for row in rows]


class InstanceLock:
    """Non-blocking cross-platform file lock scoped to one account worker."""

    def __init__(self, path: str) -> None:
        self.path = resolve_project_path(path)
        self._file = None

    def acquire(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"0")
            self._file.flush()
        self._file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            self._file = None
            raise RuntimeError("同一账号已有抓取进程正在运行") from exc

    def release(self) -> None:
        if self._file is None:
            return
        self._file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
