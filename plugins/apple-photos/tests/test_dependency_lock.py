"""Apple Photos dependency lock should stay installable on bundled Python."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_photo_library_lock_uses_python_313_compatible_pyobjc() -> None:
    lock_text = (Path(__file__).resolve().parents[1] / "requirements.lock").read_text()

    assert "osxphotos==0.76.1 ; sys_platform == 'darwin'" in lock_text
    assert "pyobjc-core==12.2 ; sys_platform == 'darwin'" in lock_text
    assert "pyobjc-core==9.2 ; sys_platform == 'darwin'" not in lock_text


def test_vendored_bitmath_has_provenance_and_locked_wheel_hash() -> None:
    root = Path(__file__).resolve().parents[1]
    provenance = json.loads((root / "wheel-provenance/bitmath.json").read_text())
    wheel = root / provenance["wheel_file"]
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert digest == provenance["wheel_sha256"]
    lock = (root / "requirements.lock").read_text()
    assert f"--hash=sha256:{digest}" in lock
    assert "bitmath==1.4.0.1" in lock
    assert "file://" not in lock
    assert provenance["source_changes"] == []
    assert (root / provenance["license_file"]).read_text().startswith("The MIT License")
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            (
                f"import sys; sys.path.insert(0, {str(wheel)!r}); import bitmath; "
                "assert bitmath.MiB(1).bytes == 1048576; "
                "assert bitmath.parse_string('1 MiB').bytes == 1048576; "
                "assert bitmath.Byte(1024).best_prefix().value == 1"
            ),
        ],
        check=True,
        timeout=30,
    )


def test_build_recipe_rejects_unverified_source_before_extraction(tmp_path) -> None:
    recipe_path = (
        Path(__file__).resolve().parents[1] / "wheel-provenance/build_bitmath.py"
    )
    spec = importlib.util.spec_from_file_location("bitmath_build", recipe_path)
    assert spec and spec.loader
    recipe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recipe)
    source = tmp_path / "unverified.tar.gz"
    source.write_bytes(b"unverified")
    with pytest.raises(ValueError, match="SHA-256"):
        recipe.verified_extract(source, tmp_path / "extracted")
    assert not (tmp_path / "extracted").exists()
