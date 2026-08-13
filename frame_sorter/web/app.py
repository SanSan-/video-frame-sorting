"""Защищённый локальный FastAPI-интерфейс без публикации изображений."""

from __future__ import annotations

import importlib
import ipaddress
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Generator, Iterable
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from frame_sorter import __version__
from frame_sorter.logging_utils import WEB_RUNTIME_LOGGING_ENV, setup_web_logging
from frame_sorter.web.job_store import SQLiteJobStore
from frame_sorter.web.picker import PickerError, pick_directory, pick_video as pick_video_path
from frame_sorter.web.workflow import (
    WorkflowArtifactError,
    inspect_folder_artifacts,
    require_rebuild_cache,
    require_undo_cache,
    resolve_cached_plan_for_rename,
)

ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "static"
PROJECT_ROOT = ROOT_DIR.parents[1]
DEFAULT_JOB_DB = PROJECT_ROOT / "resources" / "state" / "jobs.sqlite3"
SERVICE_MODULE = "frame_sorter.service"
TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})
MAX_JOBS = 16
LOG_HISTORY_LIMIT = 250
EVENT_HISTORY_LIMIT = 2_000
DIAGNOSTIC_HISTORY_LIMIT = 50
SSE_KEEP_ALIVE_SECONDS = 1.0
BAD_REQUEST_RESPONSE = {"description": "Некорректный каталог или путь к файлу."}
NOT_FOUND_RESPONSE = {"description": "Фоновая задача не найдена."}
CONFLICT_RESPONSE = {"description": "Операция конфликтует с текущим состоянием."}
PICKER_FAILURE_RESPONSE = {"description": "Системный диалог выбора завершился ошибкой."}


class FolderRequest(BaseModel):
    """Каталог и необязательный путь итогового CSV."""

    folder: str | None = None
    directory: str | None = None
    output_csv: str | None = None


class RenameRequest(FolderRequest):
    """Явно подтверждённое применение созданного CSV."""

    csv_path: str | None = None
    confirm: bool = False


class VideoPickRequest(BaseModel):
    """Режим системного выбора MP4 и начальный каталог."""

    kind: Literal["source", "output"]
    folder: str | None = None


class RebuildRequest(FolderRequest):
    """Параметры подтверждённой сборки нового MP4."""

    original_video: str | None = None
    output_video: str | None = None
    confirm: bool = False


class UndoRequest(FolderRequest):
    """Явно подтверждённое восстановление исходных имён из undo CSV."""

    confirm: bool = False


class ServiceAdapter:
    """Ленивая точка интеграции веб-слоя с доменным сервисом."""

    @staticmethod
    def analyze_folder(
        folder: Path,
        output_csv: Path | None,
        *,
        emit_event: Any,
    ) -> Any:
        service = importlib.import_module(SERVICE_MODULE)
        return service.analyze_folder(
            folder,
            output_csv=output_csv,
            emit_event=emit_event,
        )

    @staticmethod
    def preview_rename(folder: Path, csv_path: Path) -> Any:
        service = importlib.import_module(SERVICE_MODULE)
        return service.preview_rename(folder, csv_path)

    @staticmethod
    def apply_rename(folder: Path, csv_path: Path, *, emit_event: Any) -> Any:
        service = importlib.import_module(SERVICE_MODULE)
        return service.apply_rename(folder, csv_path, emit_event=emit_event)

    @staticmethod
    def rebuild_video(
        folder: Path,
        original_video: Path | None,
        output_video: Path | None,
        *,
        emit_event: Any,
    ) -> Any:
        service = importlib.import_module(SERVICE_MODULE)
        return service.rebuild_video(
            folder,
            original_video=original_video,
            output_video=output_video,
            emit_event=emit_event,
        )


service_api = ServiceAdapter()
logger = logging.getLogger(__name__)
_state_lock = threading.RLock()
_picker_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_job_conditions: dict[str, threading.Condition] = {}
_active_job_id: str | None = None
_last_job_id: str | None = None
_workers: dict[str, threading.Thread] = {}
_job_store: SQLiteJobStore | None = None


@asynccontextmanager
async def _application_lifespan(_application: FastAPI) -> Any:
    runtime = os.environ.get(WEB_RUNTIME_LOGGING_ENV) == "1"
    if runtime:
        setup_web_logging()
        _open_runtime_store()
    logger.info("Локальная веб-сессия запущена, PID=%s.", os.getpid())
    try:
        yield
    finally:
        if runtime:
            _wait_for_workers()
            _close_runtime_store()
        logger.info("Локальная веб-сессия остановлена, PID=%s.", os.getpid())


app = FastAPI(
    title="Video Frame Sorter",
    version=__version__,
    lifespan=_application_lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")


@app.middleware("http")
async def protect_local_access(request: Request, call_next: Any) -> Any:
    """Разрешает только loopback Host, а POST — только точному local Origin."""
    client_host = request.client.host if request.client is not None else ""
    if not _trusted_local_request(request, client_host):
        return JSONResponse(
            content={"detail": "Локальный API доступен только через loopback."},
            status_code=403,
        )
    if request.method == "POST" and not _trusted_origin(request, client_host):
        return JSONResponse(
            content={"detail": "Изменение состояния разрешено только локальному origin."},
            status_code=403,
        )
    return await call_next(request)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "ocr-video-frame-sorting",
        "version": __version__,
    }


@app.post(
    "/api/pick",
    responses={409: CONFLICT_RESPONSE, 500: PICKER_FAILURE_RESPONSE},
)
def pick() -> dict[str, Any]:
    if not _picker_lock.acquire(blocking=False):
        raise HTTPException(409, "Системный диалог выбора уже открыт.")
    try:
        selected = pick_directory()
    except PickerError as exc:
        raise HTTPException(500, str(exc)) from exc
    finally:
        _picker_lock.release()
    if selected is None:
        return {"folder": None, "workflow": None}
    return {
        "folder": str(selected),
        "workflow": inspect_folder_artifacts(selected),
    }


@app.post(
    "/api/refresh",
    responses={400: BAD_REQUEST_RESPONSE},
)
def refresh(payload: FolderRequest) -> dict[str, Any]:
    """Повторно классифицирует этапы по артефактам выбранного каталога."""
    folder = _folder_from(payload)
    return {
        "folder": str(folder),
        "workflow": inspect_folder_artifacts(folder),
    }


@app.post(
    "/api/pick-video",
    responses={409: CONFLICT_RESPONSE, 500: PICKER_FAILURE_RESPONSE},
)
def pick_video(payload: VideoPickRequest) -> dict[str, str | None]:
    if not _picker_lock.acquire(blocking=False):
        raise HTTPException(409, "Системный диалог выбора уже открыт.")
    try:
        selected = pick_video_path(
            payload.kind,
            initial_directory=_picker_initial_directory(payload.folder),
        )
    except PickerError as exc:
        raise HTTPException(500, str(exc)) from exc
    finally:
        _picker_lock.release()
    value = str(selected) if selected is not None else None
    return {
        "kind": payload.kind,
        "path": value,
        "original_video": value if payload.kind == "source" else None,
        "output_video": value if payload.kind == "output" else None,
    }


@app.post(
    "/api/analyze",
    status_code=202,
    responses={400: BAD_REQUEST_RESPONSE, 409: CONFLICT_RESPONSE},
)
def analyze(payload: FolderRequest) -> dict[str, Any]:
    folder = _folder_from(payload)
    output_csv = _optional_path(payload.output_csv)
    return _start_job(
        "analysis",
        folder,
        _run_analysis,
        output_csv,
    )


@app.post(
    "/api/rename",
    status_code=202,
    responses={400: BAD_REQUEST_RESPONSE, 409: CONFLICT_RESPONSE},
)
def rename(payload: RenameRequest) -> dict[str, Any]:
    if payload.confirm is not True:
        raise HTTPException(400, "Для переименования требуется явное подтверждение.")
    folder = _folder_from(payload)
    requested_csv = _optional_path(payload.csv_path)
    try:
        csv_path = resolve_cached_plan_for_rename(folder, requested_csv)
    except WorkflowArtifactError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _start_job("rename", folder, _run_rename, csv_path)


@app.post(
    "/api/undo",
    status_code=202,
    responses={400: BAD_REQUEST_RESPONSE, 409: CONFLICT_RESPONSE},
)
def undo(payload: UndoRequest) -> dict[str, Any]:
    """Восстанавливает исходные имена по актуальному дисковому undo CSV."""
    if payload.confirm is not True:
        raise HTTPException(400, "Для отмены переименования требуется явное подтверждение.")
    folder = _folder_from(payload)
    try:
        undo_csv = require_undo_cache(folder)
    except WorkflowArtifactError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _start_job("undo", folder, _run_undo, undo_csv)


@app.post(
    "/api/rebuild",
    status_code=202,
    responses={400: BAD_REQUEST_RESPONSE, 409: CONFLICT_RESPONSE},
)
def rebuild(payload: RebuildRequest) -> dict[str, Any]:
    if payload.confirm is not True:
        raise HTTPException(400, "Для сборки MP4 требуется явное подтверждение.")
    folder = _folder_from(payload)
    try:
        undo_csv = require_rebuild_cache(folder)
    except WorkflowArtifactError as exc:
        raise HTTPException(409, str(exc)) from exc
    original_video = _optional_source_video(payload.original_video)
    output_video = _optional_output_video(payload.output_video)
    if output_video is not None and output_video.exists():
        raise HTTPException(409, f"Выходной файл уже существует: {output_video}")
    return _start_job(
        "rebuild",
        folder,
        _run_rebuild,
        undo_csv,
        original_video,
        output_video,
    )


@app.get("/api/active")
@app.get("/api/active-job")
def active_job() -> dict[str, Any]:
    with _state_lock:
        job_id = _active_job_id or _last_job_id
        if job_id is None:
            return {"active": False, "terminal": False, "rebuild_ready": False}
        return _job_snapshot(_jobs[job_id])


@app.get("/api/jobs")
def list_jobs(
    limit: Annotated[int, Query(ge=1, le=100)] = MAX_JOBS,
) -> dict[str, Any]:
    with _state_lock:
        tasks = sorted(
            _jobs.values(),
            key=lambda task: (float(task["updated_at"]), str(task["job_id"])),
            reverse=True,
        )[:limit]
        return {"jobs": [_job_summary(task) for task in tasks]}


@app.get("/api/job/{job_id}", responses={404: NOT_FOUND_RESPONSE})
def job(job_id: str) -> dict[str, Any]:
    with _state_lock:
        return _job_snapshot(_get_job_locked(job_id))


@app.get("/api/jobs/{job_id}", responses={404: NOT_FOUND_RESPONSE})
def persisted_job(job_id: str) -> dict[str, Any]:
    with _state_lock:
        selected = _get_job_locked(job_id)
        snapshot = _job_snapshot(selected)
        snapshot["events"] = [
            {"id": event_id, "event": dict(event)}
            for event_id, event in selected["events"]
        ]
        return snapshot


@app.get("/api/stream/{job_id}", responses={404: NOT_FOUND_RESPONSE})
def stream(
    job_id: str,
    cursor: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    with _state_lock:
        _get_job_locked(job_id)
    events = _iter_sse(job_id, max(cursor, _parse_event_id(last_event_id)))
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _start_job(
    kind: str,
    folder: Path,
    target: Any,
    *arguments: Path | None,
) -> dict[str, Any]:
    global _active_job_id, _last_job_id
    with _state_lock:
        if _active_job_id is not None:
            raise HTTPException(409, "Другая задача уже выполняется.")
        _prune_jobs_locked()
        job_id = uuid.uuid4().hex
        now = time.time()
        task = {
            "job_id": job_id,
            "kind": kind,
            "folder": str(folder),
            "status": "queued",
            "active": True,
            "terminal": False,
            "progress": 0,
            "message": "Задача поставлена в очередь.",
            "logs": deque(maxlen=LOG_HISTORY_LIMIT),
            "events": deque(maxlen=EVENT_HISTORY_LIMIT),
            "diagnostics": [],
            "latest_event_id": 0,
            "terminal_event": None,
            "persistence_error": None,
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        task["logs"].append(task["message"])
        _jobs[job_id] = task
        _job_conditions[job_id] = threading.Condition(_state_lock)
        _active_job_id = job_id
        _last_job_id = job_id
        try:
            _record_events_locked(
                task,
                [
                    _job_event(task),
                    {"type": "log", "message": task["message"]},
                ],
                strict=True,
            )
        except Exception:
            _active_job_id = None
            _jobs.pop(job_id, None)
            _job_conditions.pop(job_id, None)
            raise
        worker = threading.Thread(
            target=target,
            args=(job_id, folder, *arguments),
            name=f"frame-sorter-{kind}-{job_id[:8]}",
            daemon=False,
        )
        _workers[job_id] = worker
        try:
            worker.start()
        except Exception:
            _active_job_id = None
            _workers.pop(job_id, None)
            _jobs.pop(job_id, None)
            _job_conditions.pop(job_id, None)
            if _job_store is not None:
                _job_store.delete(job_id)
            raise
        logger.info("Задача %s поставлена в очередь: операция=%s.", job_id, kind)
        return _job_snapshot(task)


def _run_analysis(job_id: str, folder: Path, output_csv: Path | None) -> None:
    try:
        _update_job(job_id, status="running", progress=1, message="Анализ начат.")
        result = service_api.analyze_folder(
            folder,
            output_csv,
            emit_event=lambda event: _apply_event(job_id, event),
        )
        payload = _result_dict(result)
        _finish_job(job_id, payload, "CSV сортировки создан.")
    except Exception as exc:
        _fail_job(job_id, exc)


def _run_rename(job_id: str, folder: Path, csv_path: Path | None) -> None:
    try:
        if csv_path is None:
            raise ValueError("Не указан CSV сортировки.")
        _update_job(job_id, status="running", progress=1, message="План повторно проверяется.")
        preview = service_api.preview_rename(folder, csv_path)
        _append_log(job_id, f"Проверено переименований: {_result_dict(preview).get('rename_count', 0)}.")
        result = service_api.apply_rename(
            folder,
            csv_path,
            emit_event=lambda event: _apply_event(job_id, event),
        )
        _finish_job(job_id, _result_dict(result), "Переименование завершено.")
    except Exception as exc:
        _fail_job(job_id, exc)


def _run_undo(job_id: str, folder: Path, undo_csv: Path | None) -> None:
    try:
        if undo_csv is None:
            raise ValueError("Не указан обратный CSV.")
        _update_job(
            job_id,
            status="running",
            progress=1,
            message="Обратный CSV повторно проверяется.",
        )
        preview = service_api.preview_rename(folder, undo_csv)
        _append_log(
            job_id,
            "Проверено восстановлений имён: "
            f"{_result_dict(preview).get('rename_count', 0)}.",
        )
        result = service_api.apply_rename(
            folder,
            undo_csv,
            emit_event=lambda event: _apply_event(job_id, event),
        )
        payload = _result_dict(result)
        reverse_csv = payload.pop("undo_csv_path", None)
        if reverse_csv is not None:
            payload["redo_csv_path"] = reverse_csv
        _finish_job(job_id, payload, "Исходные имена восстановлены.")
    except Exception as exc:
        _fail_job(job_id, exc)


def _run_rebuild(
    job_id: str,
    folder: Path,
    undo_csv: Path | None,
    original_video: Path | None,
    output_video: Path | None,
) -> None:
    try:
        if undo_csv is None:
            raise ValueError("Не указан подтверждающий undo CSV.")
        if output_video is not None and output_video.exists():
            raise FileExistsError(f"Выходной файл уже существует: {output_video}")
        _update_job(
            job_id,
            status="running",
            progress=1,
            message="Артефакты переименования проверяются.",
        )
        preview = service_api.preview_rename(folder, undo_csv)
        _append_log(
            job_id,
            "Подтверждено отсортированных кадров: "
            f"{_result_dict(preview).get('frame_count', 0)}.",
        )
        _update_job(job_id, progress=2, message="Сборка MP4 начата.")
        result = service_api.rebuild_video(
            folder,
            original_video,
            output_video,
            emit_event=lambda event: _apply_event(job_id, event),
        )
        _finish_job(job_id, _result_dict(result), "MP4 создан.")
    except Exception as exc:
        _fail_job(job_id, exc)


def _apply_event(job_id: str, event: Any) -> None:
    if not isinstance(event, Mapping):
        return
    normalized = json.loads(
        json.dumps(dict(event), ensure_ascii=False, default=str)
    )
    normalized["type"] = str(normalized.get("type") or "progress")
    phase = str(normalized.get("phase") or "").strip()
    message = str(event.get("message") or _phase_message(phase)).strip()
    progress = event.get("progress")
    changes: dict[str, Any] = {}
    if phase:
        changes["phase"] = phase
    for key in ("processed", "total"):
        if isinstance(normalized.get(key), (int, float)):
            changes[key] = normalized[key]
    if isinstance(progress, (int, float)):
        changes["progress"] = max(1, min(99, int(progress)))
    elif isinstance(event.get("processed"), (int, float)) and isinstance(
        event.get("total"), (int, float)
    ):
        changes["progress"] = _phase_progress(
            phase,
            float(event["processed"]),
            float(event["total"]),
        )
    if message:
        changes["message"] = message
        normalized["message"] = message
    changes["_source_event"] = normalized
    if normalized.get("type") == "media_probe":
        changes["_diagnostic"] = normalized
    if changes:
        _update_job(job_id, **changes)


def _phase_message(phase: str) -> str:
    return {
        "loading": "Кадры читаются и уменьшаются для сравнения.",
        "analyzing": "Ищутся дёргающие возвраты к прошлым кадрам.",
        "prepared": "Транзакция переименования подготовлена.",
        "phase1": "Исходные имена заменяются временными.",
        "phase2": "Назначаются итоговые имена.",
        "verifying": "Результат переименования проверяется.",
        "probing": "Проверяются кадры и исходная звуковая дорожка.",
        "encoding": "Кадры кодируются в MP4.",
        "muxing": "Звуковая дорожка добавляется в MP4.",
        "rebuilding": "FFmpeg пересобирает видео.",
        "verifying_video": "Созданный MP4 проверяется.",
        "completed": "Операция завершается.",
    }.get(phase, "")


def _phase_progress(phase: str, processed: float, total: float) -> int:
    ratio = max(0.0, min(1.0, processed / total)) if total > 0 else 0.0
    start, span = {
        "loading": (2, 43),
        "analyzing": (45, 50),
        "prepared": (2, 3),
        "phase1": (5, 40),
        "phase2": (45, 45),
        "verifying": (90, 5),
        "probing": (2, 8),
        "encoding": (10, 75),
        "muxing": (85, 10),
        "rebuilding": (10, 75),
        "verifying_video": (85, 10),
        "completed": (95, 4),
    }.get(phase, (1, 98))
    return max(1, min(99, int(start + span * ratio)))


def _finish_job(job_id: str, result: dict[str, Any], message: str) -> None:
    _update_job(
        job_id,
        _event_type="done",
        status="completed",
        active=False,
        terminal=True,
        progress=100,
        message=message,
        result=result,
    )
    _release_job(job_id)


def _fail_job(job_id: str, error: Exception) -> None:
    message = str(error).strip() or error.__class__.__name__
    logger.exception("Задача %s завершилась с ошибкой: %s", job_id, message)
    _update_job(
        job_id,
        _event_type="done",
        status="failed",
        active=False,
        terminal=True,
        message=f"Ошибка: {message}",
        error=message,
    )
    _release_job(job_id)


def _release_job(job_id: str) -> None:
    global _active_job_id
    with _state_lock:
        if _active_job_id == job_id:
            _active_job_id = None
        _workers.pop(job_id, None)


def _update_job(job_id: str, **changes: Any) -> None:
    global _active_job_id
    event_type = str(changes.pop("_event_type", "job"))
    source_event = changes.pop("_source_event", None)
    diagnostic = changes.pop("_diagnostic", None)
    with _state_lock:
        task = _jobs[job_id]
        message = changes.get("message")
        task.update(changes)
        task["updated_at"] = time.time()
        if task.get("terminal") and task.get("completed_at") is None:
            task["completed_at"] = task["updated_at"]
        if isinstance(diagnostic, Mapping):
            task["diagnostics"].append(dict(diagnostic))
            del task["diagnostics"][:-DIAGNOSTIC_HISTORY_LIMIT]
        events: list[dict[str, Any]] = []
        if message and (not task["logs"] or task["logs"][-1] != message):
            task["logs"].append(str(message))
            events.append({"type": "log", "message": str(message)})
            logger.info("Задача %s: %s", job_id, message)
        if isinstance(source_event, Mapping):
            events.append(dict(source_event))
        event = _job_event(task, event_type=event_type)
        if event_type == "done":
            task["terminal_event"] = event
        events.append(event)
        _record_events_locked(task, events)
        if task.get("terminal"):
            if _active_job_id == job_id:
                _active_job_id = None
            _workers.pop(job_id, None)


def _append_log(job_id: str, message: str) -> None:
    with _state_lock:
        task = _jobs[job_id]
        task["logs"].append(message)
        task["updated_at"] = time.time()
        _record_events_locked(task, [{"type": "log", "message": message}])
        logger.info("Задача %s: %s", job_id, message)


def _job_snapshot(task: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = {
        key: value
        for key, value in task.items()
        if key not in {"events"}
    }
    snapshot["logs"] = list(task["logs"])
    snapshot["diagnostics"] = [dict(item) for item in task.get("diagnostics", [])]
    snapshot["result"] = dict(task["result"]) if isinstance(task.get("result"), Mapping) else task.get("result")
    snapshot["rebuild_ready"] = (
        False
        if bool(task.get("active"))
        else _rebuild_ready_for(Path(str(task["folder"])))
    )
    return snapshot


def _job_summary(task: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = _job_snapshot(task)
    keys = (
        "job_id",
        "kind",
        "folder",
        "status",
        "active",
        "terminal",
        "progress",
        "message",
        "created_at",
        "updated_at",
        "completed_at",
        "latest_event_id",
    )
    return {key: snapshot.get(key) for key in keys}


def _job_event(task: Mapping[str, Any], *, event_type: str = "job") -> dict[str, Any]:
    event = {
        "type": event_type,
        "job_id": task["job_id"],
        "kind": task["kind"],
        "status": task["status"],
        "active": bool(task["active"]),
        "terminal": bool(task["terminal"]),
        "progress": int(task["progress"]),
        "message": str(task["message"]),
    }
    if event_type == "done":
        event["result"] = task.get("result")
        event["error"] = task.get("error")
    return event


def _record_events_locked(
    task: dict[str, Any],
    events: Iterable[Mapping[str, Any]],
    *,
    strict: bool = False,
) -> None:
    recorded: list[tuple[int, dict[str, Any]]] = []
    for event in events:
        normalized = json.loads(
            json.dumps(dict(event), ensure_ascii=False, default=str)
        )
        event_id = int(task["latest_event_id"]) + 1
        task["latest_event_id"] = event_id
        task["events"].append((event_id, normalized))
        recorded.append((event_id, normalized))
    store = _job_store
    if store is not None:
        try:
            store.save(_job_snapshot(task), recorded)
        except Exception as exc:
            task["persistence_error"] = str(exc) or exc.__class__.__name__
            logger.exception(
                "Не удалось сохранить состояние задачи %s.", task["job_id"]
            )
            if strict:
                raise
    condition = _job_conditions.get(str(task["job_id"]))
    if condition is not None:
        condition.notify_all()


def _get_job_locked(job_id: str) -> dict[str, Any]:
    selected = _jobs.get(job_id)
    if selected is None and _job_store is not None:
        persisted = _job_store.load(job_id)
        if persisted is not None:
            selected = _hydrate_job_locked(persisted)
    if selected is None:
        raise HTTPException(404, "Задача не найдена.")
    return selected


def _hydrate_job_locked(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    task = dict(snapshot)
    stored_events = task.pop("events", [])
    task["logs"] = deque(
        (str(line) for line in task.get("logs", [])), maxlen=LOG_HISTORY_LIMIT
    )
    task["events"] = deque(
        (
            (int(entry["id"]), dict(entry["event"]))
            for entry in stored_events
            if isinstance(entry, Mapping)
            and isinstance(entry.get("event"), Mapping)
        ),
        maxlen=EVENT_HISTORY_LIMIT,
    )
    task.setdefault("latest_event_id", 0)
    task.setdefault("diagnostics", [])
    task.setdefault("completed_at", None)
    task.setdefault("terminal_event", None)
    task.setdefault("persistence_error", None)
    _jobs[str(task["job_id"])] = task
    _job_conditions[str(task["job_id"])] = threading.Condition(_state_lock)
    return task


def _prune_jobs_locked() -> None:
    global _last_job_id
    if len(_jobs) < MAX_JOBS:
        return
    terminal = sorted(
        (task for task in _jobs.values() if task.get("terminal")),
        key=lambda task: (float(task["updated_at"]), str(task["job_id"])),
    )
    while len(_jobs) >= MAX_JOBS and terminal:
        job_id = str(terminal.pop(0)["job_id"])
        _jobs.pop(job_id, None)
        _job_conditions.pop(job_id, None)
    if _last_job_id not in _jobs:
        completed = [task for task in _jobs.values() if task.get("terminal")]
        _last_job_id = (
            str(max(completed, key=lambda task: float(task["updated_at"]))["job_id"])
            if completed
            else None
        )


def _iter_sse(job_id: str, after_event_id: int) -> Iterable[str]:
    cursor = max(0, after_event_id)
    while True:
        task, events, terminal, terminal_event, terminal_id, condition = (
            _read_sse_state(job_id, cursor)
        )
        cursor, terminal_seen = yield from _replay_sse_events(events, cursor)
        if terminal_seen:
            return
        if events:
            continue
        if terminal:
            yield from _replay_terminal_sse_event(
                terminal_event,
                terminal_id,
                cursor,
            )
            return
        if not _wait_for_sse_update(condition, task, cursor):
            yield ": keep-alive\n\n"


def _read_sse_state(
    job_id: str,
    cursor: int,
) -> tuple[
    dict[str, Any],
    list[tuple[int, dict[str, Any]]],
    bool,
    Any,
    int | None,
    threading.Condition,
]:
    with _state_lock:
        task = _get_job_locked(job_id)
        events = [
            (event_id, dict(event))
            for event_id, event in task["events"]
            if event_id > cursor
        ]
        terminal = bool(task["terminal"])
        terminal_event = task.get("terminal_event")
        terminal_id = int(task["latest_event_id"]) if terminal else None
        condition = _job_conditions[job_id]
    return task, events, terminal, terminal_event, terminal_id, condition


def _replay_sse_events(
    events: Iterable[tuple[int, Mapping[str, Any]]],
    cursor: int,
) -> Generator[str, None, tuple[int, bool]]:
    for event_id, event in events:
        cursor = event_id
        yield _format_sse(event_id, event)
        if event.get("type") == "done":
            return cursor, True
    return cursor, False


def _replay_terminal_sse_event(
    terminal_event: Any,
    terminal_id: int | None,
    cursor: int,
) -> Iterable[str]:
    if (
        isinstance(terminal_event, Mapping)
        and terminal_id is not None
        and terminal_id > cursor
    ):
        yield _format_sse(terminal_id, dict(terminal_event))


def _wait_for_sse_update(
    condition: threading.Condition,
    task: Mapping[str, Any],
    cursor: int,
) -> bool:
    with condition:
        if _has_sse_update(task, cursor):
            return True
        condition.wait(timeout=SSE_KEEP_ALIVE_SECONDS)
        return _has_sse_update(task, cursor)


def _has_sse_update(task: Mapping[str, Any], cursor: int) -> bool:
    return any(
        event_id > cursor for event_id, _event in task["events"]
    ) or bool(task["terminal"])


def _format_sse(event_id: int, event: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\ndata: {payload}\n\n"


def _parse_event_id(value: str | None) -> int:
    if not value:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        return 0


def _job_database_path() -> Path:
    configured = os.environ.get("WEB_JOB_DB", "").strip()
    path = Path(configured).expanduser() if configured else DEFAULT_JOB_DB
    return path if path.is_absolute() else PROJECT_ROOT / path


def _open_runtime_store() -> None:
    global _job_store, _active_job_id, _last_job_id
    store = SQLiteJobStore(_job_database_path())
    try:
        recovered = store.recover_interrupted()
        snapshots = store.list()
        with _state_lock:
            if _active_job_id is not None:
                raise RuntimeError(
                    "Нельзя подключить SQLite во время активной задачи в памяти."
                )
            _job_store = store
            _jobs.clear()
            _job_conditions.clear()
            _workers.clear()
            _active_job_id = None
            _last_job_id = None
            for snapshot in reversed(snapshots):
                _hydrate_job_locked(snapshot)
            if snapshots:
                _last_job_id = str(snapshots[0]["job_id"])
        if recovered:
            logger.warning(
                "После перезапуска отмечено прерванных задач: %d.", len(recovered)
            )
        logger.info("SQLite-хранилище задач открыто: %s", store.path)
    except BaseException:
        with _state_lock:
            if _job_store is store:
                _job_store = None
        store.close()
        raise


def _close_runtime_store() -> None:
    global _job_store
    with _state_lock:
        store = _job_store
        _job_store = None
    if store is not None:
        store.close()
        logger.info("SQLite-хранилище задач закрыто.")


def _wait_for_workers() -> None:
    with _state_lock:
        workers = list(_workers.values())
    if workers:
        logger.warning(
            "Остановка веб-сервиса ожидает завершения активных задач: %d.",
            len(workers),
        )
    for worker in workers:
        worker.join()


def _rebuild_ready_for(folder: Path) -> bool:
    try:
        return inspect_folder_artifacts(folder)["actions"]["rebuild"] is True
    except (OSError, WorkflowArtifactError):
        return False


def _result_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "to_dict"):
        payload = result.to_dict()
    elif is_dataclass(result):
        payload = asdict(result)
    elif isinstance(result, Mapping):
        payload = dict(result)
    else:
        raise TypeError("Доменный сервис вернул неподдерживаемый результат.")
    return {key: str(value) if isinstance(value, Path) else value for key, value in payload.items()}


def _folder_from(payload: FolderRequest) -> Path:
    raw = (payload.folder or payload.directory or "").strip()
    if not raw:
        raise HTTPException(400, "Выберите каталог с кадрами.")
    try:
        folder = Path(raw).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HTTPException(400, f"Каталог не найден: {raw}") from exc
    if not folder.is_dir():
        raise HTTPException(400, f"Каталог не найден: {raw}")
    return folder


def _optional_path(raw: str | None) -> Path | None:
    return Path(raw).expanduser().resolve(strict=False) if raw and raw.strip() else None


def _optional_source_video(raw: str | None) -> Path | None:
    if not raw or not raw.strip():
        return None
    try:
        path = Path(raw).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HTTPException(400, f"Исходный MP4 не найден: {raw}") from exc
    if not path.is_file() or path.suffix.casefold() != ".mp4":
        raise HTTPException(400, f"Исходный MP4 не найден: {raw}")
    return path


def _optional_output_video(raw: str | None) -> Path | None:
    if not raw or not raw.strip():
        return None
    path = Path(raw).expanduser().resolve(strict=False)
    if path.suffix.casefold() != ".mp4":
        raise HTTPException(400, "Выходной файл должен иметь расширение .mp4.")
    if not path.parent.is_dir():
        raise HTTPException(400, f"Каталог выходного файла не найден: {path.parent}")
    return path


def _picker_initial_directory(raw: str | None) -> Path | None:
    if not raw or not raw.strip():
        return None
    try:
        path = Path(raw).expanduser().resolve(strict=True)
    except OSError:
        return None
    return path if path.is_dir() else path.parent


def _path_key(path: Path) -> str:
    return str(path.resolve(strict=False)).casefold()


def _trusted_local_request(request: Request, client_host: str) -> bool:
    allow_testserver = client_host.casefold() == "testclient"
    if not allow_testserver and _loopback(client_host, False) is None:
        return False
    return _authority(request.headers.get("host", ""), request.url.scheme, allow_testserver) is not None


def _trusted_origin(request: Request, client_host: str) -> bool:
    allow_testserver = client_host.casefold() == "testclient"
    host = _authority(request.headers.get("host", ""), request.url.scheme, allow_testserver)
    origin = request.headers.get("origin", "")
    if host is None or not origin:
        return False
    try:
        parsed = urlsplit(origin)
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
            return False
    except ValueError:
        return False
    return _authority(parsed.netloc, parsed.scheme, allow_testserver) == host


def _authority(value: str, scheme: str, allow_testserver: bool) -> tuple[str, str, int] | None:
    if scheme not in {"http", "https"} or not value or "@" in value:
        return None
    try:
        parsed = urlsplit(f"//{value}")
        if parsed.path or parsed.query or parsed.fragment:
            return None
        host = _loopback(parsed.hostname or "", allow_testserver)
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return (scheme, host, port) if host is not None else None


def _loopback(value: str, allow_testserver: bool) -> str | None:
    host = value.strip().casefold()
    if allow_testserver and host == "testserver":
        return host
    if host == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    return address.compressed if address.is_loopback else None
