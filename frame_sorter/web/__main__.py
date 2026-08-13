"""Запуск локального веб-интерфейса."""

from __future__ import annotations

import ipaddress
import logging
import os

import uvicorn

from frame_sorter.logging_utils import setup_web_logging, web_runtime_logging_scope


def main() -> None:
    """Запускает локальный веб-интерфейс с единым журналом жизненного цикла."""
    with web_runtime_logging_scope():
        _run_web_service()


def _run_web_service() -> None:
    """Проверяет параметры и удерживает признак веб-процесса до остановки."""
    configured_host = os.environ.get("WEB_HOST", "127.0.0.1").strip()
    if not _is_loopback(configured_host):
        raise ValueError("WEB_HOST должен быть loopback-адресом.")
    host = (
        configured_host[1:-1]
        if configured_host.startswith("[") and configured_host.endswith("]")
        else configured_host
    )
    try:
        port = int(os.environ.get("WEB_PORT", "7863"))
    except ValueError as exc:
        raise ValueError("WEB_PORT должен быть целым числом.") from exc
    if not 1 <= port <= 65_535:
        raise ValueError("WEB_PORT должен находиться в диапазоне 1..65535.")
    reload_enabled = _read_boolean(os.environ.get("WEB_RELOAD"))
    logger = logging.getLogger(__name__) if reload_enabled else setup_web_logging()
    process_id = os.getpid()
    if not reload_enabled:
        logger.info("Запрошен запуск локального веб-сервиса, PID=%s.", process_id)
    try:
        if not reload_enabled:
            logger.info(
                "Запускается локальный веб-сервис на %s:%s, reload=%s, PID=%s.",
                host,
                port,
                reload_enabled,
                process_id,
            )
        uvicorn.run(
            "frame_sorter.web.app:app",
            host=host,
            port=port,
            reload=reload_enabled,
        )
    except Exception:
        if not reload_enabled:
            logger.exception(
                "Локальный веб-сервис аварийно завершён, PID=%s.", process_id
            )
        raise
    finally:
        if not reload_enabled:
            logger.info("Локальный веб-сервис завершён, PID=%s.", process_id)


def _is_loopback(value: str) -> bool:
    host = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _read_boolean(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("WEB_RELOAD должен быть логическим значением.")


if __name__ == "__main__":
    main()
