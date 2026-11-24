"""転載の見分け（設計書 6.3「Tier の調整」）。

文章がほぼ同じページ（Wikipedia の写し、同じ記事の転載など）は、独立した出典として数えない。
判定はプログラムが文章の類似度で行う: 文字 8-gram の集合のジャカード係数を MinHash で見積もる。
状態に持つのはページごとの署名（整数 64 個）だけにする（本文は持たない）。
"""

from __future__ import annotations

import re
import unicodedata
import zlib

SHINGLE = 8              # 文字 8-gram（日本語は語の区切りがないので文字で切る）
PERMUTATIONS = 64
THRESHOLD = 0.6          # 見積もったジャカード係数がこれ以上なら転載とみなす
MIN_CHARS = 200          # これより短い本文は比べない（定型文だけのページを転載と見誤らないように）
_PRIME = (1 << 61) - 1
# 署名に使うハッシュの係数（固定。プロセスをまたいでも同じ署名になるように）
_COEFS = [((i * 0x9E3779B1 + 0x7F4A7C15) % _PRIME | 1, (i * 0x85EBCA77 + 0xC2B2AE3D) % _PRIME)
          for i in range(1, PERMUTATIONS + 1)]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).lower()


def signature(text: str) -> list[int]:
    """本文の MinHash 署名。短すぎる本文は空（比べない）。"""
    t = _normalize(text)
    if len(t) < MIN_CHARS:
        return []
    shingles = {zlib.crc32(t[i:i + SHINGLE].encode()) for i in range(len(t) - SHINGLE + 1)}
    return [min((a * x + b) % _PRIME for x in shingles) for a, b in _COEFS]


def similarity(a: list[int], b: list[int]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x == y for x, y in zip(a, b)) / len(a)


def find_original(sig: list[int], known: dict[str, list[int]]) -> str | None:
    """先に読んだページ（URL → 署名）のうち、この本文とほぼ同じ文章のもの。なければ None。"""
    best, score = None, THRESHOLD
    for url, other in known.items():
        s = similarity(sig, other)
        if s >= score:
            best, score = url, s
    return best
