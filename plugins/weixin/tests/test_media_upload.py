"""Pin the crypto + thumbnail helpers used by Weixin image outbound.

These functions are the source of truth for the bytes that hit iLink's
``getuploadurl`` (rawfilemd5, filesize, thumb_*) and end up referenced by
``image_item.media.aes_key``. A regression here would break image sends
silently — the server would accept the upload and then the recipient's
client would fail to decrypt with no clear diagnostic.
"""
from __future__ import annotations

import io

import pytest

from weixin.media_upload import (
    AES_KEY_BYTES,
    aes_128_ecb_decrypt,
    aes_128_ecb_encrypt,
    b64encode,
    generate_aes_key,
    make_thumbnail,
    md5_hex,
)


def test_generate_aes_key_is_16_bytes():
    """iLink expects AES-128 specifically — wrong key size silently
    produces unreadable ciphertext on the recipient."""
    for _ in range(5):
        key = generate_aes_key()
        assert isinstance(key, bytes)
        assert len(key) == AES_KEY_BYTES == 16


def test_aes_round_trip_recovers_plaintext_exactly():
    """Encrypt → decrypt with the same key must return identical bytes,
    including for sub-block, exact-block, and multi-block lengths."""
    key = generate_aes_key()
    samples = [
        b"",                          # empty (PKCS7 pads to full block)
        b"x",                         # 1 byte
        b"a" * 15,                    # one byte less than a block
        b"b" * 16,                    # exactly one block (PKCS7 adds full pad block)
        b"c" * 17,                    # spills into a second block
        b"d" * 1024,                  # bulk
        bytes(range(256)) * 4,        # high-entropy mix
    ]
    for plaintext in samples:
        cipher = aes_128_ecb_encrypt(plaintext, key)
        assert cipher != plaintext or plaintext == b""  # changed (except trivial)
        assert len(cipher) % 16 == 0
        recovered = aes_128_ecb_decrypt(cipher, key)
        assert recovered == plaintext


def test_aes_rejects_wrong_key_length():
    """Defensive: a non-16-byte key must raise rather than silently
    truncate or pad — that would produce 'works for me, broken in prod'
    bugs that only surface on the recipient device."""
    with pytest.raises(ValueError, match="AES-128 key must be exactly 16"):
        aes_128_ecb_encrypt(b"hello", b"too-short")
    with pytest.raises(ValueError, match="AES-128 key must be exactly 16"):
        aes_128_ecb_decrypt(b"a" * 32, b"too-short")


def test_md5_hex_lowercase_matches_hashlib():
    """iLink's ``rawfilemd5`` field expects lowercase hex (matches the
    convention everywhere else in the protocol)."""
    import hashlib

    payload = b"the quick brown fox"
    assert md5_hex(payload) == hashlib.md5(payload).hexdigest()
    # Specifically lowercase — uppercase would silently mismatch on the
    # server's content check.
    assert md5_hex(b"any") == md5_hex(b"any").lower()


def test_b64encode_is_stdlib_compatible():
    """``image_item.media.aes_key`` is base64 of the AES key, decoded
    by recipients via stdlib base64. Round-trip pin."""
    import base64

    payload = b"\x00\x01\x02\xff\xfe"
    encoded = b64encode(payload)
    assert isinstance(encoded, str)
    assert base64.b64decode(encoded) == payload


def _png_bytes(width: int, height: int, color=(200, 50, 100)) -> bytes:
    """Generate a small PNG so the thumbnail tests don't depend on fixtures."""
    from PIL import Image

    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_thumbnail_shrinks_to_max_dim_and_preserves_aspect():
    """Large image → JPEG thumbnail capped at 128 on the long side,
    with aspect ratio preserved."""
    large = _png_bytes(800, 400)
    thumb, w, h = make_thumbnail(large, max_dim=128)
    assert w == 128
    assert h == 64  # 800:400 = 2:1 → 128:64
    # Sniff JPEG magic.
    assert thumb[:3] == b"\xff\xd8\xff"


def test_thumbnail_does_not_upscale_small_images():
    """Small image (smaller than max_dim) must not be upscaled — that
    would balloon payload size for no quality gain."""
    small = _png_bytes(50, 50)
    _, w, h = make_thumbnail(small, max_dim=128)
    assert (w, h) == (50, 50)


def test_thumbnail_handles_rgba_by_flattening_to_rgb():
    """JPEG can't carry alpha; the helper converts RGBA → RGB so
    Pillow doesn't error out on PNG transparency."""
    from PIL import Image

    img = Image.new("RGBA", (200, 200), (10, 20, 30, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    thumb, w, h = make_thumbnail(buf.getvalue(), max_dim=64)
    assert (w, h) == (64, 64)
    assert thumb[:3] == b"\xff\xd8\xff"
