"""Командный интерфейс анализа и безопасного переименования."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from frame_sorter import __version__
from frame_sorter.exceptions import FrameSorterError
from frame_sorter.logging_utils import setup_logging
from frame_sorter.service import (
    analyze_folder,
    apply_rename,
    preview_rename,
    recover_transaction,
    rebuild_video,
)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise FrameSorterError(f"Некорректные аргументы: {message}")


def build_parser() -> argparse.ArgumentParser:
    """Создаёт parser документированных команд."""
    parser = _ArgumentParser(
        prog="frame-sorter",
        description="Локальная сортировка возвратных кадров без удаления файлов.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    analyze = commands.add_parser("analyze", help="Создать CSV порядка.")
    analyze.add_argument("--folder", required=True, type=Path)
    analyze.add_argument("--output", type=Path, help="Путь итогового CSV.")

    preview = commands.add_parser("preview", help="Проверить CSV без изменений.")
    _add_rename_arguments(preview)

    apply_command = commands.add_parser("apply", help="Применить CSV к исходным именам.")
    _add_rename_arguments(apply_command)

    recover = commands.add_parser("recover", help="Откатить незавершённую транзакцию.")
    recover.add_argument("--folder", required=True, type=Path)
    recover.add_argument("--journal", type=Path)

    rebuild = commands.add_parser("rebuild", help="Собрать новый MP4 с исходным звуком.")
    rebuild.add_argument("--folder", required=True, type=Path)
    rebuild.add_argument("--original-video", type=Path)
    rebuild.add_argument("--output-video", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Выполняет команду и возвращает стабильный код завершения."""
    _configure_console_utf8()
    logger: logging.Logger | None = None
    try:
        logger = setup_logging()
        arguments = build_parser().parse_args(argv)
        if arguments.command == "analyze":
            result = analyze_folder(
                arguments.folder,
                output_csv=arguments.output,
                emit_event=lambda event: _log_event(logger, event),
            )
        elif arguments.command == "preview":
            result = preview_rename(arguments.folder, arguments.csv)
        elif arguments.command == "apply":
            result = apply_rename(
                arguments.folder,
                arguments.csv,
                emit_event=lambda event: _log_event(logger, event),
            )
        elif arguments.command == "recover":
            journal = recover_transaction(arguments.folder, arguments.journal)
            print(f"Восстановление завершено: {journal}")
            return 0
        else:
            result = rebuild_video(
                arguments.folder,
                original_video=arguments.original_video,
                output_video=arguments.output_video,
                emit_event=lambda event: _log_event(logger, event),
            )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        _log_error(logger, "Операция прервана пользователем.")
        return 130
    except (FrameSorterError, OSError, ValueError) as exc:
        _log_error(logger, f"Операция не выполнена: {exc}")
        return 1


def _log_event(logger: logging.Logger, event: dict[str, object]) -> None:
    message = str(event.get("message") or "").strip()
    if message:
        logger.info("%s", message)
    else:
        logger.debug("Событие: %s", event)


def _log_error(logger: logging.Logger | None, message: str) -> None:
    if logger is None:
        print(message, file=sys.stderr)
    else:
        logger.error("%s", message)


def _add_rename_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--folder", required=True, type=Path)
    parser.add_argument("--csv", required=True, type=Path)


def _configure_console_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue


__all__ = ["build_parser", "main"]
