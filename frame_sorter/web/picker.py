"""Системный выбор локального каталога и путей MP4."""

from __future__ import annotations

from pathlib import Path
from typing import Literal


class PickerError(RuntimeError):
    """Ошибка системного диалога выбора пути."""


def pick_directory() -> Path | None:
    """Открывает системный диалог и возвращает выбранный каталог."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - зависит от поставки Python
        raise PickerError(f"Не удалось загрузить системный диалог: {exc}") from exc

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="Выберите каталог с кадрами")
        if not selected:
            return None
        path = Path(selected).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise PickerError(f"Каталог не найден: {path}")
        return path
    except PickerError:
        raise
    except (OSError, RuntimeError, tk.TclError) as exc:
        raise PickerError(f"Не удалось выбрать каталог: {exc}") from exc
    finally:
        if root is not None:
            root.destroy()


def pick_video(
    kind: Literal["source", "output"],
    *,
    initial_directory: Path | None = None,
) -> Path | None:
    """Выбирает существующий исходный MP4 или новый путь выходного MP4."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - зависит от поставки Python
        raise PickerError(f"Не удалось загрузить системный диалог: {exc}") from exc

    if kind not in {"source", "output"}:
        raise PickerError(f"Неизвестный режим выбора видео: {kind}")

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = _show_video_dialog(filedialog, kind, initial_directory)
        if not selected:
            return None
        return _validate_video_path(selected, kind)
    except PickerError:
        raise
    except (OSError, RuntimeError, tk.TclError) as exc:
        raise PickerError(f"Не удалось выбрать видео: {exc}") from exc
    finally:
        if root is not None:
            root.destroy()


def _show_video_dialog(
    filedialog: object,
    kind: Literal["source", "output"],
    initial_directory: Path | None,
) -> str:
    options: dict[str, object] = {
        "filetypes": (("Видео MP4", "*.mp4"), ("Все файлы", "*.*")),
    }
    if initial_directory is not None:
        options["initialdir"] = str(initial_directory)
    if kind == "source":
        return filedialog.askopenfilename(  # type: ignore[attr-defined]
            title="Выберите исходный MP4 со звуковой дорожкой",
            **options,
        )
    return filedialog.asksaveasfilename(  # type: ignore[attr-defined]
        title="Выберите новый выходной MP4",
        defaultextension=".mp4",
        confirmoverwrite=False,
        **options,
    )


def _validate_video_path(
    selected: str,
    kind: Literal["source", "output"],
) -> Path:
    path = Path(selected).expanduser().resolve(strict=kind == "source")
    if path.suffix.casefold() != ".mp4":
        raise PickerError("Необходимо выбрать путь с расширением .mp4.")
    if kind == "source" and not path.is_file():
        raise PickerError(f"Исходный MP4 не найден: {path}")
    if kind == "output":
        if not path.parent.is_dir():
            raise PickerError(f"Каталог выходного файла не найден: {path.parent}")
        if path.exists():
            raise PickerError(f"Выходной файл уже существует: {path}")
    return path
