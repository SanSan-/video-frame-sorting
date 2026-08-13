"""Единая настройка журналов CLI и локального веб-сервиса."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGS_DIR = Path(__file__).resolve().parents[1] / "logs"

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_WEB_LOG_MAX_BYTES = 5 * 1024 * 1024
_WEB_LOG_BACKUP_COUNT = 3
WEB_RUNTIME_LOGGING_ENV = "FRAME_SORTER_WEB_RUNTIME"
_web_file_handler: RotatingFileHandler | None = None


@contextmanager
def web_runtime_logging_scope() -> Iterator[None]:
    """Временно включает файловый веб-журнал только для процесса сервера."""
    marker_was_defined = WEB_RUNTIME_LOGGING_ENV in os.environ
    previous_marker = os.environ.get(WEB_RUNTIME_LOGGING_ENV)
    os.environ[WEB_RUNTIME_LOGGING_ENV] = "1"
    try:
        yield
    finally:
        if marker_was_defined and previous_marker is not None:
            os.environ[WEB_RUNTIME_LOGGING_ENV] = previous_marker
        else:
            os.environ.pop(WEB_RUNTIME_LOGGING_ENV, None)


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Настраивает консольный и файловый журнал приложения."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("frame_sorter")
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        formatter = logging.Formatter(_FORMAT)
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        file_handler = logging.FileHandler(
            LOGS_DIR / "video-frame-sorting.log",
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(console)
        logger.addHandler(file_handler)
    for handler in logger.handlers:
        handler.setLevel(level)
    return logger


def setup_web_logging(verbose: bool = False) -> logging.Logger:
    """Подключает приложение и Uvicorn к единому ротационному журналу."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(_FORMAT)
    application_logger = logging.getLogger("frame_sorter")
    application_logger.setLevel(level)
    application_logger.propagate = False
    if not any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in application_logger.handlers
    ):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        application_logger.addHandler(console)
    file_handler = _web_log_handler()
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    if file_handler not in application_logger.handlers:
        application_logger.addHandler(file_handler)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.setLevel(level)
        uvicorn_logger.propagate = False
        if file_handler not in uvicorn_logger.handlers:
            uvicorn_logger.addHandler(file_handler)
    for handler in application_logger.handlers:
        handler.setLevel(level)
    return application_logger


def _web_log_handler() -> RotatingFileHandler:
    global _web_file_handler
    if _web_file_handler is None:
        _web_file_handler = RotatingFileHandler(
            LOGS_DIR / "video-frame-sorting-web.log",
            maxBytes=_WEB_LOG_MAX_BYTES,
            backupCount=_WEB_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    return _web_file_handler


__all__ = [
    "WEB_RUNTIME_LOGGING_ENV",
    "setup_logging",
    "setup_web_logging",
    "web_runtime_logging_scope",
]
