"""Неизменяемые модели анализа и переименования."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AnalysisSettings:
    """Проверенные параметры локального детектора возвратов."""

    thumbnail_width: int = 64
    thumbnail_height: int = 36
    hash_width: int = 16
    max_lookback: int = 60
    candidate_count: int = 4
    max_thumbnail_mae: float = 3.0
    max_hash_distance: int = 20
    jump_multiplier: float = 4.0
    bypass_ratio: float = 0.75
    strict_return_lags: tuple[int, ...] = (7, 14)
    strict_max_thumbnail_mae: float = 1.5
    strict_max_hash_distance: int = 8

    def to_dict(self) -> dict[str, Any]:
        """Возвращает сериализуемое представление настроек."""
        return asdict(self)


@dataclass(frozen=True)
class FrameEntry:
    """Один проверенный JPEG входной последовательности."""

    index: int
    path: Path
    size: int
    mtime_ns: int

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class FrameSequence:
    """Исходный естественный порядок и безопасные итоговые имена."""

    folder: Path
    frames: tuple[FrameEntry, ...]
    target_names: tuple[str, ...]
    naming_mode: str


@dataclass(frozen=True)
class DuplicateMatch:
    """Подтверждённый дёргающий возврат к прошлому кадру."""

    source_index: int
    duplicate_index: int
    thumbnail_mae: float
    hash_distance: int
    left_jump: float
    right_jump: float
    bypass_jump: float
    acceptance_mode: str


@dataclass(frozen=True)
class AnalysisResult:
    """Опубликованный план и агрегированные показатели анализа."""

    folder: Path
    csv_path: Path
    metadata_path: Path
    plan_id: str
    frame_count: int
    duplicate_count: int
    strict_similarity_count: int
    cluster_count: int
    moved_count: int
    total_cost_before: float
    total_cost_after: float
    p95_before: float
    p95_after: float
    large_edges_before: int
    large_edges_after: int

    def to_dict(self) -> dict[str, Any]:
        """Возвращает данные для CLI и веб-интерфейса."""
        data = asdict(self)
        for key in ("folder", "csv_path", "metadata_path"):
            data[key] = str(data[key])
        return data


@dataclass(frozen=True)
class RenamePreview:
    """Проверенный, но ещё не применённый план имён."""

    folder: Path
    csv_path: Path
    plan_id: str
    frame_count: int
    rename_count: int
    first_changes: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["folder"] = str(self.folder)
        data["csv_path"] = str(self.csv_path)
        data["first_changes"] = [list(item) for item in self.first_changes]
        return data


@dataclass(frozen=True)
class RenameResult:
    """Результат завершённой транзакции переименования."""

    folder: Path
    transaction_id: str
    renamed_count: int
    journal_path: Path
    undo_csv_path: Path

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("folder", "journal_path", "undo_csv_path"):
            data[key] = str(data[key])
        return data


ProgressCallback = Any
CancelCheck = Any
