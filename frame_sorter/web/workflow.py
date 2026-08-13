"""Лёгкое восстановление этапов веб-процесса по файловым артефактам."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frame_sorter.analysis import ALGORITHM_VERSION
from frame_sorter.io_utils import STATE_DIRECTORY_NAME, metadata_path_for

PLAN_CSV_NAME = "frame-sort-plan.csv"
UNDO_CSV_GLOB = "frame-sort-undo-*.csv"
UNDO_META_GLOB = "frame-sort-undo-*.meta.json"
PLAN_ARTIFACT_LABEL = "таблицы сортировки"
CACHE_CANDIDATE_DETAIL = "Кандидат кеша найден; полная проверка при запуске."


class WorkflowArtifactError(ValueError):
    """Артефакт этапа отсутствует или находится вне выбранного каталога."""


@dataclass(frozen=True)
class _ArtifactCandidate:
    csv_path: Path
    metadata_path: Path
    journal_path: Path | None = None


@dataclass(frozen=True)
class _TransactionRecord:
    kind: str
    transaction_id: str
    journal_path: Path
    csv_path: Path
    undo_csv_path: Path
    mtime_ns: int


def inspect_folder_artifacts(folder: Path) -> dict[str, Any]:
    """Находит кандидаты кеша, не сканируя и не открывая JPEG."""
    root = _resolve_folder(folder)
    warnings: list[str] = []
    plan, undo, transaction, blocked, plan_is_newer = _classify_artifacts(
        root,
        warnings,
    )
    stages, actions = _workflow_presentation(
        plan,
        undo,
        transaction,
        blocked,
        plan_is_newer,
    )
    return {
        "folder": str(root),
        "stages": stages,
        "actions": actions,
        "artifacts": _artifact_paths(plan, undo),
        "warnings": warnings,
    }


def _classify_artifacts(
    root: Path,
    warnings: list[str],
) -> tuple[
    _ArtifactCandidate | None,
    _ArtifactCandidate | None,
    _TransactionRecord | None,
    bool,
    bool,
]:
    plan = _inspect_pair(root, root / PLAN_CSV_NAME, PLAN_ARTIFACT_LABEL, warnings)
    transaction, blocked = _transaction_state(root, warnings)
    plan_is_newer = (
        plan is not None
        and transaction is not None
        and _candidate_mtime_ns(plan) > transaction.mtime_ns
    )
    undo: _ArtifactCandidate | None = None
    if blocked:
        plan = None
    elif transaction is not None and not plan_is_newer:
        plan = None
        if transaction.kind == "sort":
            undo = _candidate_from_transaction(root, transaction, warnings)
        else:
            warnings.append(
                "Последняя завершённая транзакция восстановила исходные имена; "
                "требуется новый анализ."
            )
    else:
        _warn_orphan_undo_pairs(root, warnings)

    if transaction is not None and transaction.kind == "undo" and not plan_is_newer:
        warnings.append(
            "Обратные CSV прошлых поколений не считаются текущим состоянием."
        )
    return plan, undo, transaction, blocked, plan_is_newer


def _workflow_presentation(
    plan: _ArtifactCandidate | None,
    undo: _ArtifactCandidate | None,
    transaction: _TransactionRecord | None,
    blocked: bool,
    plan_is_newer: bool,
) -> tuple[dict[str, dict[str, Any]], dict[str, bool]]:
    stages = _empty_stages()
    actions = {
        "analyze": not blocked,
        "rename": False,
        "undo": False,
        "rebuild": False,
    }
    if blocked:
        stages = _blocked_stages()
    elif (
        transaction is not None
        and transaction.kind == "sort"
        and undo is None
        and not plan_is_newer
    ):
        stages = _blocked_stages(
            "Последняя сортировка не подтверждена полной парой обратного CSV."
        )
        actions["analyze"] = False
    elif undo is not None:
        stages = {
            "analysis": _stage(
                "completed",
                cached=True,
                detail=CACHE_CANDIDATE_DETAIL,
            ),
            "rename": _stage(
                "completed",
                cached=True,
                detail=CACHE_CANDIDATE_DETAIL,
            ),
            "rebuild": _stage(
                "ready",
                cached=False,
                detail="Отсортированные кадры можно собрать в MP4.",
            ),
        }
        actions = {
            "analyze": False,
            "rename": False,
            "undo": True,
            "rebuild": True,
        }
    elif plan is not None:
        stages = {
            "analysis": _stage(
                "completed",
                cached=True,
                detail=CACHE_CANDIDATE_DETAIL,
            ),
            "rename": _stage(
                "ready",
                cached=False,
                detail="Полная проверка CSV и SHA кадров выполняется при запуске.",
            ),
            "rebuild": _stage(
                "blocked",
                cached=False,
                detail="Сначала требуется переименовать кадры.",
            ),
        }
        actions = {
            "analyze": False,
            "rename": True,
            "undo": False,
            "rebuild": False,
        }
    return stages, actions


def _artifact_paths(
    plan: _ArtifactCandidate | None,
    undo: _ArtifactCandidate | None,
) -> dict[str, str | None]:
    return {
        "plan_csv": str(plan.csv_path) if plan else None,
        "plan_meta": str(plan.metadata_path) if plan else None,
        "undo_csv": str(undo.csv_path) if undo else None,
        "undo_meta": str(undo.metadata_path) if undo else None,
        "journal": str(undo.journal_path) if undo and undo.journal_path else None,
    }


def resolve_cached_plan_for_rename(
    folder: Path,
    requested_csv: Path | None = None,
) -> Path:
    """Возвращает безопасный путь полной пары плана для строгого preview."""
    root = _resolve_folder(folder)
    csv_path = _resolve_artifact_path(root, requested_csv or root / PLAN_CSV_NAME)
    if csv_path.name != PLAN_CSV_NAME:
        raise WorkflowArtifactError(
            f"Ожидался кеш таблицы сортировки {PLAN_CSV_NAME}."
        )
    _require_complete_pair(csv_path, PLAN_ARTIFACT_LABEL)
    return csv_path


def require_rebuild_cache(folder: Path) -> Path:
    """Требует undo CSV последней завершённой сортировки."""
    root = _resolve_folder(folder)
    warnings: list[str] = []
    transaction, blocked = _transaction_state(root, warnings)
    if blocked:
        raise WorkflowArtifactError(
            "Обнаружена незавершённая транзакция; сначала требуется восстановление."
        )
    if transaction is None or transaction.kind != "sort":
        raise WorkflowArtifactError(
            "Нет завершённой сортировки, которую подтверждает обратный CSV."
        )
    plan = _inspect_pair(root, root / PLAN_CSV_NAME, PLAN_ARTIFACT_LABEL, [])
    if plan is not None and _candidate_mtime_ns(plan) > transaction.mtime_ns:
        raise WorkflowArtifactError(
            "После последней транзакции создан новый план; сначала примените его."
        )
    candidate = _candidate_from_transaction(root, transaction, warnings)
    if candidate is None:
        raise WorkflowArtifactError(
            "Последняя сортировка не подтверждена своей полной парой обратного CSV."
        )
    return candidate.csv_path


def require_undo_cache(folder: Path) -> Path:
    """Требует актуальный обратный CSV для восстановления исходных имён."""
    return require_rebuild_cache(folder)


def _empty_stages() -> dict[str, dict[str, Any]]:
    return {
        "analysis": _stage(
            "ready",
            cached=False,
            detail="Можно создать таблицу сортировки.",
        ),
        "rename": _stage(
            "blocked",
            cached=False,
            detail="Сначала требуется таблица сортировки.",
        ),
        "rebuild": _stage(
            "blocked",
            cached=False,
            detail="Сначала требуется переименовать кадры.",
        ),
    }


def _blocked_stages(
    detail: str = "Сначала требуется восстановить незавершённую транзакцию.",
) -> dict[str, dict[str, Any]]:
    return {
        "analysis": _stage("blocked", cached=False, detail=detail),
        "rename": _stage("blocked", cached=False, detail=detail),
        "rebuild": _stage("blocked", cached=False, detail=detail),
    }


def _stage(state: str, *, cached: bool, detail: str) -> dict[str, Any]:
    return {"state": state, "cached": cached, "detail": detail}


def _resolve_folder(folder: Path) -> Path:
    try:
        root = Path(folder).expanduser().resolve(strict=True)
    except OSError as exc:
        raise WorkflowArtifactError(f"Каталог не найден: {folder}") from exc
    if not root.is_dir():
        raise WorkflowArtifactError(f"Ожидался каталог: {root}")
    return root


def _resolve_artifact_path(root: Path, path: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkflowArtifactError(f"CSV не найден: {path}") from exc
    if (
        resolved.parent != root
        or resolved.suffix.casefold() != ".csv"
        or resolved.is_symlink()
        or not resolved.is_file()
    ):
        raise WorkflowArtifactError(
            "Кеш CSV должен быть обычным файлом в выбранном каталоге."
        )
    return resolved


def _inspect_pair(
    root: Path,
    csv_path: Path,
    label: str,
    warnings: list[str],
) -> _ArtifactCandidate | None:
    metadata_path = metadata_path_for(csv_path)
    csv_exists = _is_regular_file(csv_path)
    metadata_exists = _is_regular_file(metadata_path)
    if not csv_exists and not metadata_exists:
        return None
    if csv_exists != metadata_exists:
        missing = metadata_path.name if csv_exists else csv_path.name
        warnings.append(f"Неполная пара {label}: отсутствует {missing}.")
        return None
    metadata = _diagnose_metadata(root, metadata_path, warnings)
    if (
        csv_path.name == PLAN_CSV_NAME
        and metadata is not None
        and isinstance(metadata.get("algorithm_version"), str)
        and metadata.get("algorithm_version") != ALGORITHM_VERSION
    ):
        warnings.append(
            f"План {csv_path.name} создан алгоритмом "
            f"{metadata.get('algorithm_version')!r}; требуется {ALGORITHM_VERSION}."
        )
        return None
    return _ArtifactCandidate(csv_path, metadata_path)


def _candidate_from_transaction(
    root: Path,
    transaction: _TransactionRecord,
    warnings: list[str],
) -> _ArtifactCandidate | None:
    expected = root / f"frame-sort-undo-{transaction.transaction_id}.csv"
    if transaction.undo_csv_path != expected:
        warnings.append(
            f"Журнал {transaction.journal_path.name} указывает неожиданный обратный CSV."
        )
        return None
    candidate = _inspect_pair(root, expected, "обратного CSV", warnings)
    if candidate is None:
        return None
    return _ArtifactCandidate(
        candidate.csv_path,
        candidate.metadata_path,
        transaction.journal_path,
    )


def _warn_orphan_undo_pairs(root: Path, warnings: list[str]) -> None:
    csv_paths = set(root.glob(UNDO_CSV_GLOB))
    for metadata_path in root.glob(UNDO_META_GLOB):
        csv_name = metadata_path.name.removesuffix(".meta.json") + ".csv"
        csv_paths.add(root / csv_name)
    for csv_path in sorted(csv_paths, key=_safe_mtime_ns, reverse=True):
        candidate = _inspect_pair(root, csv_path, "обратного CSV", warnings)
        if candidate is not None:
            warnings.append(
                f"{csv_path.name} проигнорирован: нет соответствующей "
                "завершённой транзакции сортировки."
            )


def _require_complete_pair(csv_path: Path, label: str) -> None:
    metadata_path = metadata_path_for(csv_path)
    if not _is_regular_file(csv_path) or not _is_regular_file(metadata_path):
        raise WorkflowArtifactError(f"Полная пара {label} не найдена.")


def _diagnose_metadata(
    root: Path,
    metadata_path: Path,
    warnings: list[str],
) -> dict[str, Any] | None:
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        warnings.append(
            f"Метаданные {metadata_path.name} будут проверены при запуске: {exc}"
        )
        return None
    if not isinstance(value, dict):
        warnings.append(
            f"Метаданные {metadata_path.name} не являются объектом JSON."
        )
        return None
    artifact_folder = value.get("folder")
    if isinstance(artifact_folder, str) and not _same_path(artifact_folder, root):
        warnings.append(
            f"Метаданные {metadata_path.name} указывают другой каталог; "
            "полная проверка будет выполнена при запуске."
        )
    return value


def _transaction_state(
    root: Path,
    warnings: list[str],
) -> tuple[_TransactionRecord | None, bool]:
    state_dir = root / STATE_DIRECTORY_NAME
    if not state_dir.is_dir():
        return None, False
    completed: list[_TransactionRecord] = []
    blocked = False
    for journal_path in state_dir.glob("transaction-*.json"):
        try:
            value = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            warnings.append(
                f"Журнал {journal_path.name} не удалось классифицировать: {exc}"
            )
            blocked = True
            continue
        if not isinstance(value, dict):
            warnings.append(f"Журнал {journal_path.name} не является объектом JSON.")
            blocked = True
            continue
        status = value.get("status")
        if status == "rolled_back":
            continue
        if status != "completed":
            warnings.append(
                f"Обнаружена незавершённая транзакция: {journal_path.name}."
            )
            blocked = True
            continue
        record = _parse_completed_transaction(root, journal_path, value, warnings)
        if record is None:
            blocked = True
        else:
            completed.append(record)
    latest = max(
        completed,
        key=lambda item: (item.mtime_ns, item.journal_path.name),
        default=None,
    )
    return latest, blocked


def _parse_completed_transaction(
    root: Path,
    journal_path: Path,
    value: dict[str, Any],
    warnings: list[str],
) -> _TransactionRecord | None:
    transaction_id = value.get("transaction_id")
    if (
        not isinstance(transaction_id, str)
        or not transaction_id
        or journal_path.name != f"transaction-{transaction_id}.json"
        or not _same_path(value.get("folder"), root)
    ):
        warnings.append(
            f"Журнал {journal_path.name} содержит несогласованные реквизиты транзакции."
        )
        return None
    try:
        csv_path = _record_csv_path(root, value.get("csv_path"))
        undo_csv_path = _record_csv_path(root, value.get("undo_csv_path"))
    except WorkflowArtifactError as exc:
        warnings.append(
            f"Журнал {journal_path.name} содержит небезопасный путь: {exc}"
        )
        return None
    kind = "undo" if csv_path.name.startswith("frame-sort-undo-") else "sort"
    return _TransactionRecord(
        kind=kind,
        transaction_id=transaction_id,
        journal_path=journal_path,
        csv_path=csv_path,
        undo_csv_path=undo_csv_path,
        mtime_ns=_safe_mtime_ns(journal_path),
    )


def _record_csv_path(
    root: Path,
    value: Any,
) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise WorkflowArtifactError("ожидался абсолютный путь CSV")
    path = Path(value).expanduser().resolve(strict=False)
    if path.parent != root or path.suffix.casefold() != ".csv":
        raise WorkflowArtifactError("CSV находится вне выбранного каталога")
    return path


def _candidate_mtime_ns(candidate: _ArtifactCandidate) -> int:
    return max(
        _safe_mtime_ns(candidate.csv_path),
        _safe_mtime_ns(candidate.metadata_path),
    )


def _same_path(value: Any, expected: Path) -> bool:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        return False
    try:
        resolved = Path(value).expanduser().resolve(strict=False)
    except OSError:
        return False
    return os.path.normcase(str(resolved)) == os.path.normcase(str(expected))


def _is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _safe_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


__all__ = [
    "WorkflowArtifactError",
    "inspect_folder_artifacts",
    "require_rebuild_cache",
    "require_undo_cache",
    "resolve_cached_plan_for_rename",
]
