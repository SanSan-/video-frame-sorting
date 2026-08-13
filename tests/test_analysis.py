from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import pytest
import numpy as np

from frame_sorter.analysis import _adjacent_costs, _find_disruptive_returns
from frame_sorter.exceptions import ValidationError
from frame_sorter.models import AnalysisSettings
from frame_sorter.service import analyze_folder


def test_analysis_creates_two_column_csv_and_groups_return(
    frame_directory: Path, tmp_path: Path
) -> None:
    output = tmp_path / "план.csv"

    result = analyze_folder(frame_directory, output_csv=output)

    assert result.frame_count == 12
    assert result.duplicate_count >= 1
    assert result.total_cost_after < result.total_cost_before
    with output.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0].keys() == {"position", "source_filename"}
    assert len(rows) == 12
    assert [int(row["position"]) for row in rows] == list(range(12))
    assert len({row["source_filename"] for row in rows}) == 12
    names = [row["source_filename"] for row in rows]
    source_position = names.index("VID_20260813_120000_00001.jpg")
    duplicate_position = names.index("VID_20260813_120000_00008.jpg")
    assert duplicate_position == source_position + 1
    assert output.read_bytes()[:3] != b"\xef\xbb\xbf"

    metadata = json.loads(output.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert metadata["frame_count"] == 12
    assert metadata["summary"]["duplicate_count"] >= 1
    assert len(metadata["snapshot"]) == 12


def test_analysis_emits_monotonic_terminal_progress(
    frame_directory: Path, tmp_path: Path
) -> None:
    events: list[dict[str, object]] = []

    analyze_folder(
        frame_directory,
        output_csv=tmp_path / "events.csv",
        emit_event=events.append,
    )

    assert events[0]["phase"] == "loading"
    assert events[-1]["phase"] == "completed"
    assert events[-1]["processed"] == 12


@pytest.mark.parametrize(
    "bypass_ratio",
    [0.0, 1.0, -0.1, 1.1, math.nan, math.inf, -math.inf],
)
def test_analysis_rejects_bypass_ratio_outside_finite_open_interval(
    frame_directory: Path,
    tmp_path: Path,
    bypass_ratio: float,
) -> None:
    settings = AnalysisSettings(bypass_ratio=bypass_ratio)

    with pytest.raises(ValidationError, match="Коэффициент обхода"):
        analyze_folder(
            frame_directory,
            output_csv=tmp_path / "invalid-ratio.csv",
            settings=settings,
        )


def test_analysis_reports_corrupt_jpeg_as_validation_error(
    frame_directory: Path, tmp_path: Path
) -> None:
    corrupt = frame_directory / "VID_20260813_120000_00003.jpg"
    corrupt.write_bytes(b"not-a-jpeg")

    with pytest.raises(ValidationError, match="Не удалось декодировать"):
        analyze_folder(frame_directory, output_csv=tmp_path / "corrupt.csv")


def test_strict_known_lag_accepts_near_duplicate_when_bypass_is_not_smooth() -> None:
    thumbnails = np.full((10, 1, 1), 180, dtype=np.uint8)
    thumbnails[1, 0, 0] = 0
    thumbnails[7, 0, 0] = 100
    thumbnails[8, 0, 0] = 0
    thumbnails[9, 0, 0] = 200
    hashes = np.ones((10, 16), dtype=np.bool_)
    hashes[1] = False
    hashes[8] = False

    matches = _find_disruptive_returns(
        thumbnails,
        hashes,
        _adjacent_costs(thumbnails),
        AnalysisSettings(),
        emit_event=None,
        cancel_check=None,
    )

    match = next(item for item in matches if item.duplicate_index == 8)
    assert match.source_index == 1
    assert match.acceptance_mode == "strict-known-lag"
    assert match.bypass_jump >= 0.75 * min(match.left_jump, match.right_jump)


def test_strict_similarity_does_not_override_unknown_lag() -> None:
    thumbnails = np.full((10, 1, 1), 180, dtype=np.uint8)
    thumbnails[2, 0, 0] = 0
    thumbnails[7, 0, 0] = 100
    thumbnails[8, 0, 0] = 0
    thumbnails[9, 0, 0] = 200
    hashes = np.ones((10, 16), dtype=np.bool_)
    hashes[2] = False
    hashes[8] = False

    matches = _find_disruptive_returns(
        thumbnails,
        hashes,
        _adjacent_costs(thumbnails),
        AnalysisSettings(),
        emit_event=None,
        cancel_check=None,
    )

    assert all(item.duplicate_index != 8 for item in matches)
