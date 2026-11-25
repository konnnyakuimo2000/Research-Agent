"""再レビュー（.claude/review-fixes.md）の 2・3。

2. 転載の連鎖（C は B の写し、B は A の写し）を、独立した 2 サイトの裏付けとして数えない
3. 同じ検索語の繰り返しを止めた後も、報告の前の判定（未読のページ・画像の確認）に戻る
"""

from __future__ import annotations

from research_agent.agent import coverage as cov
from research_agent.agent import reprints
from research_agent.agent.guard import check_decision
from research_agent.agent.nodes import sources_text
from research_agent.tiers import canonical

# Tier 2（百科事典・報道）の別々のドメイン。A・B・C は同じ文章
A, B, C = "https://ja.wikipedia.org/wiki/x", "https://www.nhk.or.jp/x", "https://www.asahi.com/x"
D = "https://kotobank.jp/word/x"                      # 中身が独立した別のサイト（Tier 2）
ASPECTS = [{"name": "高さ", "words": ""}]


def fact(url):
    return {"text": "高さは 333 m", "quote": "高さは333メートル", "aspect": "高さ", "source": url}


def row(verified, chain):
    return cov.build_coverage(ASPECTS, verified, {"reprints": chain})[0]


# ---- 2. 転載の連鎖 ----

CHAIN = {canonical(B): canonical(A), canonical(C): canonical(B)}     # 前の版で記録された連鎖


def test_a_chain_of_copies_is_one_site():
    r = row([fact(A), fact(B), fact(C)], CHAIN)
    assert r["independent"] == 1 and r["status"] == cov.WEAK          # 前は A と B の 2 サイトで「確認済み」
    r = row([fact(A), fact(B), fact(C), fact(D)], CHAIN)
    assert r["independent"] == 2 and r["status"] == cov.CONFIRMED      # 独立した別のサイトがあれば確認済み


def test_copies_are_recorded_against_the_original():
    copies = {canonical(B): canonical(A)}
    assert reprints.original_of(C, copies) == C                       # まだ記録のない C は自分自身
    copies[canonical(C)] = reprints.original_of(canonical(B), copies) # C は B に似ていた → 原典 A で記録
    assert copies[canonical(C)] == canonical(A)
    assert reprints.original_of(A, {canonical(A): canonical(B), canonical(B): canonical(A)}) in (A, canonical(B))  # 輪でも止まる


def test_the_source_list_names_the_original():
    sources = [{"title": "A", "url": A, "tier": 2}, {"title": "C", "url": C, "tier": 2}]
    assert sources_text({"reprints": CHAIN}, sources).splitlines()[1] == f"[2] C — {C}（[1] とほぼ同じ文章）"


# ---- 3. 同じ検索語の後 ----

TARGET = {"id": "Q1", "label": "東京タワー", "description": "電波塔", "sitelinks": 100}


def state(**kw):
    return {"request": "東京タワーについて調べて", "target": TARGET, "candidates": [TARGET], "steps": 3,
            "questions_asked": 0, "searched": ["東京タワー"], "aspects": ASPECTS, "coverage": [],
            "reading_list": ["https://www.tokyotower.co.jp/"], "pages_read": [], "verified": [],
            "wants_images": True, "images_checked": False, **kw}


def both(s):
    respond = check_decision(s, {"action": "respond", "reason": ""})
    repeat = check_decision(s, {"action": "search", "reason": "", "query": "東京タワー"})
    return respond, repeat


def test_a_repeated_search_goes_on_to_the_remaining_work():
    respond, repeat = both(state())
    assert respond["action"] == repeat["action"] == "read_pages"          # 前は同じ語の search で即座に報告した
    assert "同じ検索（東京タワー）の繰り返しを防ぐ" in repeat["guard"]
    read = state(pages_read=[{"url": "https://www.tokyotower.co.jp/", "ok": True}], verified=[fact(A)],
                 conflicts_checked_at=1, coverage=[row([fact(A)], {})])
    respond, repeat = both(read)
    assert respond["action"] == repeat["action"] == "find_images"         # 本文の後は画像の確認へ
    for d in (respond, repeat):
        assert not (d["action"] == "search" and d.get("query") in read["searched"])


def test_ambiguity_and_limits_still_win():
    other = {"id": "Q2", "label": "東京タワー", "description": "映画", "sitelinks": 5}
    ambiguous = state(target=None, candidates=[TARGET, other])
    assert check_decision(ambiguous, {"action": "search", "query": "東京タワー"})["action"] == "ask_user"
    late = state(trace=[{"seconds": 10_000}])                           # 時間の上限を過ぎた
    assert check_decision(late, {"action": "search", "query": "東京タワー"})["action"] in ("respond", "find_images")
