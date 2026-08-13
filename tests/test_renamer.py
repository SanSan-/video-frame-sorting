from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from frame_sorter import renamer
from frame_sorter.exceptions import TransactionError, ValidationError
from frame_sorter.service import analyze_folder, apply_rename, preview_rename
from tests.conftest import content_hashes


def test_preview_does_not_change_files(frame_directory: Path, tmp_path: Path) -> None:
    plan = tmp_path / "plan.csv"
    analyze_folder(frame_directory, output_csv=plan)
    before = content_hashes(frame_directory)

    preview = preview_rename(frame_directory, plan)

    assert preview.rename_count > 0
    assert content_hashes(frame_directory) == before
    assert not (frame_directory / ".frame-sort-state").exists()


def test_apply_preserves_content_and_undo_restores_names(
    frame_directory: Path, tmp_path: Path
) -> None:
    plan = tmp_path / "plan.csv"
    analyze_folder(frame_directory, output_csv=plan)
    original = content_hashes(frame_directory)
    original_multiset = sorted(original.values())

    result = apply_rename(frame_directory, plan)

    renamed = content_hashes(frame_directory)
    assert sorted(renamed.values()) == original_multiset
    assert result.renamed_count > 0
    assert result.undo_csv_path.is_file()
    assert json.loads(result.journal_path.read_text(encoding="utf-8"))["status"] == "completed"
    assert not list(frame_directory.glob(".frame-sort-*.tmp"))

    apply_rename(frame_directory, result.undo_csv_path)
    assert content_hashes(frame_directory) == original


def test_failure_during_first_phase_rolls_back(
    frame_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.csv"
    analyze_folder(frame_directory, output_csv=plan)
    original = content_hashes(frame_directory)
    real_replace = renamer._replace
    calls = 0

    def fail_once(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("имитация сбоя")
        real_replace(source, target)

    monkeypatch.setattr(renamer, "_replace", fail_once)

    with pytest.raises(TransactionError, match="восстановлены"):
        apply_rename(frame_directory, plan)

    assert content_hashes(frame_directory) == original
    journals = list((frame_directory / ".frame-sort-state").glob("transaction-*.json"))
    assert len(journals) == 1
    assert json.loads(journals[0].read_text(encoding="utf-8"))["status"] == "rolled_back"


def test_modified_csv_is_rejected(frame_directory: Path, tmp_path: Path) -> None:
    plan = tmp_path / "plan.csv"
    analyze_folder(frame_directory, output_csv=plan)
    with plan.open("a", encoding="utf-8", newline="") as stream:
        stream.write("\n")

    with pytest.raises(ValidationError, match="CSV изменился"):
        preview_rename(frame_directory, plan)


def test_csv_with_extra_field_is_rejected(
    frame_directory: Path, tmp_path: Path
) -> None:
    plan = tmp_path / "strict.csv"
    analyze_folder(frame_directory, output_csv=plan)
    lines = plan.read_text(encoding="utf-8").splitlines()
    lines[1] = f"{lines[1]},лишнее"
    plan.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    metadata_path = plan.with_suffix(".meta.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["csv_sha256"] = renamer.file_sha256(plan)
    renamer.atomic_write_json(metadata_path, metadata)

    with pytest.raises(ValidationError, match="Лишнее поле CSV"):
        preview_rename(frame_directory, plan)


def test_content_change_with_same_size_and_mtime_is_rejected(
    frame_directory: Path, tmp_path: Path
) -> None:
    plan = tmp_path / "content-hash.csv"
    analyze_folder(frame_directory, output_csv=plan)
    frame = next(frame_directory.glob("*.jpg"))
    stat = frame.stat()
    payload = bytearray(frame.read_bytes())
    payload[len(payload) // 2] ^= 1
    frame.write_bytes(payload)
    os.utime(frame, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    with pytest.raises(ValidationError, match="Каталог кадров изменился"):
        preview_rename(frame_directory, plan)


def test_arbitrary_names_are_renamed_and_restored(
    frame_directory: Path, tmp_path: Path
) -> None:
    for position, path in enumerate(sorted(frame_directory.glob("*.jpg"))):
        path.rename(frame_directory / f"сцена {position * 3 + 1}.jpg")
    original = content_hashes(frame_directory)
    plan = tmp_path / "arbitrary.csv"
    analyze_folder(frame_directory, output_csv=plan)

    result = apply_rename(frame_directory, plan)

    assert {path.name for path in frame_directory.glob("*.jpg")} == {
        f"frame_{position:05d}.jpg" for position in range(12)
    }
    apply_rename(frame_directory, result.undo_csv_path)
    assert content_hashes(frame_directory) == original


def test_keyboard_interrupt_rolls_back(
    frame_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "interrupt.csv"
    analyze_folder(frame_directory, output_csv=plan)
    original = content_hashes(frame_directory)
    real_replace = renamer._replace
    calls = 0

    def interrupt_once(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        real_replace(source, target)

    monkeypatch.setattr(renamer, "_replace", interrupt_once)

    with pytest.raises(KeyboardInterrupt):
        apply_rename(frame_directory, plan)

    assert content_hashes(frame_directory) == original


def test_failure_during_second_phase_rolls_back(
    frame_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "phase2.csv"
    analyze_folder(frame_directory, output_csv=plan)
    original = content_hashes(frame_directory)
    real_replace = renamer._replace
    failed = False

    def fail_phase2_once(source: Path, target: Path) -> None:
        nonlocal failed
        if source.suffix == ".tmp" and not failed:
            failed = True
            raise OSError("имитация сбоя второй фазы")
        real_replace(source, target)

    monkeypatch.setattr(renamer, "_replace", fail_phase2_once)

    with pytest.raises(TransactionError, match="восстановлены"):
        apply_rename(frame_directory, plan)

    assert content_hashes(frame_directory) == original


def test_external_target_is_not_overwritten(
    frame_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "collision.csv"
    analyze_folder(frame_directory, output_csv=plan)
    original = content_hashes(frame_directory)
    real_replace = renamer._replace
    injected = False
    external_path: Path | None = None

    def inject_external_file(source: Path, target: Path) -> None:
        nonlocal injected, external_path
        if source.suffix == ".tmp" and not injected:
            injected = True
            external_path = target
            target.write_bytes(b"external-file")
        real_replace(source, target)

    monkeypatch.setattr(renamer, "_replace", inject_external_file)

    with pytest.raises(TransactionError, match="автоматический откат"):
        apply_rename(frame_directory, plan)

    assert external_path is not None
    assert external_path.read_bytes() == b"external-file"
    transaction_files = {
        path.name: path
        for path in frame_directory.iterdir()
        if path.is_file() and path.name != external_path.name
    }
    assert sorted(path.read_bytes() for path in transaction_files.values() if path.suffix == ".tmp")
    assert set(original.values()).issubset(
        {
            renamer.file_sha256(path)
            for path in transaction_files.values()
            if path.suffix.casefold() in {".jpg", ".jpeg", ".tmp"}
        }
    )


def test_completed_callback_error_does_not_change_success(
    frame_directory: Path, tmp_path: Path
) -> None:
    plan = tmp_path / "callback.csv"
    analyze_folder(frame_directory, output_csv=plan)

    def reject_completed(event: dict[str, object]) -> None:
        if event.get("phase") == "completed":
            raise RuntimeError("сбой обработчика прогресса")

    result = renamer.apply_rename(frame_directory, plan, emit_event=reject_completed)

    assert result.journal_path.is_file()
    assert json.loads(result.journal_path.read_text(encoding="utf-8"))["status"] == "completed"


@pytest.mark.parametrize(
    ("status", "locations"),
    [
        ("prepared", {"a.jpg": "a", "b.jpg": "b"}),
        ("phase1", {".frame-sort-recovery-00000000.tmp": "a", "b.jpg": "b"}),
        ("phase2", {"b.jpg": "a", ".frame-sort-recovery-00000001.tmp": "b"}),
        ("verifying", {"b.jpg": "a", "a.jpg": "b"}),
        (
            "rollback_targets",
            {".frame-sort-recovery-00000000.tmp": "a", "a.jpg": "b"},
        ),
        (
            "rollback_sources",
            {"a.jpg": "a", ".frame-sort-recovery-00000001.tmp": "b"},
        ),
    ],
)
def test_recovery_resumes_each_transaction_phase(
    tmp_path: Path,
    status: str,
    locations: dict[str, str],
) -> None:
    folder, journal_path, originals = _create_recovery_case(
        tmp_path, status, locations
    )

    recovered = renamer.recover_transaction(folder, journal_path)

    assert recovered == journal_path
    assert (folder / "a.jpg").read_bytes() == originals["a"]
    assert (folder / "b.jpg").read_bytes() == originals["b"]
    assert not list(folder.glob(".frame-sort-*.tmp"))
    assert json.loads(journal_path.read_text(encoding="utf-8"))["status"] == "rolled_back"


def test_recovery_rejects_unknown_status(tmp_path: Path) -> None:
    folder, journal_path, originals = _create_recovery_case(
        tmp_path,
        "неизвестный",
        {"a.jpg": "a", "b.jpg": "b"},
    )

    with pytest.raises(TransactionError, match="неизвестный статус"):
        renamer.recover_transaction(folder, journal_path)

    assert (folder / "a.jpg").read_bytes() == originals["a"]
    assert (folder / "b.jpg").read_bytes() == originals["b"]


def test_recovery_rejects_tampered_temporary_file(tmp_path: Path) -> None:
    temporary_name = ".frame-sort-recovery-00000000.tmp"
    folder, journal_path, _originals = _create_recovery_case(
        tmp_path,
        "phase1",
        {temporary_name: "a", "b.jpg": "b"},
    )
    temporary = folder / temporary_name
    payload = bytearray(temporary.read_bytes())
    payload[0] ^= 1
    temporary.write_bytes(payload)

    with pytest.raises(TransactionError, match="Содержимое не совпадает"):
        renamer.recover_transaction(folder, journal_path)

    assert temporary.read_bytes() == bytes(payload)
    assert not (folder / "a.jpg").exists()


def _create_recovery_case(
    tmp_path: Path,
    status: str,
    locations: dict[str, str],
) -> tuple[Path, Path, dict[str, bytes]]:
    folder = tmp_path / f"recovery-{status}"
    folder.mkdir()
    originals = {"a": b"source-a", "b": b"source-b"}
    for filename, content_key in locations.items():
        (folder / filename).write_bytes(originals[content_key])
    state_dir = folder / ".frame-sort-state"
    state_dir.mkdir()
    entries = [
        {
            "source": "a.jpg",
            "temporary": ".frame-sort-recovery-00000000.tmp",
            "target": "b.jpg",
            "size": len(originals["a"]),
            "sha256": hashlib.sha256(originals["a"]).hexdigest(),
        },
        {
            "source": "b.jpg",
            "temporary": ".frame-sort-recovery-00000001.tmp",
            "target": "a.jpg",
            "size": len(originals["b"]),
            "sha256": hashlib.sha256(originals["b"]).hexdigest(),
        },
    ]
    journal_path = state_dir / "transaction-recovery.json"
    renamer.atomic_write_json(
        journal_path,
        {
            "schema_version": 1,
            "transaction_id": "recovery",
            "status": status,
            "folder": str(folder),
            "entries": entries,
        },
    )
    return folder, journal_path, originals
