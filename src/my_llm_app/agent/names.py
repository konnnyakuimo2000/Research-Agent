"""対象の名前の照合と、検索結果からの表記ゆれの取り出し。"""

from __future__ import annotations

import re
import unicodedata

from my_llm_app.agent.quotes import normalize

_LATIN_WORD = r"[a-zà-öø-ÿ]"


def mentions(text: str, names: list[str]) -> bool:
    """文に対象の名前が出てくるか。型番のように数字を含む語は、その語だけで一致とみなす。"""
    t = normalize(text).lower()
    for name in names:
        n = normalize(name).lower()
        if len(n) >= 2 and n in t:
            return True
        words = [normalize(w).lower() for w in re.split(r"[\s・,，.]+", name)]
        words = [w for w in words if len(w) >= 2]
        model = [w for w in words if len(w) >= 3 and re.search(r"\d", w)]
        if model:
            if any(w in t for w in model):
                return True
            continue                          # 型番のある名前は型番で見る（兄弟機種を通さない）
        # 「DÜRST, Martin Jakob」と「Martin J. Dürst」のように、語順や略し方が違う名前。
        # 名前の語のうち 2 つ以上が出てくれば一致とみなす（ラテン文字の語だけ。語の境目で見る）
        latin = [w for w in words if re.fullmatch(rf"{_LATIN_WORD}+", w)]
        tl = unicodedata.normalize("NFKC", text).lower()
        if len(latin) >= 2 and sum(bool(re.search(rf"(?<!{_LATIN_WORD}){w}(?!{_LATIN_WORD})", tl))
                                   for w in latin) >= 2:
            return True
    return False


# 題名の区切り（「研究者情報 - DÜRST, Martin J. | 青山学院大学」）
_TITLE_SEP = re.compile(r"\s+[-–—:：|｜]\s+|[|｜【】「」]")
_PAREN = re.compile(r"[（(][^）)]*[）)]")


def same_name(a: str, b: str) -> bool:
    return normalize(a).lower() == normalize(b).lower()


def name_variants(titles: list[str], label: str) -> dict[str, int]:
    """検索結果の題名から、対象の名前の別の表記と、それが出てきた題名の数を取り出す（疑似適合フィードバック）。

    題名を区切りで分け、対象の名前と一致する（mentions）短い区切りだけを表記ゆれとみなす。
    「Olympus PEN EE-3 Half Frame 35mm Camera With …」のような長い題名は名前ではないので除く。
    """
    limit = max(len(label) + 12, int(len(label) * 1.5))
    counts: dict[str, int] = {}
    for title in titles:
        in_title: set[str] = set()
        for seg in _TITLE_SEP.split(_PAREN.sub(" ", title)):
            seg = re.sub(r"\s+", " ", seg).strip(" .,、。…")
            if not (2 <= len(seg) <= limit and mentions(seg, [label])) or same_name(seg, label):
                continue
            key = next((k for k in counts if same_name(k, seg)), seg)
            if key not in in_title:
                in_title.add(key)
                counts[key] = counts.get(key, 0) + 1
    return counts
