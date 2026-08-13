from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_run_web_script_is_windows_powershell_51_safe() -> None:
    data = (PROJECT_ROOT / "run_web.ps1").read_bytes()

    assert not data.startswith(b"\xef\xbb\xbf")
    script = data.decode("ascii")
    assert "-UseBasicParsing" in script
    assert 'health.service -eq "ocr-video-frame-sorting"' in script
    assert "already running" in script
