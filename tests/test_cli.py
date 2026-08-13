from __future__ import annotations

from pathlib import Path
import logging

import pytest

from frame_sorter import cli
from frame_sorter.cli import build_parser, main
from rename_frames import main as rename_main


def test_cli_analysis_and_separate_preview(
    frame_directory: Path,
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "setup_logging", lambda: logging.getLogger("test.cli"))
    plan = tmp_path / "cli.csv"

    assert main(["analyze", "--folder", str(frame_directory), "--output", str(plan)]) == 0
    assert rename_main(["--folder", str(frame_directory), "--csv", str(plan)]) == 0
    assert plan.is_file()


def test_cli_parses_custom_video_paths(tmp_path: Path) -> None:
    arguments = build_parser().parse_args(
        [
            "rebuild",
            "--folder",
            str(tmp_path),
            "--original-video",
            str(tmp_path / "source.mp4"),
            "--output-video",
            str(tmp_path / "result.mp4"),
        ]
    )

    assert arguments.command == "rebuild"
    assert arguments.original_video == tmp_path / "source.mp4"
    assert arguments.output_video == tmp_path / "result.mp4"
