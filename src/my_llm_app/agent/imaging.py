"""画像の確信度の判定と候補の並べ替え（設計書 8.2, 8.3）。判定はプログラムが行う。"""

from __future__ import annotations

import re
import unicodedata

from my_llm_app.tiers import EXCLUDED_TIER, tier_of
from my_llm_app.tools.images import ImageCandidate, image_key

CONFIRMED = "確認済み"
PROBABLE = "おそらく正しい"
REJECTED = "不採用"

# 画像を求める依頼かどうかを決める言葉（「図」は「地図」「図書館」にも当たるので入れない）
IMAGE_WORDS = ("画像", "写真", "イメージ図", "見た目", "外観", "ビジュアル")
_STAGE_ORDER = {"L2-Wikidata": 0, "L4-ページ内": 1, "L1-画像検索": 2, "L2-Commons": 3}


def wants_images(request: str) -> bool:
    return any(w in request for w in IMAGE_WORDS)


def judge(page_tier: int, fetched_ok: bool, context: str, visual: str) -> str:
    """確信度（設計書 8.3）。

    context: 掲載ページの文脈が、対象そのものの画像だと示しているか（high / mid / low）  … V-1（LLM）
    visual:  画像の内容が対象の説明・事実と矛盾しないか（consistent / contradict / unknown） … V-3（LLM）
    page_tier: 掲載ページの Tier                                                         … V-2（プログラム）
    fetched_ok: 画像として使えるか（取得・大きさ）                                        … V-5（プログラム）
    """
    if not fetched_ok or visual == "contradict" or context == "low" or page_tier >= EXCLUDED_TIER:
        return REJECTED
    if context == "high" and page_tier <= 2:
        return CONFIRMED
    if visual == "unknown" and page_tier >= 3:
        # 一般のサイトで、写っているものが対象と合うか判断できない（読み込み中の白い仮画像など）→ 採らない
        return REJECTED
    if (context == "high" and page_tier <= 4) or (context == "mid" and page_tier <= 2):
        return PROBABLE
    return REJECTED


def _squash(text: str) -> str:
    # 大文字小文字・全角半角・記号・空白の違いを無視して比べるための形
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text)).lower()


def rank(candidates: list[ImageCandidate], names: list[str]) -> list[ImageCandidate]:
    """確かめる順番を決める（横の仕事）。Tier 5 と重複を除き、
    Wikidata の代表画像 → 掲載ページの Tier が高い順 → 説明に対象の名前がある順 → 段階の順。"""
    seen: set[str] = set()
    kept = []
    for c in candidates:
        key = image_key(c.src)
        if key in seen or tier_of(c.page_url) == EXCLUDED_TIER:
            continue
        seen.add(key)
        kept.append(c)

    squashed = [_squash(n) for n in names if _squash(n)]

    def key(c: ImageCandidate):
        # 「File:Olympus pen ee3.jpg」と「Olympus PEN EE-3」のような表記の違いは無視して、名前があるかを見る
        text = _squash(f"{c.title} {c.caption} {c.src.rsplit('/', 1)[-1]}")
        named = any(n in text for n in squashed)
        return (c.stage != "L2-Wikidata", tier_of(c.page_url), not named, _STAGE_ORDER.get(c.stage, 9))

    return sorted(kept, key=key)
