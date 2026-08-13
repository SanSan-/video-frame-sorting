"""Ограниченное SQLite-хранилище состояния фоновых веб-задач."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
DEFAULT_MAX_JOBS = 16
DEFAULT_MAX_EVENTS_PER_JOB = 2_000
DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_MAX_LOG_LINES = 250
RECOVERY_ERROR = "Задача была прервана перезапуском приложения."
_JOB_SNAPSHOT_LABEL = "Snapshot задачи"


class SQLiteJobStore:
    """Потокобезопасно сохраняет snapshots и события фоновых задач."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_jobs: int = DEFAULT_MAX_JOBS,
        max_events_per_job: int = DEFAULT_MAX_EVENTS_PER_JOB,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if max_jobs < 1 or max_events_per_job < 1:
            raise ValueError("Лимиты хранилища должны быть положительными.")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms не может быть отрицательным.")
        self.path = Path(path).expanduser()
        self.max_jobs = max_jobs
        self.max_events_per_job = max_events_per_job
        self.busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.path),
            timeout=busy_timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._configure()
            self._initialize_schema()
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    def save(
        self,
        snapshot: Mapping[str, Any],
        events: Iterable[tuple[int, Mapping[str, Any]]] = (),
    ) -> None:
        """Атомарно сохраняет snapshot и новые события."""
        normalized = _json_mapping(snapshot, _JOB_SNAPSHOT_LABEL)
        job_id = str(normalized.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("Snapshot задачи должен содержать job_id.")
        normalized.pop("events", None)
        normalized_events = _normalize_events(events)
        created_at = _number(normalized.get("created_at"), "created_at")
        updated_at = _number(normalized.get("updated_at"), "updated_at")
        completed_at = normalized.get("completed_at")
        if completed_at is not None:
            completed_at = _number(completed_at, "completed_at")
        terminal = bool(normalized.get("terminal"))
        active = bool(normalized.get("active")) and not terminal
        latest_event_id = max(
            _nonnegative_int(normalized.get("latest_event_id", 0), "latest_event_id"),
            max((event_id for event_id, _ in normalized_events), default=0),
        )
        with self._lock, self._transaction():
            row = self._connection.execute(
                "SELECT created_at, latest_event_id FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is not None:
                created_at = float(row["created_at"])
                latest_event_id = max(latest_event_id, int(row["latest_event_id"]))
            self._connection.execute(
                """
                INSERT INTO jobs(
                    job_id, status, active, terminal, created_at, updated_at,
                    completed_at, latest_event_id, snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status, active=excluded.active,
                    terminal=excluded.terminal, created_at=excluded.created_at,
                    updated_at=excluded.updated_at, completed_at=excluded.completed_at,
                    latest_event_id=excluded.latest_event_id,
                    snapshot_json=excluded.snapshot_json
                """,
                (
                    job_id,
                    str(normalized.get("status") or "queued"),
                    int(active),
                    int(terminal),
                    created_at,
                    updated_at,
                    completed_at,
                    latest_event_id,
                    _json_text(normalized),
                ),
            )
            self._upsert_events(job_id, normalized_events, updated_at)
            self._prune_events(job_id)
            self._prune_jobs()

    def load(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return self._load_row(row) if row is not None else None

    def list(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is not None and limit < 0:
            raise ValueError("limit не может быть отрицательным.")
        with self._lock:
            self._ensure_open()
            if limit == 0:
                return []
            query = "SELECT * FROM jobs ORDER BY updated_at DESC, job_id DESC"
            parameters: tuple[int, ...] = ()
            if limit is not None:
                query += " LIMIT ?"
                parameters = (limit,)
            rows = self._connection.execute(query, parameters).fetchall()
            return [self._load_row(row) for row in rows]

    def recover_interrupted(self, error: str = RECOVERY_ERROR) -> list[str]:
        """Переводит оставшиеся активными задачи в terminal-состояние."""
        message = str(error).strip()
        if not message:
            raise ValueError("Текст восстановления не может быть пустым.")
        recovered: list[str] = []
        with self._lock, self._transaction():
            rows = self._connection.execute(
                "SELECT * FROM jobs WHERE terminal = 0 ORDER BY created_at, job_id"
            ).fetchall()
            for row in rows:
                snapshot = _json_object(str(row["snapshot_json"]), _JOB_SNAPSHOT_LABEL)
                event_id = max(
                    int(row["latest_event_id"]), self._latest_stored_event_id(str(row["job_id"]))
                ) + 1
                completed_at = max(float(row["updated_at"]), _time_now())
                terminal_event = {
                    "type": "done",
                    "status": "interrupted",
                    "reason": "interrupted",
                    "active": False,
                    "terminal": True,
                    "progress": int(snapshot.get("progress") or 0),
                    "message": f"Ошибка: {message}",
                    "error": message,
                }
                logs = [
                    str(line)
                    for line in snapshot.get("logs", [])[-(DEFAULT_MAX_LOG_LINES - 1) :]
                ]
                if not logs or logs[-1] != terminal_event["message"]:
                    logs.append(str(terminal_event["message"]))
                snapshot.update(
                    status="interrupted",
                    active=False,
                    terminal=True,
                    message=terminal_event["message"],
                    error=message,
                    logs=logs,
                    updated_at=completed_at,
                    completed_at=completed_at,
                    latest_event_id=event_id,
                    terminal_event=terminal_event,
                )
                self._connection.execute(
                    """
                    UPDATE jobs SET status='interrupted', active=0, terminal=1,
                        updated_at=?, completed_at=?, latest_event_id=?, snapshot_json=?
                    WHERE job_id=?
                    """,
                    (
                        completed_at,
                        completed_at,
                        event_id,
                        _json_text(snapshot),
                        str(row["job_id"]),
                    ),
                )
                self._upsert_events(
                    str(row["job_id"]), [(event_id, terminal_event)], completed_at
                )
                self._prune_events(str(row["job_id"]))
                recovered.append(str(row["job_id"]))
            self._prune_jobs()
        return recovered

    def delete(self, job_id: str) -> bool:
        with self._lock, self._transaction():
            cursor = self._connection.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def _configure(self) -> None:
        self._connection.execute("PRAGMA encoding = 'UTF-8'")
        mode = str(self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        if mode.casefold() != "wal":
            raise RuntimeError("SQLite не смог включить WAL для хранилища задач.")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        self._connection.execute("PRAGMA synchronous = NORMAL")

    def _initialize_schema(self) -> None:
        with self._lock, self._transaction():
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS job_store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            version_row = self._connection.execute(
                "SELECT value FROM job_store_meta WHERE key='schema_version'"
            ).fetchone()
            if version_row is not None:
                try:
                    version = int(version_row["value"])
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Некорректная версия схемы SQLite.") from exc
                if version > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"Версия схемы SQLite {version} новее поддерживаемой {SCHEMA_VERSION}."
                    )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1)),
                    terminal INTEGER NOT NULL CHECK(terminal IN (0, 1)),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    latest_event_id INTEGER NOT NULL CHECK(latest_event_id >= 0),
                    snapshot_json TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_updated_idx ON jobs(updated_at DESC, job_id DESC)"
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    event_id INTEGER NOT NULL CHECK(event_id > 0),
                    created_at REAL NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY(job_id, event_id)
                )
                """
            )
            self._connection.execute(
                """
                INSERT INTO job_store_meta(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    def _load_row(self, row: sqlite3.Row) -> dict[str, Any]:
        snapshot = _json_object(str(row["snapshot_json"]), _JOB_SNAPSHOT_LABEL)
        snapshot.update(
            job_id=str(row["job_id"]),
            status=str(row["status"]),
            active=bool(row["active"]),
            terminal=bool(row["terminal"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            completed_at=(float(row["completed_at"]) if row["completed_at"] is not None else None),
            latest_event_id=int(row["latest_event_id"]),
            events=self._load_events(str(row["job_id"])),
        )
        return snapshot

    def _load_events(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT event_id, created_at, event_json FROM job_events WHERE job_id=? ORDER BY event_id",
            (job_id,),
        ).fetchall()
        return [
            {
                "id": int(row["event_id"]),
                "created_at": float(row["created_at"]),
                "event": _json_object(str(row["event_json"]), "Событие задачи"),
            }
            for row in rows
        ]

    def _upsert_events(
        self,
        job_id: str,
        events: list[tuple[int, dict[str, Any]]],
        created_at: float,
    ) -> None:
        self._connection.executemany(
            """
            INSERT INTO job_events(job_id, event_id, created_at, event_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id, event_id) DO UPDATE SET event_json=excluded.event_json
            """,
            [(job_id, event_id, created_at, _json_text(event)) for event_id, event in events],
        )

    def _latest_stored_event_id(self, job_id: str) -> int:
        row = self._connection.execute(
            "SELECT MAX(event_id) AS event_id FROM job_events WHERE job_id=?", (job_id,)
        ).fetchone()
        return int(row["event_id"] or 0)

    def _prune_events(self, job_id: str) -> None:
        self._connection.execute(
            """
            DELETE FROM job_events WHERE job_id=? AND event_id NOT IN (
                SELECT event_id FROM job_events WHERE job_id=?
                ORDER BY event_id DESC LIMIT ?
            )
            """,
            (job_id, job_id, self.max_events_per_job),
        )

    def _prune_jobs(self) -> None:
        excess = int(self._connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]) - self.max_jobs
        if excess <= 0:
            return
        rows = self._connection.execute(
            "SELECT job_id FROM jobs WHERE active=0 ORDER BY updated_at, job_id LIMIT ?",
            (excess,),
        ).fetchall()
        self._connection.executemany(
            "DELETE FROM jobs WHERE job_id=?", [(str(row["job_id"]),) for row in rows]
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._ensure_open()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SQLite-хранилище задач уже закрыто.")


def _normalize_events(
    events: Iterable[tuple[int, Mapping[str, Any]]],
) -> list[tuple[int, dict[str, Any]]]:
    normalized: list[tuple[int, dict[str, Any]]] = []
    seen: set[int] = set()
    for event_id, event in events:
        normalized_id = _nonnegative_int(event_id, "event_id")
        if normalized_id == 0 or normalized_id in seen:
            raise ValueError(f"Некорректный или повторный event_id: {normalized_id}")
        seen.add(normalized_id)
        normalized.append((normalized_id, _json_mapping(event, "Событие задачи")))
    return normalized


def _json_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} должен быть отображением.")
    return _json_object(_json_text(dict(value)), label)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_object(value: str, label: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} должен быть JSON-объектом.")
    return decoded


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{label} должен быть числом.")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} должен быть числом.") from exc


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{label} должен быть целым числом.")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} должен быть целым числом.") from exc
    if result < 0:
        raise ValueError(f"{label} не может быть отрицательным.")
    return result


def _time_now() -> float:
    import time

    return time.time()
