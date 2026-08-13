from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frame_sorter.web import app as web_app
from frame_sorter.web.job_store import SQLiteJobStore


ORIGIN = {"Origin": "http://testserver"}


class FakeService:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.block = False
        self.renamed = False
        self.rebuild_calls: list[tuple[Path, Path | None, Path | None]] = []

    def analyze_folder(self, folder, output_csv, *, emit_event):
        emit_event({"progress": 40, "message": "Кадры сравниваются."})
        if self.block:
            assert self.release.wait(2)
        csv_path = output_csv or folder / "frame-sort-plan.csv"
        csv_path.write_text(
            "position,source_filename\n",
            encoding="utf-8",
            newline="",
        )
        csv_path.with_suffix(".meta.json").write_text(
            "{}\n",
            encoding="utf-8",
            newline="",
        )
        return {"folder": str(folder), "csv_path": str(csv_path), "frame_count": 4}

    def preview_rename(self, folder, csv_path):
        return {"folder": str(folder), "csv_path": str(csv_path), "rename_count": 2}

    def apply_rename(self, folder, csv_path, *, emit_event):
        emit_event({"progress": 60, "message": "Файлы переименовываются."})
        self.renamed = True
        undo_csv = folder / "frame-sort-undo-test.csv"
        undo_csv.write_text(
            "position,source_filename\n",
            encoding="utf-8",
            newline="",
        )
        undo_csv.with_suffix(".meta.json").write_text(
            "{}\n",
            encoding="utf-8",
            newline="",
        )
        _write_sort_journal(folder, undo_csv, "test", source_csv=csv_path)
        return {
            "folder": str(folder),
            "renamed_count": 2,
            "undo_csv_path": str(undo_csv),
        }

    def rebuild_video(self, folder, original_video, output_video, *, emit_event):
        emit_event({"phase": "encoding", "processed": 3, "total": 4})
        emit_event(
            {
                "type": "media_probe",
                "role": "output",
                "phase": "verifying_video",
                "processed": 4,
                "total": 4,
                "container_duration_seconds": "12.500000",
                "message": "Новый MP4 проверен.",
            }
        )
        self.rebuild_calls.append((folder, original_video, output_video))
        return {
            "folder": str(folder),
            "output_video": str(output_video) if output_video is not None else None,
            "frame_count": 4,
        }


def _wait(client: TestClient, job_id: str) -> dict:
    for _ in range(100):
        payload = client.get(f"/api/job/{job_id}").json()
        if payload["terminal"]:
            return payload
        threading.Event().wait(0.01)
    raise AssertionError("Фоновая задача не завершилась.")


def _write_sort_journal(
    folder: Path,
    undo_csv: Path,
    transaction_id: str,
    *,
    source_csv: Path | None = None,
) -> Path:
    state_dir = folder / ".frame-sort-state"
    state_dir.mkdir(exist_ok=True)
    journal = state_dir / f"transaction-{transaction_id}.json"
    journal.write_text(
        json.dumps(
            {
                "transaction_id": transaction_id,
                "status": "completed",
                "folder": str(folder.resolve()),
                "csv_path": str(source_csv or folder / "frame-sort-plan.csv"),
                "undo_csv_path": str(undo_csv),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
        newline="",
    )
    return journal


def test_analyze_then_confirmed_rename(tmp_path: Path, monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr(web_app, "service_api", service)
    client = TestClient(web_app.app)

    started = client.post("/api/analyze", headers=ORIGIN, json={"folder": str(tmp_path)})
    assert started.status_code == 202
    analyzed = _wait(client, started.json()["job_id"])
    assert analyzed["status"] == "completed"
    assert analyzed["result"]["frame_count"] == 4

    rejected = client.post("/api/rename", headers=ORIGIN, json={"folder": str(tmp_path)})
    assert rejected.status_code == 400
    started_rename = client.post(
        "/api/rename",
        headers=ORIGIN,
        json={"folder": str(tmp_path), "confirm": True},
    )
    assert started_rename.status_code == 202
    renamed = _wait(client, started_rename.json()["job_id"])
    assert renamed["status"] == "completed"
    assert service.renamed is True


def test_only_one_background_job_is_active(tmp_path: Path, monkeypatch) -> None:
    service = FakeService()
    service.block = True
    monkeypatch.setattr(web_app, "service_api", service)
    client = TestClient(web_app.app)
    first = client.post("/api/analyze", headers=ORIGIN, json={"folder": str(tmp_path)})
    try:
        second = client.post("/api/analyze", headers=ORIGIN, json={"folder": str(tmp_path)})
        assert second.status_code == 409
    finally:
        service.release.set()
    assert _wait(client, first.json()["job_id"])["status"] == "completed"


def test_rebuild_requires_rename_confirmation_and_new_output(tmp_path: Path, monkeypatch) -> None:
    service = FakeService()
    monkeypatch.setattr(web_app, "service_api", service)
    client = TestClient(web_app.app)

    rejected = client.post(
        "/api/rebuild",
        headers=ORIGIN,
        json={"folder": str(tmp_path), "confirm": True},
    )
    assert rejected.status_code == 409

    analysis = client.post("/api/analyze", headers=ORIGIN, json={"folder": str(tmp_path)})
    assert _wait(client, analysis.json()["job_id"])["status"] == "completed"
    rename = client.post(
        "/api/rename",
        headers=ORIGIN,
        json={"folder": str(tmp_path), "confirm": True},
    )
    renamed = _wait(client, rename.json()["job_id"])
    assert renamed["status"] == "completed"
    assert renamed["rebuild_ready"] is True

    original_video = tmp_path / "исходное видео.mp4"
    original_video.write_bytes(b"mp4")
    output_video = tmp_path / "новое имя.mp4"
    unconfirmed = client.post(
        "/api/rebuild",
        headers=ORIGIN,
        json={"folder": str(tmp_path), "output_video": str(output_video)},
    )
    assert unconfirmed.status_code == 400

    existing_output = tmp_path / "существующее.mp4"
    existing_output.write_bytes(b"mp4")
    overwrite = client.post(
        "/api/rebuild",
        headers=ORIGIN,
        json={
            "folder": str(tmp_path),
            "output_video": str(existing_output),
            "confirm": True,
        },
    )
    assert overwrite.status_code == 409

    started = client.post(
        "/api/rebuild",
        headers=ORIGIN,
        json={
            "folder": str(tmp_path),
            "original_video": str(original_video),
            "output_video": str(output_video),
            "confirm": True,
        },
    )
    assert started.status_code == 202
    rebuilt = _wait(client, started.json()["job_id"])
    assert rebuilt["status"] == "completed"
    assert rebuilt["result"]["output_video"] == str(output_video.resolve())
    assert rebuilt["phase"] == "verifying_video"
    assert rebuilt["processed"] == 4
    assert rebuilt["total"] == 4
    assert rebuilt["diagnostics"][-1]["container_duration_seconds"] == "12.500000"
    detail = client.get(f"/api/jobs/{rebuilt['job_id']}").json()
    assert any(
        entry["event"].get("type") == "media_probe"
        for entry in detail["events"]
    )
    assert service.rebuild_calls == [
        (tmp_path.resolve(), original_video.resolve(), output_video.resolve())
    ]


def test_pick_video_returns_named_path(tmp_path: Path, monkeypatch) -> None:
    selected = tmp_path / "новое имя.mp4"
    monkeypatch.setattr(web_app, "pick_video_path", lambda kind, **_kwargs: selected)
    response = TestClient(web_app.app).post(
        "/api/pick-video",
        headers=ORIGIN,
        json={"kind": "output", "folder": str(tmp_path)},
    )
    assert response.status_code == 200
    assert response.json() == {
        "kind": "output",
        "path": str(selected),
        "original_video": None,
        "output_video": str(selected),
    }


def test_refresh_restores_rebuild_from_existing_undo_pair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    undo_csv = tmp_path / "frame-sort-undo-existing.csv"
    undo_csv.write_text(
        "position,source_filename\n",
        encoding="utf-8",
        newline="",
    )
    undo_csv.with_suffix(".meta.json").write_text(
        "{}\n",
        encoding="utf-8",
        newline="",
    )
    _write_sort_journal(tmp_path, undo_csv, "existing")

    response = TestClient(web_app.app).post(
        "/api/refresh",
        headers=ORIGIN,
        json={"folder": str(tmp_path)},
    )

    assert response.status_code == 200
    workflow = response.json()["workflow"]
    assert workflow["actions"] == {
        "analyze": False,
        "rename": False,
        "undo": True,
        "rebuild": True,
    }
    assert workflow["artifacts"]["undo_csv"] == str(undo_csv)


def test_rebuild_uses_disk_artifact_without_previous_web_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = FakeService()
    monkeypatch.setattr(web_app, "service_api", service)
    undo_csv = tmp_path / "frame-sort-undo-existing.csv"
    undo_csv.write_text(
        "position,source_filename\n",
        encoding="utf-8",
        newline="",
    )
    undo_csv.with_suffix(".meta.json").write_text(
        "{}\n",
        encoding="utf-8",
        newline="",
    )
    _write_sort_journal(tmp_path, undo_csv, "existing")

    started = TestClient(web_app.app).post(
        "/api/rebuild",
        headers=ORIGIN,
        json={"folder": str(tmp_path), "confirm": True},
    )

    assert started.status_code == 202
    completed = _wait(TestClient(web_app.app), started.json()["job_id"])
    assert completed["status"] == "completed"
    assert service.rebuild_calls == [(tmp_path.resolve(), None, None)]


def test_undo_uses_disk_artifact_and_resets_workflow(
    frame_directory: Path,
) -> None:
    from frame_sorter.service import analyze_folder, apply_rename

    analysis = analyze_folder(frame_directory)
    renamed = apply_rename(frame_directory, analysis.csv_path)
    client = TestClient(web_app.app)

    unconfirmed = client.post(
        "/api/undo",
        headers=ORIGIN,
        json={"folder": str(frame_directory)},
    )
    assert unconfirmed.status_code == 400

    started = client.post(
        "/api/undo",
        headers=ORIGIN,
        json={"folder": str(frame_directory), "confirm": True},
    )
    assert started.status_code == 202
    completed = _wait(client, started.json()["job_id"])
    assert completed["status"] == "completed"
    assert completed["kind"] == "undo"
    assert completed["message"] == "Исходные имена восстановлены."
    assert completed["result"]["renamed_count"] == renamed.renamed_count
    assert "undo_csv_path" not in completed["result"]
    assert completed["result"]["redo_csv_path"].endswith(".csv")

    refreshed = client.post(
        "/api/refresh",
        headers=ORIGIN,
        json={"folder": str(frame_directory)},
    )
    assert refreshed.status_code == 200
    workflow = refreshed.json()["workflow"]
    assert workflow["actions"] == {
        "analyze": True,
        "rename": False,
        "undo": False,
        "rebuild": False,
    }
    assert workflow["artifacts"]["plan_csv"] is None
    assert workflow["artifacts"]["undo_csv"] is None

    repeated = client.post(
        "/api/undo",
        headers=ORIGIN,
        json={"folder": str(frame_directory), "confirm": True},
    )
    assert repeated.status_code == 409


def test_active_job_snapshot_does_not_read_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(_folder: Path) -> bool:
        raise AssertionError("Активная задача не должна читать журналы транзакций.")

    monkeypatch.setattr(web_app, "_rebuild_ready_for", fail_if_called)
    snapshot = web_app._job_snapshot(
        {
            "folder": str(tmp_path),
            "active": True,
            "logs": [],
            "diagnostics": [],
            "result": None,
        }
    )

    assert snapshot["rebuild_ready"] is False


def test_post_requires_exact_origin_and_loopback_host(tmp_path: Path) -> None:
    client = TestClient(web_app.app)
    assert client.post("/api/analyze", json={"folder": str(tmp_path)}).status_code == 403
    assert client.post(
        "/api/analyze",
        headers={"Origin": "http://localhost"},
        json={"folder": str(tmp_path)},
    ).status_code == 403
    assert client.get("/api/health", headers={"Host": "example.test"}).status_code == 403


def test_index_has_required_short_buttons() -> None:
    response = TestClient(web_app.app).get("/")
    assert response.status_code == 200
    assert "Создать CSV" in response.text
    assert "Переименовать" in response.text
    assert "Вернуть имена" in response.text
    assert 'id="undo"' in response.text
    assert 'aria-describedby="renameDetail" hidden disabled' in response.text
    assert "Собрать MP4" in response.text
    assert 'id="original-video"' in response.text
    assert 'id="output-video"' in response.text
    assert '<script src="/static/app.js" type="module"></script>' in response.text
    assert "<img" not in response.text.casefold()


def test_frontend_undo_uses_workflow_action_and_async_endpoint() -> None:
    script = TestClient(web_app.app).get("/static/app.js")

    assert script.status_code == 200
    assert "actions.undo === true" in script.text
    assert "undoButton.hidden = !canUndo" in script.text
    assert "undoButton.disabled = locked || !hasFolder || !canUndo" in script.text
    assert 'window.confirm("Вернуть исходные имена файлов?' in script.text
    assert '"/api/undo"' in script.text


def test_frontend_uses_validated_relative_sse_and_top_level_await() -> None:
    script = TestClient(web_app.app).get("/static/app.js")

    assert script.status_code == 200
    assert "const safeJobId = validatedJobId(jobId)" in script.text
    assert "const streamPath = `/api/stream/${safeJobId}?cursor=${safeCursor}`" in script.text
    assert "new EventSource(streamPath)" in script.text
    assert "await initialize()" in script.text
    assert "initialize().catch" not in script.text


def test_openapi_documents_http_errors() -> None:
    paths = TestClient(web_app.app).get("/openapi.json").json()["paths"]

    assert set(paths["/api/pick"]["post"]["responses"]) >= {"200", "409", "500"}
    assert set(paths["/api/refresh"]["post"]["responses"]) >= {"200", "400", "422"}
    assert set(paths["/api/pick-video"]["post"]["responses"]) >= {"200", "409", "422", "500"}
    for path in ("/api/analyze", "/api/rename", "/api/undo", "/api/rebuild"):
        assert set(paths[path]["post"]["responses"]) >= {"202", "400", "409", "422"}
    for path in ("/api/job/{job_id}", "/api/jobs/{job_id}"):
        assert set(paths[path]["get"]["responses"]) >= {"200", "404", "422"}


def test_sse_replays_events_and_honors_cursor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_app, "service_api", FakeService())
    client = TestClient(web_app.app)
    started = client.post("/api/analyze", headers=ORIGIN, json={"folder": str(tmp_path)})
    completed = _wait(client, started.json()["job_id"])

    stream = client.get(f"/api/stream/{completed['job_id']}?cursor=0")
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "id: 1\n" in stream.text
    assert '"type":"done"' in stream.text

    exhausted = client.get(
        f"/api/stream/{completed['job_id']}?cursor={completed['latest_event_id']}"
    )
    assert exhausted.status_code == 200
    assert exhausted.text == ""


def test_sse_sends_keep_alive_while_waiting(monkeypatch) -> None:
    job_id = "sse-keep-alive"
    task = {
        "job_id": job_id,
        "events": [],
        "terminal": False,
        "terminal_event": None,
        "latest_event_id": 0,
    }
    condition = threading.Condition(web_app._state_lock)
    monkeypatch.setattr(web_app, "SSE_KEEP_ALIVE_SECONDS", 0.0)
    with web_app._state_lock:
        web_app._jobs[job_id] = task
        web_app._job_conditions[job_id] = condition

    stream = web_app._iter_sse(job_id, 0)
    try:
        assert next(stream) == ": keep-alive\n\n"
        done = {"type": "done", "status": "completed"}
        with web_app._state_lock:
            task["events"].append((1, done))
            task["terminal"] = True
            task["terminal_event"] = done
            task["latest_event_id"] = 1
        assert list(stream) == ['id: 1\ndata: {"type":"done","status":"completed"}\n\n']
    finally:
        with web_app._state_lock:
            web_app._jobs.pop(job_id, None)
            web_app._job_conditions.pop(job_id, None)


def test_job_logs_and_events_are_bounded(tmp_path: Path, monkeypatch) -> None:
    class ChattyService(FakeService):
        def analyze_folder(self, folder, output_csv, *, emit_event):
            for index in range(2_100):
                emit_event({"progress": 50, "message": f"Стадия {index}"})
            return super().analyze_folder(folder, output_csv, emit_event=emit_event)

    monkeypatch.setattr(web_app, "service_api", ChattyService())
    client = TestClient(web_app.app)
    started = client.post("/api/analyze", headers=ORIGIN, json={"folder": str(tmp_path)})
    completed = _wait(client, started.json()["job_id"])
    detailed = client.get(f"/api/jobs/{completed['job_id']}").json()

    assert len(detailed["logs"]) == web_app.LOG_HISTORY_LIMIT
    assert len(detailed["events"]) == web_app.EVENT_HISTORY_LIMIT
    assert detailed["events"][-1]["event"]["type"] == "done"


def test_background_worker_is_not_daemon(tmp_path: Path, monkeypatch) -> None:
    service = FakeService()
    service.block = True
    monkeypatch.setattr(web_app, "service_api", service)
    client = TestClient(web_app.app)
    started = client.post("/api/analyze", headers=ORIGIN, json={"folder": str(tmp_path)})
    job_id = started.json()["job_id"]
    try:
        assert web_app._workers[job_id].daemon is False
    finally:
        service.release.set()
    assert _wait(client, job_id)["status"] == "completed"


def test_sqlite_is_enabled_only_for_explicit_web_runtime(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "state" / "jobs.sqlite3"
    monkeypatch.setenv("WEB_JOB_DB", str(database))
    monkeypatch.delenv(web_app.WEB_RUNTIME_LOGGING_ENV, raising=False)
    with TestClient(web_app.app) as client:
        assert client.get("/api/health").status_code == 200
    assert not database.exists()

    store = SQLiteJobStore(database)
    snapshot = {
        "job_id": "f" * 32,
        "kind": "analysis",
        "folder": str(tmp_path),
        "status": "running",
        "active": True,
        "terminal": False,
        "progress": 45,
        "message": "Анализ выполняется.",
        "logs": ["Анализ выполняется."],
        "result": None,
        "error": None,
        "created_at": 1.0,
        "updated_at": 2.0,
        "completed_at": None,
        "latest_event_id": 1,
    }
    store.save(snapshot, [(1, {"type": "job", "status": "running"})])
    store.close()

    monkeypatch.setenv(web_app.WEB_RUNTIME_LOGGING_ENV, "1")
    monkeypatch.setattr(web_app, "setup_web_logging", lambda: None)
    with TestClient(web_app.app) as client:
        recovered = client.get("/api/active-job").json()
        assert recovered["job_id"] == "f" * 32
        assert recovered["status"] == "interrupted"
        assert recovered["terminal"] is True
        assert web_app._job_store is not None
    assert web_app._job_store is None
