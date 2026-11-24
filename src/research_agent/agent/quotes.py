"""引用の照合（設計書 3.3 M-4）。

LLM が抜き出した事実には、ページ本文を一字一句写した引用を付けさせる。
その引用が本当に本文にあるかをプログラムで確かめ、ない事実は捨てる（作り話の防止）。
"""

from __future__ import annotations

import re
import unicodedata

MIN_QUOTE_CHARS = 8   # 短すぎる引用（「ある」など）はどこにでも一致するので認めない

_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """全角・半角の違いと空白・改行の違いを無視して比べるための正規化。"""
    return _SPACES.sub("", unicodedata.normalize("NFKC", text))


def quote_in_text(quote: str, text: str) -> bool:
    q = normalize(quote).strip("「」『』\"'。、")
    return len(q) >= MIN_QUOTE_CHARS and q in normalize(text)
