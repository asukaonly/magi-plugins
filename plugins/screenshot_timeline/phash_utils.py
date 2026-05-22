"""Perceptual-hash utilities for image deduplication.

The Swift helper computes a 64-bit difference hash (dHash) per capture
and returns it as a 16-char lowercase hex string (see helper/PHash.swift).
On the Python side we just need to compare hashes via hamming distance
and decide whether a new capture is too similar to the previous one for
the same window.

Threshold rule of thumb (out of 64 bits):
  0-3  near-identical (cursor blink, minor anti-alias).
  4-8  noticeable change (scroll, typing one line).
  9+   distinct content (page swap, app switch).

We default to 5 — captures within hamming distance ≤ 5 of the previous
one for the same window are treated as redundant and dropped without
writing L1 or keeping the jpgs.
"""
from __future__ import annotations


def hamming_distance(a: str, b: str) -> int:
    """Hamming distance between two 16-char hex dHash strings.

    Returns ``64`` (max possible for 64-bit) when either input is
    malformed — callers should treat that as "no dedup signal" and
    keep the capture rather than risk dropping a real event.
    """
    if not a or not b or len(a) != 16 or len(b) != 16:
        return 64
    try:
        x = int(a, 16) ^ int(b, 16)
    except ValueError:
        return 64
    # Python 3.10+: int.bit_count(); fallback would be bin(x).count("1").
    return x.bit_count()


__all__ = ["hamming_distance"]
