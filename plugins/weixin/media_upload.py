"""Client-side encryption + thumbnail helpers for Weixin iLink media upload.

The iLink ``getuploadurl`` → CDN PUT → ``sendmessage(image_item)`` flow
requires the client to:
  1. Generate a random AES-128 key.
  2. AES-128-ECB encrypt the file bytes (PKCS7 padded) — this is the
     ciphertext we PUT to the CDN.
  3. Hand the server the plaintext MD5 + plaintext size + ciphertext
     size + the AES key (base64) so peers can decrypt later via the
     ``encrypt_query_param`` + ``aes_key`` carried in the message.
  4. Same dance for a JPEG thumbnail when posting an image.

Confidence note: the openclaw-weixin TypeScript types
(``GetUploadUrlReq`` / ``GetUploadUrlResp`` / ``CDNMedia``) and the
plugin's README describe these shapes but stop short of the exact
PUT semantics for ``upload_full_url`` and the precise wiring of
``encrypt_query_param`` into ``image_item.media``. This module
implements the unambiguous parts (crypto, sizes, MD5, thumbnail)
in isolation so the upload orchestrator (in ``adapter.py``) and the
HTTP wrappers (in ``api.py``) can be iterated independently if iLink
returns an unexpected error code on first run.
"""
from __future__ import annotations

import base64
import hashlib
import io
import secrets

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


AES_KEY_BYTES = 16  # AES-128
DEFAULT_THUMB_MAX_DIM = 128  # iLink thumb is small — 128px keeps payload tiny


def generate_aes_key() -> bytes:
    """16 cryptographically-random bytes — fresh key per upload."""
    return secrets.token_bytes(AES_KEY_BYTES)


def aes_128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB with PKCS7 padding. Matches the openclaw-weixin spec.

    ECB is normally a bad mode (no IV → identical blocks reveal patterns)
    but the iLink protocol mandates it for image transit. Plaintext is
    PKCS7-padded to a 16-byte multiple before encryption.
    """
    if len(key) != AES_KEY_BYTES:
        raise ValueError(
            f"AES-128 key must be exactly {AES_KEY_BYTES} bytes, got {len(key)}"
        )
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def aes_128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """Round-trip companion to ``aes_128_ecb_encrypt`` — used by tests
    to verify the encrypted bytes recover to the original plaintext."""
    if len(key) != AES_KEY_BYTES:
        raise ValueError(
            f"AES-128 key must be exactly {AES_KEY_BYTES} bytes, got {len(key)}"
        )
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def md5_hex(data: bytes) -> str:
    """Lowercase hex MD5 — what iLink expects for ``rawfilemd5``."""
    return hashlib.md5(data).hexdigest()


def b64encode(data: bytes) -> str:
    """Stdlib base64 wrapper — what iLink expects for ``aes_key`` /
    ``aeskey`` fields in JSON bodies."""
    return base64.b64encode(data).decode("ascii")


def make_thumbnail(
    image_bytes: bytes,
    *,
    max_dim: int = DEFAULT_THUMB_MAX_DIM,
    jpeg_quality: int = 75,
) -> tuple[bytes, int, int]:
    """Render a JPEG thumbnail no larger than ``max_dim`` on the long side.

    Returns ``(jpeg_bytes, width, height)`` so the caller can fill
    ``image_item.thumb_width`` / ``thumb_height`` on the outbound message.
    JPEG is the universal Weixin thumbnail format — using anything else
    breaks the client preview rendering.
    """
    from PIL import Image  # local import: Pillow is a heavy dep at module load

    with Image.open(io.BytesIO(image_bytes)) as img:
        # ``thumbnail`` mutates in place and preserves aspect ratio,
        # only shrinking — never upscaling. RGBA → RGB so we can save
        # as JPEG (which doesn't support alpha).
        rgb = img.convert("RGB")
        rgb.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        return buf.getvalue(), rgb.width, rgb.height


__all__ = [
    "AES_KEY_BYTES",
    "DEFAULT_THUMB_MAX_DIM",
    "aes_128_ecb_decrypt",
    "aes_128_ecb_encrypt",
    "b64encode",
    "generate_aes_key",
    "make_thumbnail",
    "md5_hex",
]
