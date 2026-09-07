"""Vendored wheel resolution is read-only, portable and independent of source builds."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "lock_deps", Path(__file__).with_name("lock-deps.py")
)
assert SPEC and SPEC.loader
locks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(locks)


def test_vendored_wheel_lock_is_portable_and_contains_artifact_hash(
    tmp_path, monkeypatch
):
    if shutil.which("uv") is None:
        pytest.fail("Lockfile tests require the CI-pinned uv executable")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    run = locks.subprocess.run

    def offline_run(command, **kwargs):
        assert "--no-build" in command
        return run([*command, "--no-index", "--offline"], **kwargs)

    monkeypatch.setattr(locks.subprocess, "run", offline_run)
    outputs = []
    wheel_bytes = None
    for location in ["first-checkout", "relocated checkout"]:
        package = tmp_path / location
        directory = package / "wheels"
        directory.mkdir(parents=True)
        wheel = directory / "vendored_lock_probe-1.0.0-py3-none-any.whl"
        if wheel_bytes is None:
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "vendored_lock_probe-1.0.0.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: vendored-lock-probe\nVersion: 1.0.0\n",
                )
                archive.writestr(
                    "vendored_lock_probe-1.0.0.dist-info/WHEEL",
                    "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                )
                archive.writestr(
                    "vendored_lock_probe-1.0.0.dist-info/RECORD",
                    "vendored_lock_probe-1.0.0.dist-info/METADATA,,\n"
                    "vendored_lock_probe-1.0.0.dist-info/WHEEL,,\n"
                    "vendored_lock_probe-1.0.0.dist-info/RECORD,,\n",
                )
            wheel_bytes = wheel.read_bytes()
        else:
            wheel.write_bytes(wheel_bytes)
        output = locks.compile_lock(["vendored-lock-probe==1.0.0"], package)
        assert "vendored-lock-probe==1.0.0" in output
        assert f"--hash=sha256:{hashlib.sha256(wheel_bytes).hexdigest()}" in output
        assert "file://" not in output and str(tmp_path) not in output
        assert "--find-links" not in output
        assert wheel.read_bytes() == wheel_bytes
        outputs.append(output)
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    "kind", ["symlink_directory", "symlink_file", "directory", "sdist"]
)
def test_lock_generation_rejects_unreviewed_wheel_sources(tmp_path, kind):
    package = tmp_path / "package"
    package.mkdir()
    directory = package / "wheels"
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        if kind == "symlink_directory":
            directory.symlink_to(outside, target_is_directory=True)
        else:
            directory.mkdir()
            if kind == "symlink_file":
                (directory / "probe.whl").symlink_to(outside / "missing.whl")
            elif kind == "directory":
                (directory / "nested.whl").mkdir()
            else:
                (directory / "source.tar.gz").touch()
    except OSError:
        if os.name == "nt":
            pytest.skip("Creating symlinks requires Windows symlink privileges")
        raise
    with pytest.raises(ValueError, match="wheels"):
        locks.compile_lock(["demo==1.0.0"], package)
