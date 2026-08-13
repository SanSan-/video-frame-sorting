from __future__ import annotations

from pathlib import Path

import pytest

from frame_sorter.exceptions import ValidationError
from frame_sorter.io_utils import validate_frames_directory


def test_unicode_directory_and_continuous_indices(frame_directory: Path) -> None:
    sequence = validate_frames_directory(frame_directory)

    assert sequence.folder == frame_directory.resolve()
    assert len(sequence.frames) == 12
    assert sequence.naming_mode == "preserved-numeric-pattern"
    assert sequence.target_names[0] == "VID_20260813_120000_00000.jpg"


def test_index_gap_is_supported_by_natural_order(frame_directory: Path) -> None:
    (frame_directory / "VID_20260813_120000_00005.jpg").rename(
        frame_directory / "VID_20260813_120000_00020.jpg"
    )

    sequence = validate_frames_directory(frame_directory)

    assert sequence.frames[-1].name == "VID_20260813_120000_00020.jpg"


def test_arbitrary_names_use_generated_targets(frame_directory: Path) -> None:
    for position, path in enumerate(sorted(frame_directory.glob("*.jpg"))):
        path.rename(frame_directory / f"кадр {position * 3 + 1}.jpg")

    sequence = validate_frames_directory(frame_directory)

    assert sequence.naming_mode == "generated-frame-pattern"
    assert sequence.frames[1].name == "кадр 4.jpg"
    assert sequence.target_names[0] == "frame_00000.jpg"


def test_directory_without_jpeg_is_rejected(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    (folder / "notes.txt").write_text("не кадр", encoding="utf-8")

    with pytest.raises(ValidationError, match="нет кадров JPEG"):
        validate_frames_directory(folder)


def test_directory_named_as_jpeg_is_rejected(tmp_path: Path) -> None:
    folder = tmp_path / "frames"
    folder.mkdir()
    (folder / "nested.jpg").mkdir()

    with pytest.raises(ValidationError, match="Ожидался обычный JPEG-файл"):
        validate_frames_directory(folder)
