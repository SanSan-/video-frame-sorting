from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from frame_sorter import logging_utils
from frame_sorter.web import __main__ as web_main


def test_logging_is_utf8_and_contains_console_and_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = logging.getLogger("frame_sorter")
    saved = (list(logger.handlers), logger.level, logger.propagate)
    monkeypatch.setattr(logging_utils, "LOGS_DIR", tmp_path)
    logger.handlers = []
    try:
        configured = logging_utils.setup_logging(verbose=True)

        file_handlers = [
            handler
            for handler in configured.handlers
            if isinstance(handler, logging.FileHandler)
        ]
        console_handlers = [
            handler
            for handler in configured.handlers
            if isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
        ]
        assert len(file_handlers) == 1
        assert len(console_handlers) == 1
        assert file_handlers[0].encoding.casefold().replace("-", "") == "utf8"
        assert file_handlers[0].baseFilename == str(tmp_path / "video-frame-sorting.log")

        configured.info("Проверка обычного журнала в UTF-8.")
        file_handlers[0].flush()
        log_path = tmp_path / "video-frame-sorting.log"
        assert "Проверка обычного журнала" in log_path.read_text(encoding="utf-8")
        assert not log_path.read_bytes().startswith(b"\xef\xbb\xbf")
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers, logger.level, logger.propagate = saved


def test_web_logging_is_utf8_rotating_and_captures_app_and_uvicorn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger_names = ("frame_sorter", "uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in logger_names
    }
    monkeypatch.setattr(logging_utils, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(logging_utils, "_WEB_LOG_MAX_BYTES", 320)
    monkeypatch.setattr(logging_utils, "_WEB_LOG_BACKUP_COUNT", 2)
    monkeypatch.setattr(logging_utils, "_web_file_handler", None)
    handler = None
    try:
        logging_utils.setup_web_logging()
        handler = logging_utils._web_log_handler()
        assert handler.encoding.casefold().replace("-", "") == "utf8"
        assert handler.maxBytes == 320
        assert handler.backupCount == 2
        for name in logger_names:
            assert handler in logging.getLogger(name).handlers

        app_logger = logging.getLogger("frame_sorter.web.app")
        job_logger = logging.getLogger("frame_sorter.web.job.test")
        access_logger = logging.getLogger("uvicorn.access")
        for index in range(20):
            app_logger.info("Подготовка каталога, шаг %s: данные в UTF-8.", index)
        job_logger.info("Задание передало ход обработки в файловый журнал.")
        access_logger.info("Локальный HTTP-запрос завершён.")
        handler.flush()

        paths = sorted(tmp_path.glob("video-frame-sorting-web.log*"))
        assert 2 <= len(paths) <= 3
        combined = "".join(path.read_text(encoding="utf-8") for path in paths)
        assert "Подготовка каталога" in combined
        assert combined.count("Задание передало ход обработки") == 1
        assert "Локальный HTTP-запрос" in combined
        assert all(not path.read_bytes().startswith(b"\xef\xbb\xbf") for path in paths)
    finally:
        if handler is not None:
            handler.close()
        for name, (handlers, level, propagate) in saved.items():
            target = logging.getLogger(name)
            target.handlers = handlers
            target.setLevel(level)
            target.propagate = propagate


def test_web_logging_uses_release_rotation_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging_utils, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(logging_utils, "_web_file_handler", None)

    handler = logging_utils._web_log_handler()
    try:
        assert handler.maxBytes == 5 * 1024 * 1024
        assert handler.backupCount == 3
        assert Path(handler.baseFilename) == tmp_path / "video-frame-sorting-web.log"
    finally:
        handler.close()


def test_web_runtime_logging_scope_restores_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = logging_utils.WEB_RUNTIME_LOGGING_ENV
    monkeypatch.setenv(marker, "предыдущее-значение")

    with logging_utils.web_runtime_logging_scope():
        assert os.environ[marker] == "1"

    assert os.environ[marker] == "предыдущее-значение"


def test_web_entrypoint_logs_lifecycle_and_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.web-entrypoint")
    monkeypatch.setattr(web_main, "setup_web_logging", lambda: logger)
    monkeypatch.setattr(web_main.os, "getpid", lambda: 4321)
    monkeypatch.setattr(
        web_main.uvicorn,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("сбой запуска")),
    )
    monkeypatch.delenv("WEB_RELOAD", raising=False)
    caplog.set_level(logging.INFO, logger=logger.name)

    with pytest.raises(RuntimeError, match="сбой запуска"):
        web_main._run_web_service()

    messages = [record.getMessage() for record in caplog.records]
    assert any("Запрошен запуск" in message and "PID=4321" in message for message in messages)
    assert any("Запускается" in message and "PID=4321" in message for message in messages)
    assert any("аварийно завершён" in message and "PID=4321" in message for message in messages)
    assert any("завершён" in message and "PID=4321" in message for message in messages)
    assert any(record.exc_info is not None for record in caplog.records)
