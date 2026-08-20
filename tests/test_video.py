from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from frame_sorter import video
from frame_sorter.exceptions import ValidationError
from frame_sorter.video import VideoRebuildError, rebuild_video


def _probe_payload(
    timestamps: list[str],
    *,
    duration: str,
    audio: bool,
    last_duration: str = "0.040",
) -> dict[str, Any]:
    streams: list[dict[str, Any]] = [
        {
            "index": 0,
            "codec_type": "video",
            "duration": duration,
            "avg_frame_rate": "25/1",
            "r_frame_rate": "25/1",
            "time_base": "1/90000",
        }
    ]
    if audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "duration": duration,
                "codec_name": "aac",
                "sample_rate": "48000",
            }
        )
    frames = [
        {
            "media_type": "video",
            "stream_index": 0,
            "pts_time": timestamp,
            "best_effort_timestamp_time": timestamp,
            **({"duration_time": last_duration} if index == len(timestamps) - 1 else {}),
        }
        for index, timestamp in enumerate(timestamps)
    ]
    return {
        "streams": streams,
        "frames": frames,
        "format": {"duration": duration},
    }


def _install_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(video.shutil, "which", lambda name: f"C:/tools/{name}.exe")


def test_rebuild_uses_natural_order_and_publishes_verified_mp4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "Кадры O'Brien"
    folder.mkdir()
    for name in ("frame10.jpg", "frame2.jpeg", "frame1.jpg"):
        (folder / name).write_bytes(name.encode("utf-8"))
    original = tmp_path / f"{folder.name}.mp4"
    original.write_bytes(b"original")
    source_payload = _probe_payload(
        ["10.000", "10.033", "10.082"],
        duration="0.120",
        audio=True,
        last_duration="0.038",
    )
    output_payload = _probe_payload(
        ["0.000", "0.033", "0.082"],
        duration="0.120",
        audio=True,
        last_duration="0.038",
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []
    manifest_bytes = b""
    manifest_path: Path | None = None
    temporary_output: Path | None = None

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal manifest_bytes, manifest_path, temporary_output
        calls.append((list(command), dict(kwargs)))
        executable = Path(command[0]).stem.casefold()
        if executable == "ffprobe":
            payload = output_payload if command[-1].endswith(".building.mp4") else source_payload
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(payload, ensure_ascii=False),
                stderr="",
            )
        assert executable == "ffmpeg"
        first_input = command.index("-i") + 1
        manifest_path = Path(command[first_input])
        manifest_bytes = manifest_path.read_bytes()
        temporary_output = Path(command[-1])
        temporary_output.write_bytes(b"rebuilt")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    _install_tools(monkeypatch)
    monkeypatch.setattr(video.subprocess, "run", fake_run)
    events: list[dict[str, Any]] = []

    result = rebuild_video(folder, emit_event=events.append)

    output = tmp_path / f"{folder.name}_sorted.mp4"
    assert result.output_video == output.resolve()
    assert result.frame_count == 3
    assert result.audio_copied is True
    assert result.audio_timestamps_repaired is False
    assert result.output_duration_seconds == pytest.approx(0.120)
    assert result.to_dict()["output_video"] == str(output.resolve())
    assert output.read_bytes() == b"rebuilt"
    assert original.read_bytes() == b"original"
    assert manifest_bytes[:3] != b"\xef\xbb\xbf"
    manifest = manifest_bytes.decode("utf-8")
    assert manifest.index("frame1.jpg") < manifest.index("frame2.jpeg")
    assert manifest.index("frame2.jpeg") < manifest.index("frame10.jpg")
    assert manifest.count("frame10.jpg") == 2
    assert "O'\\''Brien" in manifest
    assert "duration 0.033" in manifest
    assert "duration 0.049" in manifest
    assert "duration 0.038" in manifest
    assert manifest.count("option framerate 90000") == 4
    assert manifest_path is not None and not manifest_path.exists()
    assert temporary_output is not None and not temporary_output.exists()
    ffmpeg_command = next(command for command, _kwargs in calls if Path(command[0]).stem == "ffmpeg")
    assert _option(ffmpeg_command, "-map", occurrence=1) == "0:v:0"
    assert _option(ffmpeg_command, "-map", occurrence=2) == "1:a:0?"
    assert _option(ffmpeg_command, "-map_metadata") == "1"
    assert _option(ffmpeg_command, "-c:v") == "libx264"
    assert _option(ffmpeg_command, "-bf") == "0"
    assert _option(ffmpeg_command, "-crf") == "18"
    assert _option(ffmpeg_command, "-pix_fmt") == "yuv420p"
    assert _option(ffmpeg_command, "-fps_mode:v") == "passthrough"
    assert _option(ffmpeg_command, "-enc_time_base:v") == "1:90000"
    assert _option(ffmpeg_command, "-bsf:v") == (
        "setts=duration=if(eq(N\\,2)\\,3420\\,DURATION)"
    )
    assert _option(ffmpeg_command, "-vf") == "trim=end=0.120"
    assert "-frames:v" not in ffmpeg_command
    assert _option(ffmpeg_command, "-c:a") == "copy"
    assert _option(ffmpeg_command, "-t") == "0.120"
    assert _option(ffmpeg_command, "-movflags") == "+faststart"
    assert "-shortest" not in ffmpeg_command
    assert all(kwargs["shell"] is False for _command, kwargs in calls)
    assert events[0]["phase"] == "probing"
    assert any(
        event.get("type") == "media_probe"
        and event.get("role") == "source"
        and event.get("container_duration_seconds") == "0.120"
        for event in events
    )
    assert any(
        event.get("type") == "media_probe"
        and event.get("role") == "output"
        and event.get("video_timeline_duration_seconds") == "0.120"
        for event in events
    )
    assert events[-1]["phase"] == "completed"


def test_ffmpeg_output_limit_cannot_precede_video_timeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run_command(
        command: list[str],
        *,
        timeout: int,
        operation: str,
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == video.FFMPEG_TIMEOUT_SECONDS
        assert operation == "ffmpeg"
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(video, "_run_command", fake_run_command)

    video._run_ffmpeg(
        "ffmpeg",
        tmp_path / "frames.ffconcat",
        tmp_path / "source.mp4",
        tmp_path / "output.mp4",
        3,
        video.Decimal("0.280000"),
        video.Decimal("0.200000"),
        video.Decimal("0.060000"),
        None,
    )

    command = commands[0]
    assert _option(command, "-t") == "0.280000"
    assert _option(command, "-bsf:v") == (
        "setts=duration=if(eq(N\\,2)\\,18000\\,DURATION)"
    )


def test_inconsistent_aac_timestamps_trigger_sample_preserving_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "frames"
    folder.mkdir()
    (folder / "one.jpg").write_bytes(b"one")
    original = tmp_path / "frames.mp4"
    original.write_bytes(b"source")
    source_payload = _probe_payload(["0.000"], duration="52.416000", audio=True)
    source_payload["streams"][1].update(
        {"sample_rate": "16000", "nb_frames": "3", "duration": "0.192000"}
    )
    source_payload["format"]["duration"] = "0.192000"
    source_payload["frames"].extend(
        {
            "media_type": "audio",
            "stream_index": 1,
            "pts_time": timestamp,
            "best_effort_timestamp_time": timestamp,
            "duration_time": frame_duration,
            "nb_samples": 1024,
        }
        for timestamp, frame_duration in (
            ("0.000000", "0.701813"),
            ("0.701813", "0.064000"),
            ("0.829813", "0.064000"),
        )
    )
    output_payload = _probe_payload(["0.000"], duration="0.192000", audio=True)
    output_payload["streams"][1].update(
        {"sample_rate": "16000", "nb_frames": "3", "duration": "0.192000"}
    )
    output_payload["frames"].extend(
        {
            "media_type": "audio",
            "stream_index": 1,
            "pts_time": f"{index * 0.064:.6f}",
            "best_effort_timestamp_time": f"{index * 0.064:.6f}",
            "duration_time": "0.064000",
            "nb_samples": 1024,
        }
        for index in range(3)
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        if Path(command[0]).stem.casefold() == "ffmpeg":
            Path(command[-1]).write_bytes(b"rebuilt")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        payload = output_payload if command[-1].endswith(".building.mp4") else source_payload
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    _install_tools(monkeypatch)
    monkeypatch.setattr(video.subprocess, "run", fake_run)

    result = rebuild_video(folder)

    command = next(item for item in commands if Path(item[0]).stem.casefold() == "ffmpeg")
    assert _option(command, "-c:a") == "copy"
    assert _option(command, "-bsf:v") == (
        "setts=duration=if(eq(N\\,0)\\,3600\\,DURATION)"
    )
    assert _option(command, "-bsf:a") == (
        "setts=pts=N*1024:dts=N*1024:duration=1024:time_base=1/16000"
    )
    assert "-t" not in command
    assert result.audio_timestamps_repaired is True


def test_consistent_aac_timestamps_keep_plain_stream_copy() -> None:
    frames = tuple(
        {
            "media_type": "audio",
            "stream_index": 1,
            "pts_time": f"{index * 0.064:.6f}",
            "duration_time": "0.064000",
            "nb_samples": 1024,
        }
        for index in range(3)
    )
    probe = video._MediaProbe(
        video_stream={"index": 0},
        video_frames=({},),
        audio_stream_count=1,
        audio_stream={
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "16000",
            "duration": "0.192000",
        },
        audio_frames=frames,
        reported_duration=video.Decimal("0.192000"),
    )

    assert video._audio_timestamp_repair(probe) is None


def test_output_verification_rejects_lost_audio_frames() -> None:
    audio_frame = {"nb_samples": 1024}
    source_probe = video._MediaProbe(
        video_stream={"index": 0},
        video_frames=({},),
        audio_stream_count=1,
        audio_stream={"index": 1},
        audio_frames=(audio_frame, audio_frame, audio_frame),
        reported_duration=video.Decimal("0.192"),
    )
    output_probe = video._MediaProbe(
        video_stream={"index": 0, "time_base": "1/90000"},
        video_frames=({},),
        audio_stream_count=1,
        audio_stream={"index": 1},
        audio_frames=(audio_frame, audio_frame),
        reported_duration=video.Decimal("0.192"),
    )
    timing = video._FrameTiming(
        normalized_pts=(video.Decimal("0"),),
        durations=(video.Decimal("0.040"),),
    )
    expected_duration = video.Decimal("0.192")

    with pytest.raises(VideoRebuildError, match="2 вместо 3"):
        video._verify_rebuilt_video(
            source_probe=source_probe,
            source_timing=timing,
            source_duration=expected_duration,
            source_has_audio=True,
            output_probe=output_probe,
            output_timing=timing,
            output_duration=expected_duration,
        )


def test_output_verification_rejects_collapsed_last_frame_duration() -> None:
    source_probe = video._MediaProbe(
        video_stream={"index": 0},
        video_frames=({}, {}),
        audio_stream_count=0,
        audio_stream=None,
        audio_frames=(),
        reported_duration=video.Decimal("0.080000"),
    )
    output_probe = video._MediaProbe(
        video_stream={"index": 0, "time_base": "1/90000"},
        video_frames=({}, {}),
        audio_stream_count=0,
        audio_stream=None,
        audio_frames=(),
        reported_duration=video.Decimal("0.080000"),
    )
    source_timing = video._FrameTiming(
        normalized_pts=(video.Decimal("0"), video.Decimal("0.040000")),
        durations=(video.Decimal("0.040000"), video.Decimal("0.040000")),
    )
    collapsed_timing = video._FrameTiming(
        normalized_pts=(video.Decimal("0"), video.Decimal("0.040000")),
        durations=(video.Decimal("0.040000"), video.Decimal("0.000011")),
    )
    source_duration = video.Decimal("0.080000")
    output_duration = video.Decimal("0.080000")

    with pytest.raises(VideoRebuildError, match="длительность последнего кадра"):
        video._verify_rebuilt_video(
            source_probe=source_probe,
            source_timing=source_timing,
            source_duration=source_duration,
            source_has_audio=False,
            output_probe=output_probe,
            output_timing=collapsed_timing,
            output_duration=output_duration,
        )


def test_output_verification_rejects_video_timestamp_shift() -> None:
    source_timing = video._FrameTiming(
        normalized_pts=(video.Decimal("0"), video.Decimal("0.040000")),
        durations=(video.Decimal("0.040000"), video.Decimal("0.040000")),
    )
    shifted_timing = video._FrameTiming(
        normalized_pts=(video.Decimal("0"), video.Decimal("0.041001")),
        durations=(video.Decimal("0.041001"), video.Decimal("0.040000")),
    )

    with pytest.raises(VideoRebuildError, match="кадра 1"):
        video._verify_video_timestamps(source_timing, shifted_timing)


def test_output_verification_accepts_last_frame_duration_within_output_tick() -> None:
    probe = video._MediaProbe(
        video_stream={"index": 0, "time_base": "1/90000"},
        video_frames=({}, {}),
        audio_stream_count=0,
        audio_stream=None,
        audio_frames=(),
        reported_duration=video.Decimal("0.080000"),
    )
    source_timing = video._FrameTiming(
        normalized_pts=(video.Decimal("0"), video.Decimal("0.040000")),
        durations=(video.Decimal("0.040000"), video.Decimal("0.040000")),
    )
    rounded_timing = video._FrameTiming(
        normalized_pts=(video.Decimal("0"), video.Decimal("0.040000")),
        durations=(video.Decimal("0.040000"), video.Decimal("0.039994")),
    )

    video._verify_rebuilt_video(
        source_probe=probe,
        source_timing=source_timing,
        source_duration=video.Decimal("0.080000"),
        source_has_audio=False,
        output_probe=probe,
        output_timing=rounded_timing,
        output_duration=video.Decimal("0.080000"),
    )


def test_output_verification_rejects_missing_video_time_base() -> None:
    probe = video._MediaProbe(
        video_stream={"index": 0},
        video_frames=({},),
        audio_stream_count=0,
        audio_stream=None,
        audio_frames=(),
        reported_duration=video.Decimal("0.040000"),
    )
    timing = video._FrameTiming(
        normalized_pts=(video.Decimal("0"),),
        durations=(video.Decimal("0.040000"),),
    )
    duration = video.Decimal("0.040000")

    with pytest.raises(VideoRebuildError, match="временную базу"):
        video._verify_rebuilt_video(
            source_probe=probe,
            source_timing=timing,
            source_duration=duration,
            source_has_audio=False,
            output_probe=probe,
            output_timing=timing,
            output_duration=duration,
        )


def test_container_duration_error_contains_diagnostics() -> None:
    source_probe = video._MediaProbe(
        video_stream={"index": 0},
        video_frames=({"media_type": "video", "stream_index": 0},),
        audio_stream_count=1,
        audio_stream={"index": 1, "codec_type": "audio"},
        audio_frames=(),
        reported_duration=video.Decimal("52.416000"),
    )
    source_timing = video._FrameTiming(
        normalized_pts=(video.Decimal("0"),),
        durations=(video.Decimal("52.373333"),),
    )
    output_timing = video._FrameTiming(
        normalized_pts=(video.Decimal("0"),),
        durations=(video.Decimal("52.373333"),),
    )
    output_probe = video._MediaProbe(
        video_stream={"index": 0, "time_base": "1/90000"},
        video_frames=({"media_type": "video", "stream_index": 0},),
        audio_stream_count=1,
        audio_stream={"index": 1, "codec_type": "audio"},
        audio_frames=(),
        reported_duration=video.Decimal("53.053813"),
    )
    source_duration = video.Decimal("52.416000")
    output_duration = video.Decimal("53.053813")

    with pytest.raises(VideoRebuildError) as caught:
        video._verify_rebuilt_video(
            source_probe=source_probe,
            source_timing=source_timing,
            source_duration=source_duration,
            source_has_audio=True,
            output_probe=output_probe,
            output_timing=output_timing,
            output_duration=output_duration,
        )

    message = str(caught.value)
    assert "получено 53.053813 с" in message
    assert "ожидалось 52.416000 с" in message
    assert "разница 0.637813 с" in message
    assert "допуск 0.52416000 с" in message


def test_failed_command_keeps_bounded_stderr_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = "начало" + "x" * (video._STDERR_TAIL_LIMIT + 100) + "КОНЕЦ"
    monkeypatch.setattr(
        video.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["ffmpeg"],
            9,
            stdout="",
            stderr=detail,
        ),
    )

    with pytest.raises(VideoRebuildError) as caught:
        video._run_command(
            ["ffmpeg", "input with spaces.mp4"],
            timeout=1,
            operation="ffmpeg",
        )

    message = str(caught.value)
    assert "кодом 9" in message
    assert "КОНЕЦ" in message
    assert "начало" not in message
    assert len(message) < video._STDERR_TAIL_LIMIT + 100


def test_source_frame_count_must_equal_jpeg_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "frames"
    folder.mkdir()
    (folder / "b.jpg").write_bytes(b"b")
    (folder / "a.jpeg").write_bytes(b"a")
    (tmp_path / "frames.mp4").write_bytes(b"source")
    payload = _probe_payload(["0.000"], duration="0.040", audio=False)
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    _install_tools(monkeypatch)
    monkeypatch.setattr(video.subprocess, "run", fake_run)

    with pytest.raises(ValidationError, match="не совпадает с числом JPEG"):
        rebuild_video(folder)

    assert len(commands) == 1
    assert Path(commands[0][0]).stem == "ffprobe"
    assert not (tmp_path / "frames_sorted.mp4").exists()


def test_failed_output_verification_removes_all_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "frames"
    folder.mkdir()
    (folder / "one.jpg").write_bytes(b"1")
    (folder / "two.jpg").write_bytes(b"2")
    original = tmp_path / "frames.mp4"
    original.write_bytes(b"source")
    source_payload = _probe_payload(
        ["0.000", "0.040"],
        duration="0.080",
        audio=False,
    )
    invalid_output_payload = _probe_payload(
        ["0.000"],
        duration="0.040",
        audio=False,
    )
    created: list[Path] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if Path(command[0]).stem == "ffmpeg":
            temporary = Path(command[-1])
            created.append(temporary)
            temporary.write_bytes(b"invalid")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        payload = invalid_output_payload if command[-1].endswith(".building.mp4") else source_payload
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    _install_tools(monkeypatch)
    monkeypatch.setattr(video.subprocess, "run", fake_run)

    with pytest.raises(VideoRebuildError, match="ожидалось кадров 2"):
        rebuild_video(folder)

    assert not (tmp_path / "frames_sorted.mp4").exists()
    assert created and all(not path.exists() for path in created)
    assert not list(tmp_path.glob(".frame-sort-*.ffconcat"))


@pytest.mark.parametrize("same_as_source", [False, True])
def test_existing_or_source_output_is_never_overwritten(
    tmp_path: Path,
    same_as_source: bool,
) -> None:
    folder = tmp_path / "frames"
    folder.mkdir()
    (folder / "one.jpg").write_bytes(b"jpeg")
    original = tmp_path / "frames.mp4"
    original.write_bytes(b"source")
    output = original if same_as_source else tmp_path / "already.mp4"
    if not same_as_source:
        output.write_bytes(b"keep")

    with pytest.raises(ValidationError, match="совпадать|уже существует"):
        rebuild_video(folder, output_video=output)

    assert output.read_bytes() == (b"source" if same_as_source else b"keep")


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg не установлен",
)
def test_real_ffmpeg_rebuilds_small_video(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    folder = tmp_path / "real frames"
    folder.mkdir()
    original = tmp_path / "real frames.mp4"
    output = tmp_path / "другой результат.mp4"
    _run_real(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=25:duration=0.16",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.48",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            "setpts=N*N/(25*TB)",
            "-fps_mode:v",
            "vfr",
            "-c:v",
            "libx264",
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-enc_time_base:v",
            "1:90000",
            "-bsf:v",
            "setts=duration=if(eq(N\\,3)\\,10800\\,DURATION)",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(original),
        ]
    )
    _run_real(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(original),
            "-map",
            "0:v:0",
            "-fps_mode:v",
            "passthrough",
            str(folder / "arbitrary-%03d.jpg"),
        ]
    )
    expected_count = len(list(folder.glob("*.jpg")))
    assert expected_count == 4

    result = rebuild_video(folder, original_video=original, output_video=output)
    output_probe = video._probe_media(output, video._resolve_tool("ffprobe"))
    output_timing = video._frame_timing(
        output_probe,
        expected_count=expected_count,
        output=True,
    )

    assert result.frame_count == expected_count
    assert result.audio_copied is True
    assert output_timing.durations[-1] == video.Decimal("0.120000")
    assert output_timing.total_duration == video.Decimal("0.480000")
    assert output.is_file()
    assert output.stat().st_size > 0


def _option(command: list[str], name: str, *, occurrence: int = 1) -> str:
    positions = [index for index, value in enumerate(command) if value == name]
    return command[positions[occurrence - 1] + 1]


def _run_real(command: list[str]) -> None:
    completed = subprocess.run(
        command,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
