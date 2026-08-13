"""Отдельный безопасный применитель CSV-порядка кадров."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from frame_sorter.exceptions import FrameSorterError
from frame_sorter.service import apply_rename, preview_rename


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Проверяет CSV-план и только с --apply переименовывает исходные файлы."
        )
    )
    parser.add_argument("--folder", required=True, type=Path)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Явно разрешить двухфазное переименование исходных файлов.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console_utf8()
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.apply:
            result = apply_rename(arguments.folder, arguments.csv)
        else:
            result = preview_rename(arguments.folder, arguments.csv)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        if not arguments.apply:
            print("Предварительный просмотр: файлы не изменены.")
        return 0
    except KeyboardInterrupt:
        print("Операция прервана пользователем.", file=sys.stderr)
        return 130
    except (FrameSorterError, OSError, ValueError) as exc:
        print(f"Операция не выполнена: {exc}", file=sys.stderr)
        return 1


def _configure_console_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue


if __name__ == "__main__":
    raise SystemExit(main())

