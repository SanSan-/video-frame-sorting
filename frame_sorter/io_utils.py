"""Проверка входных кадров и атомарная запись текстовых артефактов."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from frame_sorter.exceptions import TransactionError, ValidationError
from frame_sorter.models import FrameEntry, FrameSequence

FRAME_NAME_RE = re.compile(
    r"^(?P<prefix>.+_)(?P<index>\d+)(?P<extension>\.jpe?g)$",
    re.IGNORECASE,
)
STATE_DIRECTORY_NAME = ".frame-sort-state"
SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg"})
_NATURAL_PART_RE = re.compile(r"(\d+)")


def validate_frames_directory(folder: str | Path) -> FrameSequence:
    """Проверяет один каталог и задаёт естественный исходный порядок JPEG."""
    root = _resolve_frames_root(folder)
    _ensure_no_incomplete_transaction(root)
    paths = _collect_frame_paths(root)
    frames = _build_frame_entries(paths)
    target_names, naming_mode = _build_target_names(paths)
    return FrameSequence(
        folder=root,
        frames=frames,
        target_names=target_names,
        naming_mode=naming_mode,
    )


def _resolve_frames_root(folder: str | Path) -> Path:
    """Разрешает и проверяет путь к каталогу кадров."""
    try:
        root = Path(folder).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"Каталог не найден: {folder}") from exc
    if not root.is_dir():
        raise ValidationError(f"Ожидался каталог с кадрами: {root}")
    return root


def _collect_frame_paths(root: Path) -> list[Path]:
    """Собирает проверенные JPEG без рекурсивного обхода."""
    paths: list[Path] = []
    try:
        with os.scandir(root) as scanner:
            entries = sorted(scanner, key=lambda item: item.name.casefold())
    except OSError as exc:
        raise ValidationError(f"Не удалось прочитать каталог {root}: {exc}") from exc
    for entry in entries:
        suffix = Path(entry.name).suffix.casefold()
        if suffix not in SUPPORTED_EXTENSIONS:
            continue
        _validate_frame_entry(entry)
        paths.append(Path(entry.path))
    if not paths:
        raise ValidationError(f"В каталоге нет кадров JPEG: {root}")
    paths.sort(key=lambda path: natural_name_key(path.name))
    if len({path.name.casefold() for path in paths}) != len(paths):
        raise ValidationError("Имена JPEG должны быть уникальны без учёта регистра.")
    return paths


def _validate_frame_entry(entry: os.DirEntry[str]) -> None:
    """Отвергает ссылки и необычные объекты с расширением JPEG."""
    try:
        if entry.is_symlink():
            raise ValidationError(
                f"Символическая ссылка JPEG не поддерживается: {entry.name}"
            )
        if not entry.is_file(follow_symlinks=False):
            raise ValidationError(f"Ожидался обычный JPEG-файл: {entry.name}")
    except OSError as exc:
        raise ValidationError(f"Не удалось проверить файл {entry.name}: {exc}") from exc


def _build_frame_entries(paths: list[Path]) -> tuple[FrameEntry, ...]:
    """Снимает метаданные каждого кадра в проверенном порядке."""
    frames: list[FrameEntry] = []
    for index, path in enumerate(paths):
        try:
            stat = path.stat()
        except OSError as exc:
            raise ValidationError(f"Не удалось прочитать сведения о {path.name}: {exc}") from exc
        frames.append(
            FrameEntry(index=index, path=path, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        )
    return tuple(frames)


def natural_name_key(name: str) -> tuple[tuple[int, object, int], ...]:
    """Сортирует произвольные имена естественно: `frame2` раньше `frame10`."""
    parts: list[tuple[int, object, int]] = []
    for part in _NATURAL_PART_RE.split(name):
        if not part:
            continue
        if part.isdigit():
            parts.append((1, int(part), len(part)))
        else:
            parts.append((0, part.casefold(), len(part)))
    return tuple(parts)


def _build_target_names(paths: list[Path]) -> tuple[tuple[str, ...], str]:
    """Сохраняет привычный числовой шаблон либо создаёт нейтральные имена."""
    matches = [FRAME_NAME_RE.fullmatch(path.name) for path in paths]
    if all(match is not None for match in matches):
        typed_matches = [match for match in matches if match is not None]
        prefixes = {match.group("prefix") for match in typed_matches}
        widths = {len(match.group("index")) for match in typed_matches}
        extensions = {match.group("extension").casefold() for match in typed_matches}
        if len(prefixes) == len(widths) == len(extensions) == 1:
            prefix = typed_matches[0].group("prefix")
            width = max(len(str(len(paths) - 1)), len(typed_matches[0].group("index")))
            extension = typed_matches[0].group("extension")
            return (
                tuple(f"{prefix}{position:0{width}d}{extension}" for position in range(len(paths))),
                "preserved-numeric-pattern",
            )
    width = max(5, len(str(len(paths) - 1)))
    return (
        tuple(
            f"frame_{position:0{width}d}{path.suffix}"
            for position, path in enumerate(paths)
        ),
        "generated-frame-pattern",
    )


def frame_snapshot(
    sequence: FrameSequence,
    content_hashes: Mapping[str, str] | None = None,
) -> list[dict[str, int | str]]:
    """Возвращает снимок имён, метаданных и необязательных SHA-256."""
    snapshot: list[dict[str, int | str]] = []
    for frame in sequence.frames:
        item: dict[str, int | str] = {
            "name": frame.name,
            "size": frame.size,
            "mtime_ns": frame.mtime_ns,
        }
        if content_hashes is not None:
            digest = content_hashes.get(frame.name)
            if digest is None:
                raise ValidationError(f"Не найден SHA-256 кадра: {frame.name}")
            item["sha256"] = digest
        snapshot.append(item)
    return snapshot


def frame_content_hashes(sequence: FrameSequence) -> dict[str, str]:
    """Считает SHA-256 содержимого каждого JPEG для проверки перед записью."""
    return {frame.name: file_sha256(frame.path) for frame in sequence.frames}


def snapshot_digest(snapshot: Iterable[dict[str, Any]]) -> str:
    """Вычисляет стабильный отпечаток снимка каталога."""
    payload = json.dumps(
        list(snapshot), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    """Считает SHA-256 небольшого служебного файла."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    """Атомарно записывает UTF-8 без BOM в каталог назначения."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Атомарно записывает читаемый JSON как UTF-8 без BOM."""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def metadata_path_for(csv_path: Path) -> Path:
    """Возвращает единое имя метаданных для CSV-плана."""
    return csv_path.with_suffix(".meta.json")


def _ensure_no_incomplete_transaction(folder: Path) -> None:
    state_dir = folder / STATE_DIRECTORY_NAME
    if not state_dir.exists():
        return
    for journal in sorted(state_dir.glob("transaction-*.json")):
        try:
            data = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TransactionError(
                f"Не удалось проверить журнал транзакции {journal}: {exc}"
            ) from exc
        if data.get("status") not in {"completed", "rolled_back"}:
            raise TransactionError(
                "Обнаружена незавершённая транзакция. Сначала требуется восстановление: "
                f"{journal}"
            )


__all__ = [
    "FRAME_NAME_RE",
    "STATE_DIRECTORY_NAME",
    "SUPPORTED_EXTENSIONS",
    "atomic_write_json",
    "atomic_write_text",
    "file_sha256",
    "frame_content_hashes",
    "frame_snapshot",
    "metadata_path_for",
    "natural_name_key",
    "snapshot_digest",
    "validate_frames_directory",
]
