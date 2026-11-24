"""対象の名前の照合と、検索結果からの表記ゆれの取り出し。"""

from __future__ import annotations

import re
import unicodedata

from research_agent.agent.quotes import normalize

_LATIN_WORD = r"[a-zà-öø-ÿ]"


def _has_token(token: str, text: str) -> bool:
    """token が text の中に、前後を英数字に挟まれずに出てくるか（「ee-3」は「ee-30」「xee-3」に当てない）。"""
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text))


def mentions(text: str, names: list[str]) -> bool:
    """文に対象の名前が出てくるか。型番のように数字を含む語は、その語だけで一致とみなす。

    型番のある名前は、全文の部分一致より先に型番で見る。型番は前後の英数字との境目を確かめる
    （「Example EE-30」は「EE-3」ではない）。
    """
    t = normalize(text).lower()
    # 型番の境目は、空白を残した形で見る（normalize は空白を消すので「PEN EE-3」が「penee-3」になり、境目が消える）
    spaced = unicodedata.normalize("NFKC", text).lower()
    for name in names:
        n = normalize(name).lower()
        words = [normalize(w).lower() for w in re.split(r"[\s・,，.]+", name)]
        words = [w for w in words if len(w) >= 2]
        model = [w for w in words if len(w) >= 3 and re.search(r"\d", w)]
        if model:
            if any(_has_token(w, spaced) or _has_token(w, t) for w in model):
                return True
            continue                          # 型番のある名前は型番で見る（兄弟機種を通さない）
        if len(n) >= 2 and n in t:
            return True
        # 「DÜRST, Martin Jakob」と「Martin J. Dürst」のように、語順や略し方が違う名前。
        # 名前の語のうち 2 つ以上が出てくれば一致とみなす（ラテン文字の語だけ。語の境目で見る）
        latin = [w for w in words if re.fullmatch(rf"{_LATIN_WORD}+", w)]
        tl = unicodedata.normalize("NFKC", text).lower()
        if len(latin) >= 2 and sum(bool(re.search(rf"(?<!{_LATIN_WORD}){w}(?!{_LATIN_WORD})", tl))
                                   for w in latin) >= 2:
            return True
    return False


def _distance(a: str, b: str) -> int:
    """編集距離（文字の挿入・削除・置き換えの回数）。"""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


_WORD = re.compile(rf"{_LATIN_WORD}{{4,}}", re.IGNORECASE)


def fix_spelling(query: str, names: list[str]) -> tuple[str, list[str]]:
    """検索語の中の、確かめた名前と 1〜2 文字だけ違う語（綴りの誤り）を、名前の表記に直す。

    LLM は検索語を作るたびに名前を書き写すので、「Dürst」を「Dürrst」と書くことがある。誤った綴りで検索すると、
    対象と関係のない結果ばかりになる。ラテン文字の 5 文字以上の語だけを直す（短い語・日本語は直さない）。
    正しい綴りがすでに検索語にあれば、似ているだけの別の語（「Martin」に対する「Martian」）なので直さない。
    直した語の組（「Dürrst→Dürst」）も返す。
    """
    words: dict[str, str] = {}
    for name in names:                       # 先に渡した名前の表記を優先する。大文字だけの表記より普通の書き方を使う
        for w in _WORD.findall(unicodedata.normalize("NFC", name)):
            key = w.lower()
            if key not in words or (words[key].isupper() and not w.isupper()):
                words[key] = w
    fixed: list[str] = []
    present = {w.lower() for w in _WORD.findall(unicodedata.normalize("NFC", query))}

    def repl(m: re.Match) -> str:
        w = m.group(0)
        key = w.lower()
        if key in words or len(key) < 5:
            return w
        limit = 1 if len(key) < 8 else 2
        near = [(d, v) for k, v in words.items()
                if k not in present and k[0] == key[0] and abs(len(k) - len(key)) <= limit
                and 0 < (d := _distance(k, key)) <= limit]
        if not near:
            return w
        right = min(near)[1]
        fixed.append(f"{w}→{right}")
        return right

    out = _WORD.sub(repl, unicodedata.normalize("NFC", query))
    return out, fixed


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
