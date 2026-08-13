from __future__ import annotations

from pathlib import Path

from frame_sorter.web.job_store import RECOVERY_ERROR, SQLiteJobStore


def _snapshot(job_id: str, *, active: bool, terminal: bool, updated: float) -> dict:
    return {
        "job_id": job_id,
        "kind": "analysis",
        "folder": "D:/Кадры",
        "status": "running" if active else "completed",
        "active": active,
        "terminal": terminal,
        "progress": 50,
        "message": "Кадры сравниваются.",
        "logs": ["Кадры сравниваются."],
        "result": None,
        "error": None,
        "created_at": 10.0,
        "updated_at": updated,
        "completed_at": updated if terminal else None,
        "latest_event_id": 0,
    }


def test_roundtrip_unicode_and_bounded_history(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3", max_jobs=2, max_events_per_job=2)
    try:
        for index in range(3):
            job_id = f"{index:032x}"
            snapshot = _snapshot(job_id, active=False, terminal=True, updated=20.0 + index)
            events = [
                (event_id, {"type": "log", "message": f"Сообщение {event_id}"})
                for event_id in range(1, 4)
            ]
            snapshot["latest_event_id"] = 3
            store.save(snapshot, events)

        jobs = store.list()
        assert [job["job_id"] for job in jobs] == [f"{2:032x}", f"{1:032x}"]
        assert [entry["id"] for entry in jobs[0]["events"]] == [2, 3]
        assert jobs[0]["folder"] == "D:/Кадры"
    finally:
        store.close()


def test_recovery_terminalizes_active_job_once(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)
    snapshot = _snapshot("a" * 32, active=True, terminal=False, updated=20.0)
    store.save(snapshot, [(1, {"type": "job", "status": "running"})])

    assert store.recover_interrupted() == ["a" * 32]
    recovered = store.load("a" * 32)
    assert recovered is not None
    assert recovered["status"] == "interrupted"
    assert recovered["active"] is False
    assert recovered["terminal"] is True
    assert recovered["error"] == RECOVERY_ERROR
    assert recovered["events"][-1]["event"]["type"] == "done"
    assert recovered["events"][-1]["event"]["status"] == "interrupted"
    assert store.recover_interrupted() == []
    store.close()


def test_close_is_idempotent(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    store.close()
    store.close()
