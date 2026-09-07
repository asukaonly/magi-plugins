# Reviewed bitmath wheel

`osxphotos==0.76.1` requires `bitmath>=1.3.3.1,<2.0.0`. Those upstream
releases have no PyPI wheels. Apple Photos therefore carries a wheel built
from the original, unmodified `bitmath==1.4.0.1` source archive. This preserves
the upstream dependency constraint and avoids running a source build on an
end user's machine.

`bitmath.json` records the exact source URL, source SHA-256, wheel SHA-256,
license, Python version, build tools, and reproducibility evidence.
`LICENSE.bitmath` is copied verbatim from that verified source archive and is
also included in the wheel. The source `setup.py` declares two pure Python
packages and a console entry point; it has no native extensions, build-time
dependencies, or downloads. No source changes were applied.

To reproduce with CPython 3.13.5, from the repository root:

```sh
python plugins/apple-photos/wheel-provenance/build_bitmath.py --output /tmp/bitmath-reviewed-build
```

The output directory must be empty. The recipe verifies the source before
extraction, installs only hash-pinned binary build tools into a temporary
environment, and builds offline with a fixed timestamp. Two independent
builds produced byte-identical wheels. The expected result is
`bitmath-1.4.0.1-py3-none-any.whl`, SHA-256
`86423d08f9a60a6da9cde8368551e4e58977f5c0b318ab2f039c070509898569`.

Building is a maintainer-only operation. Runtime installation and CI lock
checks never invoke this recipe. They use the published `wheels/` artifact;
the runtime still requires exact lock hashes and prebuilt wheels only.
`wheels/` contains only regular `.whl` files. Keep provenance outside it.

After any artifact or provenance change, bump the plugin patch version,
stage the complete package, run `bash scripts/refresh.sh apple-photos`, and
commit the generated registry and append-only version history. The wheel and
these provenance files all participate in `package_sha256`.
