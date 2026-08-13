"""Единый доменный сервис для CLI и локального веб-интерфейса."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from frame_sorter.analysis import analyze_frames
from frame_sorter.exceptions import ValidationError
from frame_sorter.models import (
    AnalysisResult,
    AnalysisSettings,
    RenamePreview,
    RenameResult,
)
from frame_sorter.renamer import apply_rename as _apply_rename
from frame_sorter.renamer import preview_rename as _preview_rename
from frame_sorter.renamer import recover_transaction
from frame_sorter.video import VideoRebuildResult
from frame_sorter.video import rebuild_video as _rebuild_video

ProgressCallback = Callable[[dict[str, Any]], None]
CancelCheck = Callable[[], bool]


def analyze_folder(
    folder: str | Path,
    output_csv: str | Path | None = None,
    settings: AnalysisSettings | Mapping[str, Any] | None = None,
    *,
    emit_event: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> AnalysisResult:
    """Создаёт CSV сортировки для одного каталога."""
    active_settings = _coerce_settings(settings)
    return analyze_frames(
        folder,
        output_csv=output_csv,
        settings=active_settings,
        emit_event=emit_event,
        cancel_check=cancel_check,
    )


def preview_rename(folder: str | Path, csv_path: str | Path) -> RenamePreview:
    """Проверяет CSV без изменения исходных имён."""
    return _preview_rename(folder, csv_path)


def apply_rename(
    folder: str | Path,
    csv_path: str | Path,
    *,
    emit_event: ProgressCallback | None = None,
) -> RenameResult:
    """Применяет CSV через файловую транзакцию."""
    return _apply_rename(folder, csv_path, emit_event=emit_event)


def rebuild_video(
    folder: str | Path,
    original_video: str | Path | None = None,
    output_video: str | Path | None = None,
    *,
    emit_event: ProgressCallback | None = None,
) -> VideoRebuildResult:
    """Пересобирает отсортированные JPEG в новый MP4 с исходным звуком."""
    return _rebuild_video(
        folder,
        original_video=original_video,
        output_video=output_video,
        emit_event=emit_event,
    )


def _coerce_settings(
    settings: AnalysisSettings | Mapping[str, Any] | None,
) -> AnalysisSettings:
    if settings is None:
        return AnalysisSettings()
    if isinstance(settings, AnalysisSettings):
        return settings
    try:
        return AnalysisSettings(**dict(settings))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Некорректные настройки анализа: {exc}") from exc


__all__ = [
    "analyze_folder",
    "apply_rename",
    "preview_rename",
    "recover_transaction",
    "rebuild_video",
]
