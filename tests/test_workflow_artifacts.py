from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from frame_sorter.web.workflow import (
    WorkflowArtifactError,
    inspect_folder_artifacts,
    require_rebuild_cache,
    require_undo_cache,
    resolve_cached_plan_for_rename,
)


def test_empty_folder_requires_analysis_without_scanning_jpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "кадры"
    folder.mkdir()
    (folder / "произвольное имя.jpeg").write_bytes(b"not-opened")

    def reject_iteration(_path: Path):
        raise AssertionError("JPEG и содержимое каталога сканировать нельзя")

    monkeypatch.setattr(Path, "iterdir", reject_iteration)

    workflow = inspect_folder_artifacts(folder)

    assert workflow["folder"] == str(folder.resolve())
    assert workflow["stages"]["analysis"]["state"] == "ready"
    assert workflow["actions"] == {
        "analyze": True,
        "rename": False,
        "undo": False,
        "rebuild": False,
    }
    assert workflow["warnings"] == []


def test_complete_plan_pair_enables_rename_and_defers_validation(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    plan = folder / "frame-sort-plan.csv"
    plan.write_text("битый CSV", encoding="utf-8", newline="")
    plan.with_suffix(".meta.json").write_text("{", encoding="utf-8", newline="")

    workflow = inspect_folder_artifacts(folder)

    assert workflow["stages"]["analysis"] == {
        "state": "completed",
        "cached": True,
        "detail": "Кандидат кеша найден; полная проверка при запуске.",
    }
    assert workflow["actions"] == {
        "analyze": False,
        "rename": True,
        "undo": False,
        "rebuild": False,
    }
    assert workflow["artifacts"]["plan_csv"] == str(plan)
    assert any("будут проверены при запуске" in item for item in workflow["warnings"])
    assert resolve_cached_plan_for_rename(folder) == plan


def test_complete_undo_pair_dominates_plan_and_enables_rebuild(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    _write_pair(folder, "frame-sort-plan")
    undo = _write_pair(folder, "frame-sort-undo-abc123")
    journal = _write_completed_journal(folder, undo, "abc123")

    workflow = inspect_folder_artifacts(folder)

    assert workflow["stages"]["analysis"]["state"] == "completed"
    assert workflow["stages"]["rename"] == {
        "state": "completed",
        "cached": True,
        "detail": "Кандидат кеша найден; полная проверка при запуске.",
    }
    assert workflow["actions"] == {
        "analyze": False,
        "rename": False,
        "undo": True,
        "rebuild": True,
    }
    assert workflow["artifacts"]["undo_csv"] == str(undo)
    assert workflow["artifacts"]["journal"] == str(journal)
    assert require_rebuild_cache(folder) == undo
    assert require_undo_cache(folder) == undo


def test_completed_undo_transaction_resets_workflow(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    _write_pair(folder, "frame-sort-plan")
    source_undo = _write_pair(folder, "frame-sort-undo-source")
    reverse_undo = _write_pair(folder, "frame-sort-undo-reverse")
    _write_completed_journal(folder, source_undo, "source")
    journal = _write_completed_journal(folder, reverse_undo, "reverse")
    value = json.loads(journal.read_text(encoding="utf-8"))
    value["csv_path"] = str(source_undo)
    journal.write_text(
        json.dumps(value, ensure_ascii=False),
        encoding="utf-8",
        newline="",
    )

    workflow = inspect_folder_artifacts(folder)

    assert workflow["actions"] == {
        "analyze": True,
        "rename": False,
        "undo": False,
        "rebuild": False,
    }
    assert workflow["artifacts"]["plan_csv"] is None
    assert workflow["artifacts"]["undo_csv"] is None
    with pytest.raises(WorkflowArtifactError, match="Нет завершённой сортировки"):
        require_undo_cache(folder)


def test_undo_pair_without_journal_is_ignored(
    tmp_path: Path,
) -> None:
    folder = _folder(tmp_path)
    _write_pair(folder, "frame-sort-undo-no-journal")

    workflow = inspect_folder_artifacts(folder)

    assert workflow["actions"]["undo"] is False
    assert workflow["actions"]["rebuild"] is False
    assert workflow["artifacts"]["undo_csv"] is None
    assert workflow["artifacts"]["journal"] is None
    assert any("нет соответствующей" in item for item in workflow["warnings"])


def test_partial_pairs_do_not_unlock_actions(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    (folder / "frame-sort-plan.csv").write_text("csv", encoding="utf-8", newline="")
    (folder / "frame-sort-undo-x.meta.json").write_text(
        "{}", encoding="utf-8", newline=""
    )

    workflow = inspect_folder_artifacts(folder)

    assert workflow["actions"] == {
        "analyze": True,
        "rename": False,
        "undo": False,
        "rebuild": False,
    }
    assert len(workflow["warnings"]) == 2
    assert all("Неполная пара" in item for item in workflow["warnings"])
    with pytest.raises(WorkflowArtifactError, match="Полная пара"):
        resolve_cached_plan_for_rename(folder)
    with pytest.raises(WorkflowArtifactError, match="Нет завершённой сортировки"):
        require_rebuild_cache(folder)


def test_foreign_metadata_is_candidate_but_reported_for_strict_worker(
    tmp_path: Path,
) -> None:
    folder = _folder(tmp_path)
    plan = folder / "frame-sort-plan.csv"
    plan.write_text("csv", encoding="utf-8", newline="")
    plan.with_suffix(".meta.json").write_text(
        json.dumps({"folder": str(tmp_path / "другой")}, ensure_ascii=False),
        encoding="utf-8",
        newline="",
    )

    workflow = inspect_folder_artifacts(folder)

    assert workflow["actions"]["rename"] is True
    assert any("другой каталог" in item for item in workflow["warnings"])


def test_requested_plan_must_be_named_pair_inside_folder(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    custom = _write_pair(folder, "custom")
    outside = _write_pair(tmp_path, "frame-sort-plan")

    with pytest.raises(WorkflowArtifactError, match="Ожидался кеш"):
        resolve_cached_plan_for_rename(folder, custom)
    with pytest.raises(WorkflowArtifactError, match="выбранном каталоге"):
        resolve_cached_plan_for_rename(folder, outside)


def test_old_plan_requires_analysis_after_algorithm_upgrade(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    plan = _write_pair(folder, "frame-sort-plan")
    plan.with_suffix(".meta.json").write_text(
        json.dumps(
            {"folder": str(folder), "algorithm_version": "local-return-v1"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
        newline="",
    )

    workflow = inspect_folder_artifacts(folder)

    assert workflow["actions"]["analyze"] is True
    assert workflow["actions"]["rename"] is False
    assert any("требуется local-return-v2" in item for item in workflow["warnings"])


def test_latest_undo_transaction_requires_new_analysis(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    plan = _write_pair(folder, "frame-sort-plan")
    plan.with_suffix(".meta.json").write_text(
        json.dumps(
            {"folder": str(folder), "algorithm_version": "local-return-v1"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
        newline="",
    )
    undo = _write_pair(folder, "frame-sort-undo-original")
    journal = _write_completed_journal(folder, undo, "original")
    value = json.loads(journal.read_text(encoding="utf-8"))
    value["csv_path"] = str(undo)
    journal.write_text(
        json.dumps(value, ensure_ascii=False),
        encoding="utf-8",
        newline="",
    )

    workflow = inspect_folder_artifacts(folder)

    assert workflow["actions"] == {
        "analyze": True,
        "rename": False,
        "undo": False,
        "rebuild": False,
    }
    with pytest.raises(WorkflowArtifactError, match="Нет завершённой сортировки"):
        require_rebuild_cache(folder)


def test_new_plan_after_undo_transaction_enables_rename(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    source_undo = _write_pair(folder, "frame-sort-undo-source")
    reverse_undo = _write_pair(folder, "frame-sort-undo-reverse")
    journal = _write_completed_journal(folder, reverse_undo, "reverse")
    value = json.loads(journal.read_text(encoding="utf-8"))
    value["csv_path"] = str(source_undo)
    journal.write_text(
        json.dumps(value, ensure_ascii=False),
        encoding="utf-8",
        newline="",
    )
    plan = _write_pair(folder, "frame-sort-plan")
    newer = journal.stat().st_mtime_ns + 10_000_000
    os.utime(plan, ns=(newer, newer))
    os.utime(plan.with_suffix(".meta.json"), ns=(newer, newer))

    workflow = inspect_folder_artifacts(folder)

    assert workflow["actions"] == {
        "analyze": False,
        "rename": True,
        "undo": False,
        "rebuild": False,
    }


def test_unfinished_transaction_blocks_cached_actions(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    _write_pair(folder, "frame-sort-plan")
    undo = _write_pair(folder, "frame-sort-undo-sort")
    _write_completed_journal(folder, undo, "sort")
    state_dir = folder / ".frame-sort-state"
    (state_dir / "transaction-unfinished.json").write_text(
        json.dumps({"status": "phase1"}, ensure_ascii=False),
        encoding="utf-8",
        newline="",
    )

    workflow = inspect_folder_artifacts(folder)

    assert workflow["actions"] == {
        "analyze": False,
        "rename": False,
        "undo": False,
        "rebuild": False,
    }
    assert all(stage["state"] == "blocked" for stage in workflow["stages"].values())
    with pytest.raises(WorkflowArtifactError, match="незавершённая транзакция"):
        require_undo_cache(folder)


def test_authoritative_sort_does_not_fall_back_to_orphan_undo(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    _write_pair(folder, "frame-sort-plan")
    authoritative = _write_pair(folder, "frame-sort-undo-sort")
    _write_completed_journal(folder, authoritative, "sort")
    orphan = _write_pair(folder, "frame-sort-undo-orphan")
    newer = authoritative.stat().st_mtime_ns + 10_000_000
    os.utime(orphan, ns=(newer, newer))
    os.utime(orphan.with_suffix(".meta.json"), ns=(newer, newer))

    assert require_undo_cache(folder) == authoritative


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "кадры"
    folder.mkdir()
    return folder.resolve()


def _write_pair(folder: Path, stem: str) -> Path:
    folder.mkdir(exist_ok=True)
    csv_path = folder / f"{stem}.csv"
    csv_path.write_text("position,source_filename\n", encoding="utf-8", newline="")
    csv_path.with_suffix(".meta.json").write_text(
        json.dumps(
            {"folder": str(folder), "algorithm_version": "local-return-v2"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
        newline="",
    )
    return csv_path


def _write_completed_journal(
    folder: Path,
    undo_csv: Path,
    transaction_id: str,
) -> Path:
    state_dir = folder / ".frame-sort-state"
    state_dir.mkdir(exist_ok=True)
    journal = state_dir / f"transaction-{transaction_id}.json"
    journal.write_text(
        json.dumps(
            {
                "transaction_id": transaction_id,
                "status": "completed",
                "folder": str(folder),
                "undo_csv_path": str(undo_csv),
                "csv_path": str(folder / "frame-sort-plan.csv"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
        newline="",
    )
    return journal
