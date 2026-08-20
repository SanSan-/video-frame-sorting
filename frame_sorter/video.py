"""Безопасная пересборка MP4 из проверенной последовательности JPEG-кадров."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from frame_sorter.exceptions import FrameSorterError, ValidationError
from frame_sorter.io_utils import validate_frames_directory

FFPROBE_TIMEOUT_SECONDS = 10 * 60
FFMPEG_TIMEOUT_SECONDS = 4 * 60 * 60
_VIDEO_TIME_BASE = "1:90000"
_VIDEO_TIME_BASE_DENOMINATOR = 90_000
_MANIFEST_FRAME_RATE = "90000"
_STDERR_TAIL_LIMIT = 8 * 1024
ProgressCallback = Callable[[dict[str, Any]], None]
logger = logging.getLogger(__name__)


class VideoRebuildError(FrameSorterError):
    """Ошибка запуска FFmpeg либо проверки созданного видео."""


@dataclass(frozen=True)
class VideoRebuildResult:
    """Проверенный результат атомарно завершённой пересборки."""

    folder: Path
    original_video: Path
    output_video: Path
    frame_count: int
    audio_copied: bool
    audio_timestamps_repaired: bool
    source_duration_seconds: float
    output_duration_seconds: float

    def to_dict(self) -> dict[str, Any]:
        """Возвращает сериализуемое представление для CLI и веб-интерфейса."""
        data = asdict(self)
        for key in ("folder", "original_video", "output_video"):
            data[key] = str(data[key])
        return data


@dataclass(frozen=True)
class _MediaProbe:
    """Минимальные проверенные сведения одного запуска ffprobe."""

    video_stream: dict[str, Any]
    video_frames: tuple[dict[str, Any], ...]
    audio_stream_count: int
    audio_stream: dict[str, Any] | None
    audio_frames: tuple[dict[str, Any], ...]
    reported_duration: Decimal | None


@dataclass(frozen=True)
class _AudioTimestampRepair:
    """Параметры перенумерации AAC-пакетов без перекодирования звука."""

    sample_rate: int
    samples_per_packet: int


@dataclass(frozen=True)
class _FrameTiming:
    """Нормализованные метки времени и длительности кадров."""

    normalized_pts: tuple[Decimal, ...]
    durations: tuple[Decimal, ...]

    @property
    def total_duration(self) -> Decimal:
        return self.normalized_pts[-1] + self.durations[-1]


def rebuild_video(
    folder: str | Path,
    original_video: str | Path | None = None,
    output_video: str | Path | None = None,
    emit_event: ProgressCallback | None = None,
) -> VideoRebuildResult:
    """Пересобирает JPEG в новый MP4 с временной шкалой исходного видео."""
    sequence = validate_frames_directory(folder)
    source = _resolve_original_video(sequence.folder, original_video)
    output = _resolve_output_video(sequence.folder, source, output_video)
    ffprobe = _resolve_tool("ffprobe")
    ffmpeg = _resolve_tool("ffmpeg")
    frame_count = len(sequence.frames)

    _emit(
        emit_event,
        phase="probing",
        processed=0,
        total=frame_count,
        message="Проверяется временная шкала исходного видео.",
    )
    source_probe = _probe_media(source, ffprobe)
    source_timing = _frame_timing(
        source_probe,
        expected_count=frame_count,
        output=False,
    )
    source_duration = source_probe.reported_duration or source_timing.total_duration
    source_has_audio = source_probe.audio_stream_count > 0
    audio_timestamp_repair = _audio_timestamp_repair(source_probe)
    _emit(
        emit_event,
        type="media_probe",
        role="source",
        phase="probing",
        processed=frame_count,
        total=frame_count,
        frame_count=frame_count,
        audio_stream_count=source_probe.audio_stream_count,
        container_duration_seconds=_decimal_text(source_duration),
        video_timeline_duration_seconds=_decimal_text(source_timing.total_duration),
        message=(
            f"Исходный MP4: кадров {frame_count}, видеоряд "
            f"{_decimal_text(source_timing.total_duration)} с, контейнер "
            f"{_decimal_text(source_duration)} с, аудиопотоков "
            f"{source_probe.audio_stream_count}."
            + (
                " Временные метки AAC расходятся с числом сэмплов; "
                "аудиодорожка будет нормализована без изменения числа сэмплов."
                if audio_timestamp_repair is not None
                else ""
            )
        ),
    )

    manifest_path: Path | None = None
    temporary_output: Path | None = None
    try:
        manifest_path = _write_concat_manifest(
            output.parent,
            tuple(frame.path for frame in sequence.frames),
            source_timing.durations,
        )
        temporary_output = _reserve_temporary_output(output)
        _emit(
            emit_event,
            phase="rebuilding",
            processed=0,
            total=frame_count,
            message="FFmpeg пересобирает видео во временный файл.",
        )
        _run_ffmpeg(
            ffmpeg,
            manifest_path,
            source,
            temporary_output,
            frame_count,
            source_timing.total_duration,
            source_timing.durations[-1],
            source_duration if source_has_audio else source_timing.total_duration,
            audio_timestamp_repair,
        )
        _emit(
            emit_event,
            phase="verifying_video",
            processed=0,
            total=frame_count,
            message="Созданное видео проверяется перед публикацией.",
        )
        output_probe = _probe_media(temporary_output, ffprobe)
        output_timing = _frame_timing(
            output_probe,
            expected_count=frame_count,
            output=True,
        )
        output_duration = output_probe.reported_duration or output_timing.total_duration
        _emit(
            emit_event,
            type="media_probe",
            role="output",
            phase="verifying_video",
            processed=frame_count,
            total=frame_count,
            frame_count=len(output_probe.video_frames),
            audio_stream_count=output_probe.audio_stream_count,
            container_duration_seconds=_decimal_text(output_duration),
            video_timeline_duration_seconds=_decimal_text(output_timing.total_duration),
            message=(
                f"Новый MP4: кадров {len(output_probe.video_frames)}, видеоряд "
                f"{_decimal_text(output_timing.total_duration)} с, контейнер "
                f"{_decimal_text(output_duration)} с, аудиопотоков "
                f"{output_probe.audio_stream_count}."
            ),
        )
        _verify_rebuilt_video(
            source_probe=source_probe,
            source_timing=source_timing,
            source_duration=source_duration,
            source_has_audio=source_has_audio,
            output_probe=output_probe,
            output_timing=output_timing,
            output_duration=output_duration,
        )
        if output.exists():
            raise VideoRebuildError(
                f"Итоговый файл появился во время пересборки и не будет перезаписан: {output}"
            )
        try:
            os.replace(temporary_output, output)
        except OSError as exc:
            raise VideoRebuildError(
                f"Не удалось опубликовать проверенное видео: {output}"
            ) from exc
        temporary_output = None
    finally:
        _remove_temporary_file(manifest_path)
        _remove_temporary_file(temporary_output)

    _emit(
        emit_event,
        phase="completed",
        processed=frame_count,
        total=frame_count,
        output_video=str(output),
        message=f"Видео пересобрано: {output}",
    )
    return VideoRebuildResult(
        folder=sequence.folder,
        original_video=source,
        output_video=output,
        frame_count=frame_count,
        audio_copied=source_has_audio,
        audio_timestamps_repaired=audio_timestamp_repair is not None,
        source_duration_seconds=float(source_duration),
        output_duration_seconds=float(output_duration),
    )


def _resolve_original_video(folder: Path, value: str | Path | None) -> Path:
    candidate = folder.parent / f"{folder.name}.mp4" if value is None else Path(value).expanduser()
    try:
        path = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"Исходное видео не найдено: {candidate}") from exc
    if not path.is_file() or path.suffix.casefold() != ".mp4":
        raise ValidationError(f"Ожидался исходный MP4-файл: {path}")
    return path


def _resolve_output_video(
    folder: Path,
    source: Path,
    value: str | Path | None,
) -> Path:
    candidate = (
        folder.parent / f"{folder.name}_sorted.mp4"
        if value is None
        else Path(value).expanduser()
    )
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        output = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"Некорректный путь итогового видео: {candidate}") from exc
    if output.suffix.casefold() != ".mp4":
        raise ValidationError("Путь итогового видео должен иметь расширение .mp4.")
    if _path_key(output) == _path_key(source):
        raise ValidationError("Итоговый файл не может совпадать с исходным видео.")
    if output.exists():
        raise ValidationError(f"Итоговый файл уже существует: {output}")
    if not output.parent.is_dir():
        raise ValidationError(f"Каталог итогового видео не найден: {output.parent}")
    return output


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _resolve_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise ValidationError(
            f"Не найден {name}. Установите FFmpeg и добавьте его каталог в PATH."
        )
    return executable


def _probe_media(path: Path, ffprobe: str) -> _MediaProbe:
    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        (
            "stream=index,codec_type,codec_name,sample_rate,nb_frames,duration,"
            "start_time,time_base,avg_frame_rate,"
            "r_frame_rate:frame=media_type,stream_index,pts_time,"
            "best_effort_timestamp_time,pkt_duration_time,duration_time,"
            "nb_samples:format=duration"
        ),
        "-show_streams",
        "-show_frames",
        "-show_format",
        os.fspath(path),
    ]
    completed = _run_command(command, timeout=FFPROBE_TIMEOUT_SECONDS, operation="ffprobe")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VideoRebuildError("ffprobe вернул некорректный JSON.") from exc
    if not isinstance(payload, dict):
        raise VideoRebuildError("ffprobe вернул JSON неподдерживаемого вида.")
    streams = payload.get("streams")
    frames = payload.get("frames")
    if not isinstance(streams, list) or not isinstance(frames, list):
        raise VideoRebuildError("ffprobe не вернул сведения о потоках и кадрах.")
    video_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        raise VideoRebuildError(f"В видео нет видеопотока: {path.name}")
    stream_index = _integer(video_stream.get("index"), "индекс видеопотока")
    video_frames = tuple(
        frame
        for frame in frames
        if isinstance(frame, dict)
        and frame.get("media_type") == "video"
        and _optional_integer(frame.get("stream_index")) == stream_index
    )
    audio_stream_count = sum(
        isinstance(stream, dict) and stream.get("codec_type") == "audio"
        for stream in streams
    )
    audio_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"),
        None,
    )
    audio_stream_index = (
        _optional_integer(audio_stream.get("index"))
        if audio_stream is not None
        else None
    )
    audio_frames = tuple(
        frame
        for frame in frames
        if isinstance(frame, dict)
        and frame.get("media_type") == "audio"
        and _optional_integer(frame.get("stream_index")) == audio_stream_index
    )
    format_data = payload.get("format")
    format_duration = (
        _positive_decimal(format_data.get("duration"))
        if isinstance(format_data, dict)
        else None
    )
    stream_durations = [
        duration
        for stream in streams
        if isinstance(stream, dict)
        for duration in (_positive_decimal(stream.get("duration")),)
        if duration is not None
    ]
    reported_duration = format_duration or (max(stream_durations) if stream_durations else None)
    return _MediaProbe(
        video_stream=video_stream,
        video_frames=video_frames,
        audio_stream_count=audio_stream_count,
        audio_stream=audio_stream,
        audio_frames=audio_frames,
        reported_duration=reported_duration,
    )


def _audio_timestamp_repair(probe: _MediaProbe) -> _AudioTimestampRepair | None:
    """Находит AAC, чьи пакетные PTS не соответствуют числу декодируемых сэмплов."""
    stream = probe.audio_stream
    frames = probe.audio_frames
    if (
        stream is None
        or not frames
        or str(stream.get("codec_name") or "").casefold() != "aac"
    ):
        return None
    duration = _positive_decimal(stream.get("duration")) or probe.reported_duration
    sample_rate = _optional_integer(stream.get("sample_rate"))
    sample_counts = tuple(_optional_integer(frame.get("nb_samples")) for frame in frames)
    if (
        duration is None
        or sample_rate is None
        or sample_rate <= 0
        or any(value is None or value <= 0 for value in sample_counts)
    ):
        return None
    unique_sample_counts = {int(value) for value in sample_counts if value is not None}
    if len(unique_sample_counts) != 1:
        return None
    samples_per_packet = unique_sample_counts.pop()
    try:
        first_timestamp = _frame_timestamp(frames[0], 0)
        last_timestamp = _frame_timestamp(frames[-1], len(frames) - 1)
    except VideoRebuildError:
        return None
    last_duration = (
        _positive_decimal(frames[-1].get("duration_time"))
        or _positive_decimal(frames[-1].get("pkt_duration_time"))
        or Decimal(samples_per_packet) / Decimal(sample_rate)
    )
    timestamp_duration = last_timestamp - first_timestamp + last_duration
    sample_duration = Decimal(
        sum(int(value) for value in sample_counts if value is not None)
    ) / Decimal(sample_rate)
    sample_tolerance = max(Decimal("0.002"), Decimal(1) / Decimal(sample_rate))
    timestamp_tolerance = max(Decimal("0.100"), duration * Decimal("0.005"))
    if (
        abs(sample_duration - duration) <= sample_tolerance
        and abs(timestamp_duration - duration) > timestamp_tolerance
    ):
        return _AudioTimestampRepair(
            sample_rate=sample_rate,
            samples_per_packet=samples_per_packet,
        )
    return None


def _frame_timing(
    probe: _MediaProbe,
    *,
    expected_count: int,
    output: bool,
) -> _FrameTiming:
    actual_count = len(probe.video_frames)
    if actual_count != expected_count:
        message = (
            f"Проверка итогового видео не пройдена: ожидалось кадров {expected_count}, "
            f"получено {actual_count}."
            if output
            else (
                f"Число кадров исходного видео ({actual_count}) не совпадает "
                f"с числом JPEG ({expected_count})."
            )
        )
        error_type = VideoRebuildError if output else ValidationError
        raise error_type(message)

    timestamps = tuple(_frame_timestamp(frame, index) for index, frame in enumerate(probe.video_frames))
    first = timestamps[0]
    normalized = tuple(value - first for value in timestamps)
    durations: list[Decimal] = []
    for index in range(expected_count - 1):
        duration = normalized[index + 1] - normalized[index]
        if duration <= 0:
            raise VideoRebuildError(
                f"Метки времени видеокадров не возрастают на позиции {index + 1}."
            )
        durations.append(duration)
    durations.append(_last_frame_duration(probe, normalized, durations))
    return _FrameTiming(normalized_pts=normalized, durations=tuple(durations))


def _frame_timestamp(frame: dict[str, Any], position: int) -> Decimal:
    for key in ("best_effort_timestamp_time", "pts_time"):
        value = _finite_decimal(frame.get(key))
        if value is not None:
            return value
    raise VideoRebuildError(
        f"ffprobe не вернул PTS либо best_effort_timestamp для кадра {position}."
    )


def _last_frame_duration(
    probe: _MediaProbe,
    normalized: tuple[Decimal, ...],
    previous_durations: list[Decimal],
) -> Decimal:
    last_frame = probe.video_frames[-1]
    for key in ("duration_time", "pkt_duration_time"):
        duration = _positive_decimal(last_frame.get(key))
        if duration is not None:
            return duration
    stream_duration = _positive_decimal(probe.video_stream.get("duration"))
    if stream_duration is not None:
        remainder = stream_duration - normalized[-1]
        if remainder > 0:
            return remainder
    if previous_durations:
        return previous_durations[-1]
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = _fraction_decimal(probe.video_stream.get(key))
        if rate is not None and rate > 0:
            return Decimal(1) / rate
    raise VideoRebuildError("Не удалось определить длительность единственного видеокадра.")


def _write_concat_manifest(
    directory: Path,
    frames: tuple[Path, ...],
    durations: tuple[Decimal, ...],
) -> Path:
    if len(frames) != len(durations):
        raise VideoRebuildError("Число кадров и длительностей ffconcat не совпадает.")
    descriptor, name = tempfile.mkstemp(
        prefix=".frame-sort-",
        suffix=".ffconcat",
        dir=directory,
    )
    path = Path(name)
    lines = ["ffconcat version 1.0"]
    for frame, duration in zip(frames, durations, strict=True):
        lines.append(_concat_file_line(frame))
        lines.append(f"option framerate {_MANIFEST_FRAME_RATE}")
        lines.append(f"duration {_decimal_text(duration)}")
    # Повтор последнего файла даёт concat-демультиплексору конечную метку длительности.
    # trim исключает служебный повтор, а video setts ниже возвращает последнему реальному
    # пакету исходную длительность: MP4 иначе сокращает её до одного тика 1/90000.
    lines.append(_concat_file_line(frames[-1]))
    lines.append(f"option framerate {_MANIFEST_FRAME_RATE}")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        _remove_temporary_file(path)
        raise
    return path


def _concat_file_line(path: Path) -> str:
    absolute = path.resolve(strict=True).as_posix()
    escaped = absolute.replace("'", "'\\''")
    return f"file '{escaped}'"


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _reserve_temporary_output(output: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output.stem}.",
        suffix=".building.mp4",
        dir=output.parent,
    )
    os.close(descriptor)
    return Path(name)


def _run_ffmpeg(
    ffmpeg: str,
    manifest: Path,
    source: Path,
    temporary_output: Path,
    frame_count: int,
    video_duration: Decimal,
    last_frame_duration: Decimal,
    container_duration: Decimal,
    audio_timestamp_repair: _AudioTimestampRepair | None,
) -> None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        os.fspath(manifest),
        "-i",
        os.fspath(source),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
        "-map_metadata",
        "1",
        "-map_chapters",
        "1",
        "-vf",
        f"trim=end={_decimal_text(video_duration)}",
        "-c:v",
        "libx264",
        "-bf",
        "0",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode:v",
        "passthrough",
        "-enc_time_base:v",
        _VIDEO_TIME_BASE,
        "-bsf:v",
        _last_video_packet_duration_filter(frame_count, last_frame_duration),
    ]
    command.extend(["-c:a", "copy"])
    if audio_timestamp_repair is not None:
        samples = audio_timestamp_repair.samples_per_packet
        sample_rate = audio_timestamp_repair.sample_rate
        command.extend(
            [
                "-bsf:a",
                (
                    f"setts=pts=N*{samples}:dts=N*{samples}:duration={samples}:"
                    f"time_base=1/{sample_rate}"
                ),
            ]
        )
    if audio_timestamp_repair is None:
        # Длительность контейнера Samsung иногда заканчивается на несколько миллисекунд
        # раньше PTS последнего видеокадра. Глобальный -t не должен отрезать этот кадр.
        output_limit = max(container_duration, video_duration)
        command.extend(["-t", _decimal_text(output_limit)])
    command.extend(
        ["-movflags", "+faststart", "-y", os.fspath(temporary_output)]
    )
    _run_command(command, timeout=FFMPEG_TIMEOUT_SECONDS, operation="ffmpeg")


def _last_video_packet_duration_filter(
    frame_count: int,
    last_frame_duration: Decimal,
) -> str:
    """Фиксирует длительность последнего H.264-пакета в шкале 1/90000."""
    if frame_count < 1:
        raise VideoRebuildError("Некорректна длительность последнего видеокадра.")
    duration_ticks = _last_video_duration_ticks(last_frame_duration)
    last_packet = frame_count - 1
    return (
        "setts=duration="
        f"if(eq(N\\,{last_packet})\\,{duration_ticks}\\,DURATION)"
    )


def _last_video_duration_ticks(last_frame_duration: Decimal) -> int:
    """Квантует длительность последнего кадра как FFmpeg video setts."""
    if not last_frame_duration.is_finite() or last_frame_duration <= 0:
        raise VideoRebuildError("Некорректна длительность последнего видеокадра.")
    return max(
        1,
        int(
            (last_frame_duration * _VIDEO_TIME_BASE_DENOMINATOR).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        ),
    )


def _run_command(
    command: list[str],
    *,
    timeout: int,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    started_at = time.monotonic()
    logger.info("Запущен %s: %s", operation, subprocess.list2cmdline(command))
    try:
        completed = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started_at
        logger.exception("%s превысил тайм-аут за %.3f с.", operation, elapsed)
        raise VideoRebuildError(
            f"{operation} не завершился за отведённое время."
        ) from exc
    except OSError as exc:
        logger.exception("Не удалось запустить %s.", operation)
        raise VideoRebuildError(f"Не удалось запустить {operation}.") from exc
    elapsed = time.monotonic() - started_at
    if completed.returncode != 0:
        detail = _stderr_tail(completed.stderr)
        logger.error(
            "%s завершился с кодом %s за %.3f с: %s",
            operation,
            completed.returncode,
            elapsed,
            detail or "описание ошибки отсутствует",
        )
        suffix = f": {detail}" if detail else "."
        raise VideoRebuildError(
            f"{operation} завершился с кодом {completed.returncode}{suffix}"
        )
    logger.info("%s завершён успешно за %.3f с.", operation, elapsed)
    return completed


def _stderr_tail(value: str | None) -> str:
    normalized = (value or "").strip()
    if len(normalized) <= _STDERR_TAIL_LIMIT:
        return normalized
    return "…" + normalized[-_STDERR_TAIL_LIMIT:]


def _verify_rebuilt_video(
    *,
    source_probe: _MediaProbe,
    source_timing: _FrameTiming,
    source_duration: Decimal,
    source_has_audio: bool,
    output_probe: _MediaProbe,
    output_timing: _FrameTiming,
    output_duration: Decimal,
) -> None:
    _verify_audio_preserved(source_probe, source_has_audio, output_probe)
    _verify_video_timestamps(source_timing, output_timing)
    _verify_last_frame_duration(source_timing, output_probe, output_timing)
    expected_video_duration = source_timing.total_duration
    video_tolerance = max(Decimal("0.050"), expected_video_duration * Decimal("0.002"))
    if abs(output_timing.total_duration - expected_video_duration) > video_tolerance:
        raise VideoRebuildError(
            "Проверка итогового видео не пройдена: длительность видеоряда отличается "
            f"от исходной временной шкалы ({_decimal_text(output_timing.total_duration)} "
            f"вместо {_decimal_text(expected_video_duration)} с)."
        )
    expected_container_duration = source_duration if source_has_audio else expected_video_duration
    container_tolerance = max(
        Decimal("0.100"),
        expected_container_duration * Decimal("0.01"),
    )
    if abs(output_duration - expected_container_duration) > container_tolerance:
        delta = abs(output_duration - expected_container_duration)
        raise VideoRebuildError(
            "Проверка итогового видео не пройдена: длительность контейнера отличается "
            f"от ожидаемой: получено {_decimal_text(output_duration)} с, ожидалось "
            f"{_decimal_text(expected_container_duration)} с, разница "
            f"{_decimal_text(delta)} с, допуск {_decimal_text(container_tolerance)} с."
        )


def _verify_audio_preserved(
    source_probe: _MediaProbe,
    source_has_audio: bool,
    output_probe: _MediaProbe,
) -> None:
    output_has_audio = output_probe.audio_stream_count > 0
    if output_has_audio != source_has_audio:
        expected = "присутствовать" if source_has_audio else "отсутствовать"
        raise VideoRebuildError(
            f"Проверка итогового видео не пройдена: аудиопоток должен {expected}."
        )
    if source_has_audio:
        source_audio_frames = len(source_probe.audio_frames)
        output_audio_frames = len(output_probe.audio_frames)
        if source_audio_frames != output_audio_frames:
            raise VideoRebuildError(
                "Проверка итогового видео не пройдена: число аудиокадров отличается "
                f"от исходного ({output_audio_frames} вместо {source_audio_frames})."
            )
        source_samples = _audio_sample_count(source_probe.audio_frames)
        output_samples = _audio_sample_count(output_probe.audio_frames)
        if source_samples is not None and output_samples != source_samples:
            raise VideoRebuildError(
                "Проверка итогового видео не пройдена: число аудиосэмплов отличается "
                f"от исходного ({output_samples} вместо {source_samples})."
            )


def _verify_video_timestamps(
    source_timing: _FrameTiming,
    output_timing: _FrameTiming,
) -> None:
    timestamp_tolerance = Decimal("0.001")
    for position, (source_pts, output_pts) in enumerate(
        zip(
            source_timing.normalized_pts,
            output_timing.normalized_pts,
            strict=True,
        )
    ):
        if abs(output_pts - source_pts) > timestamp_tolerance:
            raise VideoRebuildError(
                "Проверка итогового видео не пройдена: метка времени кадра "
                f"{position} отличается от исходной."
            )


def _verify_last_frame_duration(
    source_timing: _FrameTiming,
    output_probe: _MediaProbe,
    output_timing: _FrameTiming,
) -> None:
    output_time_base = _fraction_decimal(output_probe.video_stream.get("time_base"))
    if output_time_base is None or output_time_base <= 0:
        raise VideoRebuildError(
            "Проверка итогового видео не пройдена: ffprobe не вернул корректную "
            "временную базу видеопотока."
        )
    last_frame_duration_tolerance = output_time_base / 2 + Decimal("0.000001")
    expected_last_frame_duration = (
        Decimal(_last_video_duration_ticks(source_timing.durations[-1]))
        / _VIDEO_TIME_BASE_DENOMINATOR
    )
    actual_last_frame_duration = output_timing.durations[-1]
    if (
        abs(actual_last_frame_duration - expected_last_frame_duration)
        > last_frame_duration_tolerance
    ):
        raise VideoRebuildError(
            "Проверка итогового видео не пройдена: длительность последнего кадра "
            f"отличается от исходной: получено "
            f"{_decimal_text(actual_last_frame_duration)} с, ожидалось "
            f"{_decimal_text(expected_last_frame_duration)} с, допуск "
            f"{_decimal_text(last_frame_duration_tolerance)} с."
        )


def _audio_sample_count(frames: tuple[dict[str, Any], ...]) -> int | None:
    values = tuple(_optional_integer(frame.get("nb_samples")) for frame in frames)
    if any(value is None or value < 0 for value in values):
        return None
    return sum(int(value) for value in values if value is not None)


def _integer(value: Any, label: str) -> int:
    parsed = _optional_integer(value)
    if parsed is None:
        raise VideoRebuildError(f"ffprobe вернул некорректный {label}.")
    return parsed


def _optional_integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finite_decimal(value: Any) -> Decimal | None:
    if value in (None, "", "N/A"):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _finite_decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _fraction_decimal(value: Any) -> Decimal | None:
    if not isinstance(value, str) or not value:
        return None
    numerator, separator, denominator = value.partition("/")
    if not separator:
        return _finite_decimal(value)
    left = _finite_decimal(numerator)
    right = _finite_decimal(denominator)
    if left is None or right is None or right == 0:
        return None
    return left / right


def _remove_temporary_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _emit(callback: ProgressCallback | None, **event: Any) -> None:
    if callback is not None:
        callback(dict(event))


__all__ = ["VideoRebuildError", "VideoRebuildResult", "rebuild_video"]
