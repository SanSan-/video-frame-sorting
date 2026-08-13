from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from frame_sorter.web.picker import pick_directory, pick_video


def test_picker_returns_resolved_directory(tmp_path: Path, monkeypatch) -> None:
    destroyed: list[bool] = []
    root = SimpleNamespace(
        withdraw=lambda: None,
        attributes=lambda *_args: None,
        destroy=lambda: destroyed.append(True),
    )
    fake_tk = SimpleNamespace(Tk=lambda: root, TclError=RuntimeError)
    fake_dialog = SimpleNamespace(askdirectory=lambda **_kwargs: str(tmp_path))
    fake_tk.filedialog = fake_dialog
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_dialog)

    assert pick_directory() == tmp_path.resolve()
    assert destroyed == [True]


def test_picker_cancel_returns_none(monkeypatch) -> None:
    root = SimpleNamespace(withdraw=lambda: None, attributes=lambda *_args: None, destroy=lambda: None)
    fake_tk = SimpleNamespace(Tk=lambda: root, TclError=RuntimeError)
    fake_dialog = SimpleNamespace(askdirectory=lambda **_kwargs: "")
    fake_tk.filedialog = fake_dialog
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_dialog)

    assert pick_directory() is None


def test_video_picker_opens_existing_source(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "исходное видео.mp4"
    source.write_bytes(b"mp4")
    destroyed: list[bool] = []
    root = SimpleNamespace(
        withdraw=lambda: None,
        attributes=lambda *_args: None,
        destroy=lambda: destroyed.append(True),
    )
    fake_tk = SimpleNamespace(Tk=lambda: root, TclError=RuntimeError)
    fake_dialog = SimpleNamespace(askopenfilename=lambda **_kwargs: str(source))
    fake_tk.filedialog = fake_dialog
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_dialog)

    assert pick_video("source", initial_directory=tmp_path) == source.resolve()
    assert destroyed == [True]


def test_video_picker_selects_new_output_name(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "новое имя.mp4"
    destroyed: list[bool] = []
    root = SimpleNamespace(
        withdraw=lambda: None,
        attributes=lambda *_args: None,
        destroy=lambda: destroyed.append(True),
    )
    fake_tk = SimpleNamespace(Tk=lambda: root, TclError=RuntimeError)
    fake_dialog = SimpleNamespace(asksaveasfilename=lambda **_kwargs: str(output))
    fake_tk.filedialog = fake_dialog
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_dialog)

    assert pick_video("output", initial_directory=tmp_path) == output.resolve()
    assert destroyed == [True]
