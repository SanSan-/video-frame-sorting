"""Проверка CSV и транзакционное переименование исходных кадров."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, NoReturn

from filelock import FileLock, Timeout

from frame_sorter.exceptions import TransactionError, ValidationError
from frame_sorter.io_utils import (
    STATE_DIRECTORY_NAME,
    atomic_write_json,
    atomic_write_text,
    file_sha256,
    frame_content_hashes,
    frame_snapshot,
    metadata_path_for,
    natural_name_key,
    snapshot_digest,
    validate_frames_directory,
)
from frame_sorter.models import FrameSequence, RenamePreview, RenameResult

ProgressCallback = Callable[[dict[str, Any]], None]
LOCK_FILE_NAME = ".frame-sort.lock"
ROLLBACK_PHASES = frozenset(
    {
        "prepared",
        "phase1",
        "phase2",
        "verifying",
        "completed",
        "rollback_targets",
        "rollback_sources",
    }
)
TARGET_ROLLBACK_PHASES = frozenset(
    {"phase2", "verifying", "completed", "rollback_targets"}
)


def preview_rename(folder: str | Path, csv_path: str | Path) -> RenamePreview:
    """Полностью проверяет план без изменения исходных файлов."""
    sequence, plan, metadata = _validated_plan(folder, csv_path)
    changes = [(source, target) for source, target in plan if source != target]
    return RenamePreview(
        folder=sequence.folder,
        csv_path=_resolve_csv(csv_path),
        plan_id=str(metadata["plan_id"]),
        frame_count=len(plan),
        rename_count=len(changes),
        first_changes=tuple(changes[:10]),
    )


def apply_rename(
    folder: str | Path,
    csv_path: str | Path,
    *,
    emit_event: ProgressCallback | None = None,
) -> RenameResult:
    """Применяет проверенный план в две фазы с журналом и откатом."""
    root = _resolve_folder(folder)
    lock = FileLock(str(root / LOCK_FILE_NAME), timeout=0)
    try:
        with lock:
            return _apply_locked(root, csv_path, emit_event=emit_event)
    except Timeout as exc:
        raise TransactionError(f"Каталог уже переименовывается другим процессом: {root}") from exc


def recover_transaction(folder: str | Path, journal_path: str | Path | None = None) -> Path:
    """Проверяемо откатывает последнюю незавершённую транзакцию."""
    root = _resolve_folder(folder)
    selected = Path(journal_path).expanduser() if journal_path is not None else None
    lock = FileLock(str(root / LOCK_FILE_NAME), timeout=0)
    try:
        with lock:
            journal = _find_recovery_journal(root, selected)
            data = _read_json(journal)
            entries = _journal_entries(root, data)
            status = str(data.get("status", ""))
            if status in {"completed", "rolled_back"}:
                raise TransactionError(f"Транзакция уже завершена: {journal}")
            _rollback_transaction(journal, data, entries, status)
            data["recovered"] = True
            atomic_write_json(journal, data)
            return journal
    except Timeout as exc:
        raise TransactionError(f"Каталог уже занят другим процессом: {root}") from exc


def _apply_locked(
    folder: Path,
    csv_path: str | Path,
    *,
    emit_event: ProgressCallback | None,
) -> RenameResult:
    sequence, plan, metadata = _validated_plan(folder, csv_path)
    changes = [(source, target) for source, target in plan if source != target]
    transaction_id = uuid.uuid4().hex
    journal_path, journal, entries = _prepare_transaction(
        folder,
        sequence,
        changes,
        metadata,
        csv_path,
        transaction_id,
    )
    try:
        undo_csv = _execute_transaction(
            folder,
            sequence,
            plan,
            metadata,
            transaction_id,
            journal_path,
            journal,
            entries,
            emit_event,
        )
    except KeyboardInterrupt as exc:
        _rollback_after_failure(folder, journal_path, journal, exc)
        raise
    except Exception as exc:
        rollback_error = _rollback_after_failure(folder, journal_path, journal, exc)
        _raise_apply_failure(exc, rollback_error, journal_path)
    _emit(
        emit_event,
        phase="completed",
        processed=len(entries),
        total=len(entries),
        undo_csv_path=str(undo_csv),
        message=f"Переименование завершено. Обратный CSV: {undo_csv}",
    )
    return RenameResult(
        folder=folder,
        transaction_id=transaction_id,
        renamed_count=len(entries),
        journal_path=journal_path,
        undo_csv_path=undo_csv,
    )


def _prepare_transaction(
    folder: Path,
    sequence: FrameSequence,
    changes: list[tuple[str, str]],
    metadata: dict[str, Any],
    csv_path: str | Path,
    transaction_id: str,
) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    state_dir = folder / STATE_DIRECTORY_NAME
    state_dir.mkdir(parents=True, exist_ok=True)
    journal_path = state_dir / f"transaction-{transaction_id}.json"
    sizes = {frame.name: frame.size for frame in sequence.frames}
    hashes = {
        str(item["name"]): str(item["sha256"])
        for item in metadata["snapshot"]
    }
    entries = [
        {
            "source": source,
            "temporary": f".frame-sort-{transaction_id}-{position:08d}.tmp",
            "target": target,
            "size": sizes[source],
            "sha256": hashes[source],
        }
        for position, (source, target) in enumerate(changes)
    ]
    _ensure_temporary_names_available(folder, entries)
    journal: dict[str, Any] = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "status": "prepared",
        "folder": str(folder),
        "plan_id": metadata["plan_id"],
        "csv_path": str(_resolve_csv(csv_path)),
        "entries": entries,
    }
    atomic_write_json(journal_path, journal)
    return journal_path, journal, entries


def _ensure_temporary_names_available(
    folder: Path, entries: list[dict[str, Any]]
) -> None:
    for entry in entries:
        temporary = folder / str(entry["temporary"])
        if temporary.exists():
            raise TransactionError(f"Временное имя уже занято: {temporary}")


def _execute_transaction(
    folder: Path,
    sequence: FrameSequence,
    plan: list[tuple[str, str]],
    metadata: dict[str, Any],
    transaction_id: str,
    journal_path: Path,
    journal: dict[str, Any],
    entries: list[dict[str, Any]],
    emit_event: ProgressCallback | None,
) -> Path:
    _run_rename_phase(
        folder,
        journal_path,
        journal,
        entries,
        phase="phase1",
        source_key="source",
        target_key="temporary",
        emit_event=emit_event,
        emit_start=True,
    )
    _run_rename_phase(
        folder,
        journal_path,
        journal,
        entries,
        phase="phase2",
        source_key="temporary",
        target_key="target",
        emit_event=emit_event,
        emit_start=False,
    )
    _start_phase(journal_path, journal, "verifying")
    _verify_targets(folder, entries, expected_count=len(sequence.frames))
    undo_csv = _write_undo_plan(
        sequence,
        plan,
        metadata,
        transaction_id=transaction_id,
        folder=folder,
    )
    journal["undo_csv_path"] = str(undo_csv)
    _set_status(journal_path, journal, "completed")
    return undo_csv


def _run_rename_phase(
    folder: Path,
    journal_path: Path,
    journal: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    phase: str,
    source_key: str,
    target_key: str,
    emit_event: ProgressCallback | None,
    emit_start: bool,
) -> None:
    _start_phase(journal_path, journal, phase)
    if emit_start:
        _emit(emit_event, phase=phase, processed=0, total=len(entries))
    for index, entry in enumerate(entries, start=1):
        _replace(folder / str(entry[source_key]), folder / str(entry[target_key]))
        if index % 128 == 0 or index == len(entries):
            _emit(emit_event, phase=phase, processed=index, total=len(entries))


def _start_phase(path: Path, journal: dict[str, Any], phase: str) -> None:
    journal["original_phase"] = phase
    _set_status(path, journal, phase)


def _rollback_after_failure(
    folder: Path,
    journal_path: Path,
    journal: dict[str, Any],
    error: BaseException,
) -> Exception | None:
    try:
        _rollback_transaction(
            journal_path,
            journal,
            _journal_entries(folder, journal),
            str(journal.get("status", "prepared")),
        )
        journal["error"] = str(error)
        atomic_write_json(journal_path, journal)
    except Exception as rollback_error:
        journal["error"] = str(error)
        journal["rollback_error"] = str(rollback_error)
        _write_journal_best_effort(journal_path, journal)
        return rollback_error
    return None


def _write_journal_best_effort(path: Path, journal: dict[str, Any]) -> None:
    try:
        atomic_write_json(path, journal)
    except Exception:
        return


def _raise_apply_failure(
    error: Exception,
    rollback_error: Exception | None,
    journal_path: Path,
) -> NoReturn:
    if rollback_error is not None:
        raise TransactionError(
            "Переименование и автоматический откат завершились ошибкой. "
            f"Сохранён журнал восстановления: {journal_path}. "
            f"Ошибка отката: {rollback_error}"
        ) from error
    raise TransactionError(
        f"Переименование отменено, исходные имена восстановлены: {error}"
    ) from error


def _validated_plan(
    folder: str | Path, csv_path: str | Path
) -> tuple[FrameSequence, list[tuple[str, str]], dict[str, Any]]:
    sequence = validate_frames_directory(folder)
    plan_path = _resolve_csv(csv_path)
    metadata = _validated_metadata(sequence, plan_path)
    target_names = _validated_target_names(metadata, len(sequence.frames))
    plan = _validated_csv_plan(sequence, plan_path, target_names)
    return sequence, plan, metadata


def _validated_metadata(sequence: FrameSequence, plan_path: Path) -> dict[str, Any]:
    metadata_path = metadata_path_for(plan_path)
    if not metadata_path.is_file():
        raise ValidationError(f"Не найдены метаданные плана: {metadata_path}")
    metadata = _read_json(metadata_path)
    if metadata.get("schema_version") != 1:
        raise ValidationError("Версия метаданных плана не поддерживается.")
    if not isinstance(metadata.get("plan_id"), str) or not metadata["plan_id"]:
        raise ValidationError("Метаданные не содержат корректный plan_id.")
    if metadata.get("folder") != str(sequence.folder):
        raise ValidationError("CSV был создан для другого каталога.")
    if metadata.get("csv_sha256") != file_sha256(plan_path):
        raise ValidationError("CSV изменился после анализа; создайте новый план.")
    current_snapshot = frame_snapshot(sequence, frame_content_hashes(sequence))
    if metadata.get("snapshot_sha256") != snapshot_digest(current_snapshot):
        raise ValidationError("Каталог кадров изменился после создания CSV.")
    if metadata.get("snapshot") != current_snapshot:
        raise ValidationError("Снимок каталога не совпадает с метаданными CSV.")
    if metadata.get("frame_count") != len(sequence.frames):
        raise ValidationError("Число кадров не совпадает с метаданными CSV.")
    return metadata


def _validated_target_names(
    metadata: dict[str, Any], frame_count: int
) -> list[str]:
    target_names = metadata.get("target_names")
    if not isinstance(target_names, list) or len(target_names) != frame_count:
        raise ValidationError("Метаданные не содержат полный список итоговых имён.")
    if any(
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or Path(name).suffix.casefold() not in {".jpg", ".jpeg"}
        for name in target_names
    ):
        raise ValidationError("Метаданные содержат небезопасное итоговое имя.")
    if len({name.casefold() for name in target_names}) != len(target_names):
        raise ValidationError("Итоговые имена должны быть уникальны без учёта регистра.")
    return target_names


def _validated_csv_plan(
    sequence: FrameSequence,
    plan_path: Path,
    target_names: list[str],
) -> list[tuple[str, str]]:
    rows = _read_csv_rows(plan_path)
    expected_positions = set(range(len(sequence.frames)))
    positions = {position for position, _name in rows}
    if positions != expected_positions or len(rows) != len(sequence.frames):
        raise ValidationError("Позиции CSV должны идти от 0 без пропусков и повторов.")
    source_names = [name for _position, name in rows]
    if len(set(source_names)) != len(source_names):
        raise ValidationError("Каждый исходный файл должен встречаться в CSV один раз.")
    current_names = {frame.name for frame in sequence.frames}
    if set(source_names) != current_names:
        raise ValidationError("Набор исходных имён CSV не совпадает с каталогом.")
    ordered_rows = sorted(rows, key=lambda item: item[0])
    plan = [(source, target_names[position]) for position, source in ordered_rows]
    targets = [target for _source, target in plan]
    if len({target.casefold() for target in targets}) != len(targets):
        raise ValidationError("Итоговые имена не образуют безопасную биекцию каталога.")
    _ensure_targets_available(sequence.folder, current_names, targets)
    return plan


def _ensure_targets_available(
    folder: Path, current_names: set[str], targets: list[str]
) -> None:
    source_keys = {name.casefold() for name in current_names}
    for target in targets:
        target_path = folder / target
        if target_path.exists() and target.casefold() not in source_keys:
            raise ValidationError(f"Итоговое имя уже занято чужим файлом: {target}")


def _read_csv_rows(path: Path) -> list[tuple[int, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != ["position", "source_filename"]:
                raise ValidationError(
                    "CSV должен содержать только position,source_filename."
                )
            rows: list[tuple[int, str]] = []
            for number, row in enumerate(reader, start=2):
                try:
                    position = int(row["position"])
                except (TypeError, ValueError) as exc:
                    raise ValidationError(
                        f"Некорректная позиция CSV в строке {number}."
                    ) from exc
                name = row["source_filename"]
                if None in row:
                    raise ValidationError(
                        f"Лишнее поле CSV в строке {number}."
                    )
                if not name or Path(name).name != name or name in {".", ".."}:
                    raise ValidationError(
                        f"Некорректное имя файла CSV в строке {number}."
                    )
                rows.append((position, name))
    except UnicodeError as exc:
        raise ValidationError(f"CSV должен быть UTF-8 без BOM: {path}") from exc
    except OSError as exc:
        raise ValidationError(f"Не удалось прочитать CSV {path}: {exc}") from exc
    return rows


def _write_undo_plan(
    sequence: FrameSequence,
    plan: list[tuple[str, str]],
    source_metadata: dict[str, Any],
    *,
    transaction_id: str,
    folder: Path,
) -> Path:
    target_for_source = dict(plan)
    rows = [(frame.index, target_for_source[frame.name]) for frame in sequence.frames]
    undo_csv = folder / f"frame-sort-undo-{transaction_id}.csv"
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("position", "source_filename"))
    writer.writerows(rows)
    atomic_write_text(undo_csv, output.getvalue())
    source_hashes = {
        str(item["name"]): str(item["sha256"])
        for item in source_metadata["snapshot"]
    }
    hash_by_target = {
        target: source_hashes[source]
        for source, target in plan
    }
    current_snapshot: list[dict[str, int | str]] = []
    final_names = sorted(
        (target for _source, target in plan),
        key=lambda name: natural_name_key(name),
    )
    for name in final_names:
        path = folder / name
        stat = path.stat()
        current_snapshot.append(
            {
                "name": name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": hash_by_target[name],
            }
        )
    payload = {
        "kind": "undo",
        "source_transaction_id": transaction_id,
        "snapshot_sha256": snapshot_digest(current_snapshot),
        "rows": rows,
    }
    plan_id = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    metadata = {
        "schema_version": 1,
        "algorithm_version": "undo-v1",
        "plan_id": plan_id,
        "folder": str(folder),
        "naming_mode": "undo-original-names",
        "target_names": [frame.name for frame in sequence.frames],
        "frame_count": len(sequence.frames),
        "csv_sha256": file_sha256(undo_csv),
        "snapshot_sha256": snapshot_digest(current_snapshot),
        "snapshot": current_snapshot,
        "settings": {"kind": "undo"},
        "summary": {"moved_count": sum(source != target for source, target in plan)},
    }
    atomic_write_json(metadata_path_for(undo_csv), metadata)
    return undo_csv


def _verify_targets(folder: Path, entries: list[dict[str, Any]], expected_count: int) -> None:
    jpeg_count = sum(
        1
        for item in os.scandir(folder)
        if item.is_file(follow_symlinks=False)
        and Path(item.name).suffix.casefold() in {".jpg", ".jpeg"}
    )
    if jpeg_count != expected_count:
        raise TransactionError(
            f"После переименования ожидалось {expected_count} JPEG, найдено {jpeg_count}."
        )
    for entry in entries:
        temporary = folder / str(entry["temporary"])
        target = folder / str(entry["target"])
        if temporary.exists() or not target.is_file():
            raise TransactionError(f"Не завершено итоговое имя: {target.name}")
        if target.stat().st_size != int(entry["size"]):
            raise TransactionError(f"Изменился размер файла: {target.name}")
        if file_sha256(target) != str(entry["sha256"]):
            raise TransactionError(f"Изменилось содержимое файла: {target.name}")


def _rollback_transaction(
    journal_path: Path,
    journal: dict[str, Any],
    entries: list[dict[str, Any]],
    phase: str,
) -> None:
    """Идемпотентно откатывает транзакцию с устойчивой границей двух фаз."""
    _validate_rollback_phase(phase)
    if phase in TARGET_ROLLBACK_PHASES:
        _set_status(journal_path, journal, "rollback_targets")
        _restore_targets_to_temporary(entries)
    _set_status(journal_path, journal, "rollback_sources")
    _restore_sources(entries)
    _verify_restored_sources(entries)
    _set_status(journal_path, journal, "rolled_back")


def _validate_rollback_phase(phase: str) -> None:
    if phase not in ROLLBACK_PHASES:
        raise TransactionError(f"Журнал содержит неизвестный статус: {phase}")


def _restore_targets_to_temporary(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        _restore_target_entry(entry)


def _restore_target_entry(entry: dict[str, Any]) -> None:
    target = Path(entry["target_path"])
    temporary = Path(entry["temporary_path"])
    if target.exists() and temporary.exists():
        raise TransactionError(
            f"Одновременно существуют итоговое и временное имя: {target.name}"
        )
    if target.exists():
        _verify_journal_file(target, entry)
        _replace(target, temporary)
        return
    if temporary.exists():
        _verify_journal_file(temporary, entry)
        return
    raise TransactionError(f"Не найден файл транзакции для отката: {target.name}")


def _restore_sources(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        _restore_source_entry(entry)


def _restore_source_entry(entry: dict[str, Any]) -> None:
    source = Path(entry["source_path"])
    temporary = Path(entry["temporary_path"])
    if temporary.exists():
        if source.exists():
            raise TransactionError(f"Откат столкнулся с занятым именем: {source}")
        _verify_journal_file(temporary, entry)
        _replace(temporary, source)
        return
    if source.exists():
        _verify_journal_file(source, entry)
        return
    raise TransactionError(
        f"Не найден файл транзакции для восстановления: {source.name}"
    )


def _verify_restored_sources(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        source = Path(entry["source_path"])
        temporary = Path(entry["temporary_path"])
        if not source.is_file() or temporary.exists():
            raise TransactionError(f"Откат не восстановил исходное имя: {source.name}")
        _verify_journal_file(source, entry)


def _journal_entries(folder: Path, journal: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    raw_entries = journal.get("entries")
    if not isinstance(raw_entries, list):
        raise TransactionError("Журнал не содержит список операций.")
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise TransactionError("Журнал содержит некорректную операцию.")
        names = [raw.get(key) for key in ("source", "temporary", "target")]
        if any(not isinstance(name, str) or Path(name).name != name for name in names):
            raise TransactionError("Журнал содержит небезопасное имя файла.")
        entries.append(
            {
                **raw,
                "source_path": folder / str(raw["source"]),
                "temporary_path": folder / str(raw["temporary"]),
                "target_path": folder / str(raw["target"]),
            }
        )
    return entries


def _find_recovery_journal(folder: Path, selected: Path | None) -> Path:
    state_dir = folder / STATE_DIRECTORY_NAME
    if selected is not None:
        path = selected.resolve(strict=True)
        if path.parent != state_dir.resolve(strict=True):
            raise TransactionError("Журнал восстановления находится вне служебного каталога.")
        return path
    candidates: list[Path] = []
    for path in sorted(state_dir.glob("transaction-*.json"), reverse=True):
        data = _read_json(path)
        if data.get("status") not in {"completed", "rolled_back"}:
            candidates.append(path)
    if len(candidates) != 1:
        raise TransactionError(
            "Для автоматического восстановления должен существовать ровно один "
            f"незавершённый журнал; найдено: {len(candidates)}."
        )
    return candidates[0]


def _set_status(path: Path, journal: dict[str, Any], status: str) -> None:
    journal["status"] = status
    atomic_write_json(path, journal)


def _resolve_folder(folder: str | Path) -> Path:
    try:
        path = Path(folder).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"Каталог не найден: {folder}") from exc
    if not path.is_dir():
        raise ValidationError(f"Ожидался каталог: {path}")
    return path


def _resolve_csv(csv_path: str | Path) -> Path:
    try:
        path = Path(csv_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"CSV не найден: {csv_path}") from exc
    if not path.is_file() or path.suffix.casefold() != ".csv":
        raise ValidationError(f"Ожидался CSV-файл: {path}")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Не удалось прочитать JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"Ожидался объект JSON: {path}")
    return data


def _replace(source: Path, target: Path) -> None:
    """Переименовывает без перезаписи и служит точкой внедрения отказов в тестах."""
    if target.exists():
        raise TransactionError(f"Имя неожиданно оказалось занято: {target}")
    try:
        os.rename(source, target)
    except OSError as exc:
        if target.exists():
            raise TransactionError(f"Имя неожиданно оказалось занято: {target}") from exc
        raise


def _verify_journal_file(path: Path, entry: dict[str, Any]) -> None:
    """Не принимает посторонний файл за часть восстанавливаемой транзакции."""
    if not path.is_file() or path.stat().st_size != int(entry["size"]):
        raise TransactionError(f"Файл транзакции не совпадает с журналом: {path.name}")
    if file_sha256(path) != str(entry["sha256"]):
        raise TransactionError(f"Содержимое не совпадает с журналом: {path.name}")


def _emit(callback: ProgressCallback | None, **event: Any) -> None:
    if callback is None:
        return
    try:
        callback(dict(event))
    except Exception:
        # Диагностический callback не является частью файловой транзакции.
        return


__all__ = ["apply_rename", "preview_rename", "recover_transaction"]
