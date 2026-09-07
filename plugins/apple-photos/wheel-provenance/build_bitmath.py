"""Rebuild the reviewed bitmath wheel; never called by installation or lock checks.

Run with Python 3.13.5 and --output pointing to an empty temporary directory.
The output is reproducible with the pinned build tools and source timestamp.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

SOURCE_URL = "https://files.pythonhosted.org/packages/ed/d4/e6cbc5632347e8326a5ecc49a5a22bc8b195edb885000d67aade392f5f2d/bitmath-1.4.0.1.tar.gz"
SOURCE_SHA256 = "7e64a1e7161887e254dbb910942e265fe840a4cf0cede4e29a1da9754b7334e8"
SOURCE_DATE_EPOCH = "1776384000"
WHEEL_SHA256 = "86423d08f9a60a6da9cde8368551e4e58977f5c0b318ab2f039c070509898569"
ROOT = Path(__file__).resolve().parent


def verified_extract(archive: Path, destination: Path) -> None:
    """Verify the fixed source hash and reject links and escaping archive paths."""
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError("Unexpected bitmath source SHA-256")
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or not (member.isfile() or member.isdir())
                or path.parts[0] != "bitmath-1.4.0.1"
            ):
                raise ValueError("Unsafe bitmath source archive member")
        source.extractall(destination, filter="data")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.version_info[:3] != (3, 13, 5):
        parser.error("Reproduction requires Python 3.13.5")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("Output directory must be empty")
    with tempfile.TemporaryDirectory(prefix="bitmath-wheel-build-") as temporary:
        root = Path(temporary)
        archive = root / "bitmath.tar.gz"
        with urllib.request.urlopen(SOURCE_URL, timeout=30) as response:
            content = response.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("Bitmath source archive exceeds the reviewed size budget")
        archive.write_bytes(content)
        verified_extract(archive, root)
        environment = root / "build-python"
        subprocess.run(
            [sys.executable, "-m", "venv", str(environment)], check=True, timeout=60
        )
        python = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        subprocess.run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "install",
                "--only-binary=:all:",
                "--require-hashes",
                "--force-reinstall",
                "--no-cache-dir",
                "-r",
                str(ROOT / "build-requirements.txt"),
            ],
            check=True,
            timeout=180,
        )
        env = os.environ.copy()
        env.update(
            SOURCE_DATE_EPOCH=SOURCE_DATE_EPOCH, PYTHONHASHSEED="0", PIP_NO_INDEX="1"
        )
        subprocess.run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--use-pep517",
                "--no-cache-dir",
                "--no-index",
                "--wheel-dir",
                str(output),
                ".",
            ],
            cwd=root / "bitmath-1.4.0.1",
            env=env,
            check=True,
            timeout=120,
        )
    wheels = list(output.glob("*.whl"))
    if len(wheels) != 1 or wheels[0].name != "bitmath-1.4.0.1-py3-none-any.whl":
        raise ValueError("Unexpected bitmath wheel output")
    digest = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
    if digest != WHEEL_SHA256:
        raise ValueError("Rebuilt bitmath wheel differs from the reviewed artifact")
    print(f"{wheels[0].name} sha256={digest}")


if __name__ == "__main__":
    main()
