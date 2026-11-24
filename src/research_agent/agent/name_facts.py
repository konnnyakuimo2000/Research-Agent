"""名前の観点（氏名・別名、名称・別名 など）の事実を、プログラムが本文から作る。

ページを読む LLM は、どのページにも書いてある対象の名前を「事実」として取り出さないことが多い。そのため、
名前がはっきり書いてあるページをいくつ読んでも、網羅表の「氏名・別名」が未発見・出典不足のまま残っていた
（2026-11-24 のダーウィン・大原先生、11-21 のマーティン先生の調査）。

対象についての事実が取れたページの本文に、対象の名前（Wikidata の名前・題名や本文で拾った表記ゆれ）が
書いてあれば、その箇所をそのまま引用にした事実を作る。引用は本文の文字そのものなので、引用の照合（M-4）を通る。
別々のサイトに名前があれば、他の観点と同じ決まりで「確認済み」になる。
"""

from __future__ import annotations

import re

from research_agent.agent.quotes import MIN_QUOTE_CHARS

# 名前だけの観点を作る語（観点の名前を「・」で分けた語が、すべてこの中にあれば名前の観点）
NAME_WORDS = {"氏名", "名称", "名前", "別名", "学名", "和名", "本名", "通称", "読み", "表記", "正式名称", "旧称", "略称"}
_HEADS = {"氏名", "名称", "名前", "学名", "和名", "本名", "正式名称"}
_SEP = r"[\s　・･,，.]*"
_NOT_A_NAME = re.compile(r"とは|[？?!！|｜「」【】『』]|https?:|\s[-–—]\s")
MAX_QUOTE = 120


def name_aspect(aspects: list[str]) -> str | None:
    """名前だけの観点（なければ None）。「名称・由来」「氏名・所属」のように名前以外も含む観点は選ばない。"""
    for a in aspects:
        parts = [p for p in re.split(r"[・･/／、,]", a) if p]
        if parts and all(p in NAME_WORDS for p in parts) and any(p in _HEADS for p in parts):
            return a
    return None


def candidates(state: dict, page_terms: list[str] | None = None) -> list[str]:
    """対象の名前の候補（対象の名前が先、表記ゆれは出てきた数の多い順、最後にそのページで見つかった別の呼び方）。
    題名の断片のような名前らしくないものは外す。"""
    label = (state.get("target") or {}).get("label", "")
    variants = sorted(state.get("name_variants", {}).items(), key=lambda kv: -kv[1])
    out: list[str] = []
    for name in [label] + [v for v, _ in variants] + list(page_terms or []):
        name = (name or "").strip(" 　,，.")
        if 2 <= len(name) <= 40 and not _NOT_A_NAME.search(name) and name not in out:
            out.append(name)
    return out


def _pattern(name: str) -> re.Pattern:
    """名前を本文から探す形。語の間の空白・「・」・カンマの違いは許す（「大原剛三」と「大原 剛三」、
    「DÜRST, Martin J.」と「DÜRST Martin J」）。ラテン文字は大文字・小文字を区別しない。"""
    words = [w for w in re.split(r"[\s　・･,，.]+", name) if w]
    if len(words) == 1 and not re.search(r"[A-Za-z]", name):
        body = _SEP.join(map(re.escape, words[0]))          # 漢字・かなの名前は、文字の間の空白も許す
    else:
        body = _SEP.join(map(re.escape, words))
    return re.compile(rf"(?<![A-Za-z]){body}(?![A-Za-z])", re.IGNORECASE)


def _key(s: str) -> str:
    return re.sub(r"[\s　・･,，.]", "", s).lower()


def source_text(title: str, text: str) -> str:
    """名前を探す文字: ページの題名と本文。研究者データベースや個人のページは、名前が題名（「研究者情報 - 大原 剛三」）
    にしかなく、本文の取り出しで見出しが落ちることが多い。題名もページの発行元が書いた文字なので、名前に限って引用元にする。"""
    return f"{title}\n{text}" if title else text


def name_fact(text: str, names: list[str], aspect: str, source: str) -> dict | None:
    """本文（source_text）から、対象の名前が書いてある箇所を探し、それを引用にした事実を作る（見つからなければ None）。

    いちばん良い行を選ぶ: 違う表記が多く並んでいる行（「大原 剛三（オオハラ コウゾウ）」）→ 対象の名前がある行 → 短い行。
    """
    pats = [(n, _pattern(n)) for n in names]
    best = None
    for line in text.split("\n"):
        found = []                                          # (開始, 終わり, 本文の表記)
        for _, p in pats:
            for m in p.finditer(line):
                if not any(s <= m.start() < e or m.start() <= s < m.end() for s, e, _ in found):
                    found.append((m.start(), m.end(), m.group(0)))
        if not found:
            continue
        distinct = list(dict.fromkeys(_key(f[2]) for f in found))
        has_label = bool(names) and bool(pats[0][1].search(line))
        score = (len(distinct), has_label, -len(line))
        if best is None or score > best[0]:
            best = (score, line, sorted(found))
    if best is None:
        return None
    _, line, found = best
    # 引用: 行が長ければ、最初の名前のあたり（少し前から MAX_QUOTE 文字まで）だけを切り出す（本文の文字のまま）。
    # 表の行の端の罫線と空白は外す
    if len(line) > MAX_QUOTE:
        start = max(0, found[0][0] - 20)
        end = min(len(line), start + MAX_QUOTE)
        found = [f for f in found if f[0] >= start and f[1] <= end]
    else:
        start, end = 0, len(line)
    quote = line[start:end]
    stop = quote.find("。", found[0][1] - start)            # 名前のある文で終える
    if stop >= 0:
        quote = quote[:stop + 1]
    quote = quote.strip(" \t|　")
    if len(quote) < MIN_QUOTE_CHARS:
        return None
    seen, shown = set(), []
    for s, e, name in found:
        if name in quote and _key(name) not in seen:     # 切り出した引用にある表記だけを書く
            seen.add(_key(name))
            shown.append(name.strip())
    if not shown:
        return None
    head = "氏名" if "氏名" in aspect or "本名" in aspect else "名称"
    text_out = f"{head}は「{shown[0]}」" + (f"（{'、'.join(shown[1:])} とも書く）" if len(shown) > 1 else "")
    return {"text": text_out, "quote": quote, "aspect": aspect, "source": source, "origin": "name"}
