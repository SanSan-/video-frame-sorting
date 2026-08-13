from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


@pytest.fixture
def frame_directory(tmp_path: Path) -> Path:
    folder = tmp_path / "Кадры с пробелом"
    folder.mkdir()
    height, width = 64, 96
    y_gradient = np.linspace(20, 100, height, dtype=np.uint8)[:, None]
    frames: list[np.ndarray] = []
    for index in range(12):
        image = np.repeat(y_gradient, width, axis=1)
        image = np.stack(
            (image, np.clip(image + 25, 0, 255), np.clip(image + 50, 0, 255)),
            axis=2,
        ).astype(np.uint8)
        left = 5 + index * 3
        image[18:46, left : left + 12] = (235, 225, 40)
        frames.append(image)
    frames[8] = frames[1].copy()
    for index, pixels in enumerate(frames):
        Image.fromarray(pixels, mode="RGB").save(
            folder / f"VID_20260813_120000_{index:05d}.jpg",
            format="JPEG",
            quality=95,
        )
    return folder


def content_hashes(folder: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.glob("*.jpg"))
    }

