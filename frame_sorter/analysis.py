"""Локальный анализ почти одинаковых возвратных кадров."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageOps

from frame_sorter.exceptions import AnalysisCancelledError, ValidationError
from frame_sorter.io_utils import (
    atomic_write_json,
    atomic_write_text,
    file_sha256,
    frame_snapshot,
    metadata_path_for,
    snapshot_digest,
    validate_frames_directory,
)
from frame_sorter.models import (
    AnalysisResult,
    AnalysisSettings,
    DuplicateMatch,
    FrameSequence,
)

ALGORITHM_VERSION = "local-return-v2"
DEFAULT_PLAN_NAME = "frame-sort-plan.csv"
ProgressCallback = Callable[[dict[str, Any]], None]
CancelCheck = Callable[[], bool]


class _DisjointSet:
    """Минимальная структура объединения групп повторов."""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def analyze_frames(
    folder: str | Path,
    *,
    output_csv: str | Path | None = None,
    settings: AnalysisSettings | None = None,
    emit_event: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> AnalysisResult:
    """Анализирует каталог и атомарно публикует CSV-перестановку."""
    active_settings = settings or AnalysisSettings()
    _validate_settings(active_settings)
    sequence = validate_frames_directory(folder)
    csv_path = _resolve_output_path(sequence, output_csv)
    metadata_path = metadata_path_for(csv_path)
    _emit(
        emit_event,
        phase="loading",
        processed=0,
        total=len(sequence.frames),
        message=f"Найдено кадров: {len(sequence.frames)}.",
    )
    thumbnails, hashes, content_hashes = _load_features(
        sequence,
        active_settings,
        emit_event=emit_event,
        cancel_check=cancel_check,
    )
    adjacent = _adjacent_costs(thumbnails)
    matches = _find_disruptive_returns(
        thumbnails,
        hashes,
        adjacent,
        active_settings,
        emit_event=emit_event,
        cancel_check=cancel_check,
    )
    order, cluster_count = _stable_cluster_order(len(sequence.frames), matches)
    metrics = _measure_order(thumbnails, order, adjacent)
    moved_count = sum(position != source for position, source in enumerate(order))
    strict_similarity_count = sum(
        match.acceptance_mode == "strict-known-lag" for match in matches
    )
    snapshot = frame_snapshot(sequence, content_hashes)
    plan_id = _plan_id(active_settings, snapshot, order)
    _publish_plan(
        sequence,
        csv_path,
        metadata_path,
        active_settings,
        snapshot,
        plan_id,
        order,
        matches,
        cluster_count,
        moved_count,
        metrics,
    )
    result = AnalysisResult(
        folder=sequence.folder,
        csv_path=csv_path,
        metadata_path=metadata_path,
        plan_id=plan_id,
        frame_count=len(sequence.frames),
        duplicate_count=len(matches),
        strict_similarity_count=strict_similarity_count,
        cluster_count=cluster_count,
        moved_count=moved_count,
        total_cost_before=metrics["total_cost_before"],
        total_cost_after=metrics["total_cost_after"],
        p95_before=metrics["p95_before"],
        p95_after=metrics["p95_after"],
        large_edges_before=int(metrics["large_edges_before"]),
        large_edges_after=int(metrics["large_edges_after"]),
    )
    _emit(
        emit_event,
        phase="completed",
        processed=len(sequence.frames),
        total=len(sequence.frames),
        duplicates=len(matches),
        csv_path=str(csv_path),
        message=f"CSV создан: {csv_path}",
    )
    return result


def _load_features(
    sequence: FrameSequence,
    settings: AnalysisSettings,
    *,
    emit_event: ProgressCallback | None,
    cancel_check: CancelCheck | None,
) -> tuple[np.ndarray, np.ndarray, dict[str, str]]:
    count = len(sequence.frames)
    thumbnails = np.empty(
        (count, settings.thumbnail_height, settings.thumbnail_width), dtype=np.uint8
    )
    hashes = np.empty((count, settings.hash_width * settings.hash_width), dtype=np.bool_)
    content_hashes: dict[str, str] = {}
    for position, frame in enumerate(sequence.frames):
        _raise_if_cancelled(cancel_check)
        try:
            encoded = frame.path.read_bytes()
            content_hashes[frame.name] = hashlib.sha256(encoded).hexdigest()
            with Image.open(io.BytesIO(encoded)) as source:
                normalized = ImageOps.exif_transpose(source).convert("L")
                thumbnail = normalized.resize(
                    (settings.thumbnail_width, settings.thumbnail_height),
                    Image.Resampling.BILINEAR,
                )
                hash_image = normalized.resize(
                    (settings.hash_width + 1, settings.hash_width),
                    Image.Resampling.LANCZOS,
                )
                thumbnails[position] = np.asarray(thumbnail, dtype=np.uint8)
                hash_pixels = np.asarray(hash_image, dtype=np.uint8)
                hashes[position] = (hash_pixels[:, 1:] > hash_pixels[:, :-1]).reshape(-1)
        except (OSError, ValueError) as exc:
            raise ValidationError(f"Не удалось декодировать {frame.name}: {exc}") from exc
        if (position + 1) % 128 == 0 or position + 1 == count:
            _emit(
                emit_event,
                phase="loading",
                processed=position + 1,
                total=count,
                message=f"Подготовлено кадров: {position + 1} из {count}.",
            )
    return thumbnails, hashes, content_hashes


def _adjacent_costs(thumbnails: np.ndarray) -> np.ndarray:
    if len(thumbnails) < 2:
        return np.empty(0, dtype=np.float64)
    costs = np.empty(len(thumbnails) - 1, dtype=np.float64)
    for index in range(len(costs)):
        costs[index] = _mae(thumbnails[index], thumbnails[index + 1])
    return costs


def _find_disruptive_returns(
    thumbnails: np.ndarray,
    hashes: np.ndarray,
    adjacent: np.ndarray,
    settings: AnalysisSettings,
    *,
    emit_event: ProgressCallback | None,
    cancel_check: CancelCheck | None,
) -> list[DuplicateMatch]:
    matches: list[DuplicateMatch] = []
    strict_similarity_count = 0
    rejected_by_context_count = 0
    total = len(thumbnails)
    for index in range(2, total - 1):
        _raise_if_cancelled(cancel_check)
        evaluation = _evaluate_disruptive_return(
            thumbnails,
            hashes,
            adjacent,
            settings,
            index,
        )
        if evaluation is None:
            continue
        match, rejected_by_context = evaluation
        if match is not None:
            matches.append(match)
            if match.acceptance_mode == "strict-known-lag":
                strict_similarity_count += 1
        elif rejected_by_context:
            rejected_by_context_count += 1
        if index % 128 == 0 or index == total - 2:
            _emit(
                emit_event,
                phase="analyzing",
                processed=index + 1,
                total=total,
                duplicates=len(matches),
            )
    _emit(
        emit_event,
        phase="analyzing",
        processed=total,
        total=total,
        duplicates=len(matches),
        strict_similarity_count=strict_similarity_count,
        rejected_by_context_count=rejected_by_context_count,
        message=(
            f"Найдено дёргающих возвратов: {len(matches)}; "
            f"по строгому совпадению известного лага: {strict_similarity_count}; "
            f"отклонено контекстом: {rejected_by_context_count}."
        ),
    )
    return matches


def _evaluate_disruptive_return(
    thumbnails: np.ndarray,
    hashes: np.ndarray,
    adjacent: np.ndarray,
    settings: AnalysisSettings,
    index: int,
) -> tuple[DuplicateMatch | None, bool] | None:
    candidate = _nearest_return_candidate(thumbnails, hashes, settings, index)
    if candidate is None:
        return None
    source_index, distance, hash_distance = candidate
    similarity_is_allowed = (
        distance <= settings.max_thumbnail_mae
        and hash_distance <= settings.max_hash_distance
    )
    if not similarity_is_allowed:
        return None, False

    left_jump = float(adjacent[index - 1])
    right_jump = float(adjacent[index])
    bypass_jump = _mae(thumbnails[index - 1], thumbnails[index + 1])
    jump_threshold = max(
        settings.max_thumbnail_mae,
        settings.jump_multiplier * distance,
    )
    jumps_are_disruptive = (
        left_jump > jump_threshold and right_jump > jump_threshold
    )
    if not jumps_are_disruptive:
        return None, False

    context_is_smooth = bypass_jump < settings.bypass_ratio * min(
        left_jump,
        right_jump,
    )
    strict_known_lag = (
        index - source_index in settings.strict_return_lags
        and distance <= settings.strict_max_thumbnail_mae
        and hash_distance <= settings.strict_max_hash_distance
    )
    if not context_is_smooth and not strict_known_lag:
        return None, True
    acceptance_mode = "context" if context_is_smooth else "strict-known-lag"
    return (
        DuplicateMatch(
            source_index=source_index,
            duplicate_index=index,
            thumbnail_mae=distance,
            hash_distance=hash_distance,
            left_jump=left_jump,
            right_jump=right_jump,
            bypass_jump=bypass_jump,
            acceptance_mode=acceptance_mode,
        ),
        False,
    )


def _nearest_return_candidate(
    thumbnails: np.ndarray,
    hashes: np.ndarray,
    settings: AnalysisSettings,
    index: int,
) -> tuple[int, float, int] | None:
    start = max(0, index - settings.max_lookback)
    stop = index - 1
    if stop <= start:
        return None
    hamming = np.count_nonzero(hashes[start:stop] != hashes[index], axis=1)
    candidate_count = min(settings.candidate_count, len(hamming))
    nearest_offsets = np.argsort(hamming, kind="stable")[:candidate_count]
    candidate_indices = nearest_offsets + start
    candidate_distances = np.asarray(
        [_mae(thumbnails[candidate], thumbnails[index]) for candidate in candidate_indices]
    )
    best_offset = int(np.argmin(candidate_distances))
    return (
        int(candidate_indices[best_offset]),
        float(candidate_distances[best_offset]),
        int(hamming[int(nearest_offsets[best_offset])]),
    )


def _stable_cluster_order(
    frame_count: int, matches: list[DuplicateMatch]
) -> tuple[list[int], int]:
    groups = _DisjointSet(frame_count)
    for match in matches:
        groups.union(match.source_index, match.duplicate_index)
    components: dict[int, list[int]] = defaultdict(list)
    for index in range(frame_count):
        components[groups.find(index)].append(index)
    ordered_components = sorted(components.values(), key=lambda values: values[0])
    cluster_count = sum(len(values) > 1 for values in ordered_components)
    return [index for values in ordered_components for index in values], cluster_count


def _measure_order(
    thumbnails: np.ndarray, order: list[int], adjacent: np.ndarray
) -> dict[str, float]:
    reordered = np.empty(max(0, len(order) - 1), dtype=np.float64)
    for position in range(len(reordered)):
        reordered[position] = _mae(
            thumbnails[order[position]], thumbnails[order[position + 1]]
        )
    if not len(adjacent):
        return {
            "total_cost_before": 0.0,
            "total_cost_after": 0.0,
            "p95_before": 0.0,
            "p95_after": 0.0,
            "large_edges_before": 0.0,
            "large_edges_after": 0.0,
        }
    threshold = float(np.percentile(adjacent, 95))
    return {
        "total_cost_before": float(adjacent.sum()),
        "total_cost_after": float(reordered.sum()),
        "p95_before": threshold,
        "p95_after": float(np.percentile(reordered, 95)),
        "large_edges_before": float(np.count_nonzero(adjacent > threshold)),
        "large_edges_after": float(np.count_nonzero(reordered > threshold)),
    }


def _publish_plan(
    sequence: FrameSequence,
    csv_path: Path,
    metadata_path: Path,
    settings: AnalysisSettings,
    snapshot: list[dict[str, int | str]],
    plan_id: str,
    order: list[int],
    matches: list[DuplicateMatch],
    cluster_count: int,
    moved_count: int,
    metrics: dict[str, float],
) -> None:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("position", "source_filename"))
    for position, source_index in enumerate(order):
        writer.writerow((position, sequence.frames[source_index].name))
    atomic_write_text(csv_path, output.getvalue())
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "algorithm_version": ALGORITHM_VERSION,
        "plan_id": plan_id,
        "folder": str(sequence.folder),
        "naming_mode": sequence.naming_mode,
        "target_names": list(sequence.target_names),
        "frame_count": len(sequence.frames),
        "csv_sha256": file_sha256(csv_path),
        "snapshot_sha256": snapshot_digest(snapshot),
        "snapshot": snapshot,
        "settings": settings.to_dict(),
        "summary": {
            "duplicate_count": len(matches),
            "strict_similarity_count": sum(
                match.acceptance_mode == "strict-known-lag" for match in matches
            ),
            "cluster_count": cluster_count,
            "moved_count": moved_count,
            **metrics,
        },
        "matches": [
            {
                "source_index": match.source_index,
                "duplicate_index": match.duplicate_index,
                "lag": match.duplicate_index - match.source_index,
                "thumbnail_mae": round(match.thumbnail_mae, 6),
                "hash_distance": match.hash_distance,
                "acceptance_mode": match.acceptance_mode,
            }
            for match in matches
        ],
    }
    atomic_write_json(metadata_path, metadata)


def _resolve_output_path(
    sequence: FrameSequence, output_csv: str | Path | None
) -> Path:
    if output_csv is None:
        return sequence.folder / DEFAULT_PLAN_NAME
    path = Path(output_csv).expanduser()
    if path.suffix.casefold() != ".csv":
        raise ValidationError("Путь результата должен иметь расширение .csv.")
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def _plan_id(
    settings: AnalysisSettings,
    snapshot: list[dict[str, int | str]],
    order: list[int],
) -> str:
    payload = {
        "algorithm_version": ALGORITHM_VERSION,
        "settings": settings.to_dict(),
        "snapshot_sha256": snapshot_digest(snapshot),
        "order": order,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_settings(settings: AnalysisSettings) -> None:
    if settings.thumbnail_width < 8 or settings.thumbnail_height < 8:
        raise ValidationError("Размер миниатюры слишком мал.")
    if settings.hash_width < 4:
        raise ValidationError("Ширина dHash слишком мала.")
    if settings.max_lookback < 2 or settings.candidate_count < 1:
        raise ValidationError("Локальное окно и число кандидатов должны быть положительными.")
    if (
        not settings.strict_return_lags
        or any(
            not isinstance(lag, int)
            or isinstance(lag, bool)
            or lag < 2
            or lag > settings.max_lookback
            for lag in settings.strict_return_lags
        )
        or len(set(settings.strict_return_lags)) != len(settings.strict_return_lags)
    ):
        raise ValidationError(
            "Строгие лаги возврата должны быть уникальны и входить в окно анализа."
        )
    if (
        not np.isfinite(settings.strict_max_thumbnail_mae)
        or settings.strict_max_thumbnail_mae <= 0
        or settings.strict_max_thumbnail_mae > settings.max_thumbnail_mae
        or settings.strict_max_hash_distance < 0
        or settings.strict_max_hash_distance > settings.max_hash_distance
    ):
        raise ValidationError(
            "Строгие пороги сходства должны быть положительными и не шире общих."
        )
    ratio_is_valid = np.isfinite(settings.bypass_ratio) & np.greater(
        settings.bypass_ratio, 0
    ) & np.less(settings.bypass_ratio, 1)
    if not bool(ratio_is_valid):
        raise ValidationError("Коэффициент обхода должен быть между 0 и 1.")


def _mae(left: np.ndarray, right: np.ndarray) -> float:
    return float(np.mean(np.abs(left.astype(np.int16) - right.astype(np.int16))))


def _raise_if_cancelled(cancel_check: CancelCheck | None) -> None:
    if cancel_check is not None and cancel_check():
        raise AnalysisCancelledError("Анализ отменён пользователем.")


def _emit(callback: ProgressCallback | None, **event: Any) -> None:
    if callback is not None:
        callback(dict(event))


__all__ = ["ALGORITHM_VERSION", "DEFAULT_PLAN_NAME", "analyze_frames"]
