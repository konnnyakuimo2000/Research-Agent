"""画像の確信度の判定と候補の並べ替え（設計書 8.2, 8.3）。判定はプログラムが行う。"""

from __future__ import annotations

import re
import unicodedata

from my_llm_app.tiers import EXCLUDED_TIER, host_of, tier_of
from my_llm_app.tools.images import ImageCandidate, image_key

CONFIRMED = "確認済み"
PROBABLE = "おそらく正しい"
REJECTED = "不採用"
REFERENCE = "参考画像"   # 対象そのものではないが関連する画像（L5 の周辺の画像。設計書 8.3）

# 画像を求める依頼かどうかを決める言葉（「図」は「地図」「図書館」にも当たるので入れない）
IMAGE_WORDS = ("画像", "写真", "イメージ図", "見た目", "外観", "ビジュアル")
# Commons のカテゴリは「対象を写した画像」として人が分類したものなので、ページ内の画像より先に確かめる
_STAGE_ORDER = {"L2-Wikidata": 0, "L2-Commonsカテゴリ": 1, "L3-分野別データベース": 1, "L4-ページ内": 2, "L1-画像検索": 3,
                "L2-Commons": 4}


# 画像を断る言い方（このときだけ画像を探さない）
NO_IMAGE = re.compile(r"(画像|写真)(は|も)?(いら|不要|要らない|なし|無し|なくて|無くて)")


# 権利者の許可なしには使えない利用条件（iNaturalist の「all rights reserved」、ジャパンサーチの「著作権あり」など）
_NEEDS_PERMISSION = re.compile(r"all rights reserved|著作権あり|無断転載", re.IGNORECASE)


def needs_permission(license_: str) -> bool:
    """利用に権利者の許可が必要な画像か（利用条件が不明なものは含めない）。"""
    return bool(_NEEDS_PERMISSION.search(license_ or ""))


def choose_shown(accepted: list[dict], limit: int = 3) -> list[dict]:
    """報告に載せる画像。自由に使える対象の画像があれば、許可が必要な画像は載せない。

    自由に使える画像がなければ、許可が必要な画像も（そのことを書いて）載せる。載せなかった画像には withheld を付ける。
    """
    free = [r for r in accepted if not needs_permission(r.get("license", ""))]
    if not any(r["level"] != REFERENCE for r in free):
        return accepted[:limit]
    for r in accepted:
        if r not in free:
            r["withheld"] = "利用に権利者の許可が必要なため、自由に使える画像を優先した"
    return free[:limit]


def asks_for_images(request: str) -> bool:
    """依頼がはっきり画像を求めているか（報告の書き方を変えるため）。"""
    return any(w in request for w in IMAGE_WORDS) and not NO_IMAGE.search(request)


def wants_images(request: str) -> bool:
    """画像を探すか。求められていなくても探す（対象の理解に役立つため）。断られたときだけ探さない。"""
    return not NO_IMAGE.search(request)


def judge(page_tier: int, fetched_ok: bool, context: str, visual: str, text_match: bool = False) -> str:
    """確信度（設計書 8.3）。

    context: 掲載ページの文脈が、対象そのものの画像だと示しているか（high / mid / low）  … V-1（LLM）
    visual:  画像の内容が対象の説明・事実と矛盾しないか（consistent / contradict / unknown） … V-3（LLM）
    page_tier: 掲載ページの Tier                                                         … V-2（プログラム）
    fetched_ok: 画像として使えるか（取得・大きさ）                                        … V-5（プログラム）
    text_match: 画像の中の文字（刻印・型番）が対象の名前と一致するか                        … V-4（照合はプログラム）
    """
    if not fetched_ok or visual == "contradict" or page_tier >= EXCLUDED_TIER:
        return REJECTED
    if text_match:
        # 写っている刻印・型番が対象と一致する。ページの題名が兄弟機種（例: PEN EE のページの EE-3 の写真）でも採る。
        # 文字の読み取りは LLM なので、信頼度の高いページで文脈も合うときだけ確認済みにする
        return CONFIRMED if page_tier <= 2 and context == "high" else PROBABLE
    if context == "low":
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
    seen: dict[str, ImageCandidate] = {}
    kept = []
    for c in candidates:
        key = image_key(c.src)
        if tier_of(c.page_url) == EXCLUDED_TIER:
            continue
        if key in seen:
            # V-4: 同じ画像が別のページにも載っている。別のサイトなら記録する（報告に書く）
            first = seen[key]
            others = first.extra.setdefault("also_on", [])
            if c.page_url != first.page_url and host_of(c.page_url) != host_of(first.page_url)                     and c.page_url not in others:
                others.append(c.page_url)
            continue
        seen[key] = c
        kept.append(c)

    squashed = [_squash(n) for n in names if _squash(n)]

    def key(c: ImageCandidate):
        # 「File:Olympus pen ee3.jpg」と「Olympus PEN EE-3」のような表記の違いは無視して、名前があるかを見る
        text = _squash(f"{c.title} {c.caption} {c.src.rsplit('/', 1)[-1]}")
        named = any(n in text for n in squashed)
        return (c.stage != "L2-Wikidata", tier_of(c.page_url), not named, _STAGE_ORDER.get(c.stage, 9))

    return sorted(kept, key=key)
