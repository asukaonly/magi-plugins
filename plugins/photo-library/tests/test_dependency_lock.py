"""photo-library dependency lock should stay installable on bundled Python."""
from __future__ import annotations

from pathlib import Path


def test_photo_library_lock_uses_python_313_compatible_pyobjc() -> None:
    lock_text = (Path(__file__).resolve().parents[1] / "requirements.lock").read_text()

    assert "osxphotos==0.76.1 ; sys_platform == 'darwin'" in lock_text
    assert "pyobjc-core==12.2 ; sys_platform == 'darwin'" in lock_text
    assert "pyobjc-core==9.2 ; sys_platform == 'darwin'" not in lock_text
