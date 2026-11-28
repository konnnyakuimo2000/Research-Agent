"""分野別データベースの充実化（.claude/database-expansion.md）の 1: 国立国会図書館サーチ・ジャパンサーチ。

- 国会図書館の応答を資料 1 冊ずつに分け、個別の URL・資料 ID・書名・著者・刊行年を持たせる（前は複数の書誌を
  1 件にまとめ、検索結果の一覧の URL を付けていた）
- ジャパンサーチは提供機関・所蔵元のページ・項目 ID を構造にして出典に渡す
- 依頼で求められた項目に合うデータベースだけを引く（「東京タワーの高さ」で、題名に建物名を含む本や写真を読まない）
- データベースごとの結果（設定なし・0 件・失敗・取得）と、報告に引用した資料の数を区別する
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from research_agent.agent.report import SourceTable, cited_db_records, collect_sources, sources_text
from research_agent.tools import databases
from research_agent.tools.databases import lookup as real_lookup   # conftest が差し替える前の本物

NDL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">
<channel><title>東京タワー</title>
<item><title>東京タワー50年</title><link>https://ndlsearch.ndl.go.jp/books/R100000002-I000009876543</link>
  <dc:title>東京タワー50年 : 戦後日本人の"熱き思い"を</dc:title><dc:creator>鮫島敦</dc:creator>
  <dc:publisher>日本経済新聞出版社</dc:publisher><dcterms:issued>2008.12</dcterms:issued></item>
<item><title>東京タワーの建設</title><link>https://ndlsearch.ndl.go.jp/books/R100000002-I000001234567</link>
  <dc:title>東京タワーの建設</dc:title><dc:creator>内藤多仲</dc:creator><dc:creator>竹中工務店</dc:creator>
  <dcterms:issued>1959</dcterms:issued></item>
<item><title>大阪の塔</title><link>https://ndlsearch.ndl.go.jp/books/R100000002-I000005555555</link>
  <dc:title>大阪の塔</dc:title></item>
<item><title>一覧だけの項目</title><dc:title>東京タワー（一覧）</dc:title></item>
</channel></rss>"""


def ndl_reply(*_, **__):
    return SimpleNamespace(content=NDL_XML.encode("utf-8"), raise_for_status=lambda: None)


def test_two_books_become_two_records_with_their_own_pages():
    with patch.object(databases.http, "get", side_effect=ndl_reply):
        recs = databases.ndl("東京タワー")
    assert [r.url for r in recs] == ["https://ndlsearch.ndl.go.jp/books/R100000002-I000009876543",
                                     "https://ndlsearch.ndl.go.jp/books/R100000002-I000001234567"]   # 題名が違う本・一覧だけの項目は入れない
    a, b = recs
    assert a.source_id == "R100000002-I000009876543" and a.kind == "書誌情報"
    assert dict(a.fields) == {"書名": '東京タワー50年 : 戦後日本人の"熱き思い"を', "著者": "鮫島敦",
                              "出版者": "日本経済新聞出版社", "刊行年": "2008.12", "資料 ID": "R100000002-I000009876543"}
    assert dict(b.fields)["著者"] == "内藤多仲、竹中工務店" and "刊行年: 1959" in b.text
    assert a.text.startswith("国立国会図書館サーチの書誌情報（資料 1 冊）")
    # 報告で 1 冊だけを引用 → その本のページを直接開ける・引用した資料は 1 件
    state = {"db_records": {}, "pages": {}, "verified": [{"source": b.url, "text": "東京タワーの建設は 1959 年刊"}]}
    from research_agent.tiers import canonical
    state["db_records"] = {canonical(r.url): {"url": r.url, "title": r.title, "database": r.database,
                                              "source_id": r.source_id, "kind": r.kind} for r in recs}
    state["pages"] = {r.url: r.title for r in recs}
    table = SourceTable(collect_sources(state))
    text = sources_text(state, table.sources)
    assert text == ("[1] 国立国会図書館サーチ: 東京タワーの建設 — https://ndlsearch.ndl.go.jp/books/R100000002-I000001234567"
                    "（書誌情報 · 資料 ID R100000002-I000001234567）")
    assert cited_db_records(state, table) == [{"url": b.url, "database": "国立国会図書館サーチ",
                                               "source_id": "R100000002-I000001234567", "kind": "書誌情報"}]


def test_japan_search_items_carry_the_provider_and_the_holders_page():
    reply = {"list": [{"id": "photoarchive-123", "common": {
        "title": "東京タワー", "provider": "写真アーカイブ", "contributor": "秋山忠右", "temporal": "1989",
        "linkUrl": "https://photo.example.jp/items/123", "thumbnailUrl": ["https://img.example.jp/123.jpg"],
        "contentsRightsType": "ccby"}}]}
    with patch.object(databases, "_json", return_value=reply):
        (rec,) = databases.japan_search("東京タワー")
    assert rec.url == "https://jpsearch.go.jp/item/photoarchive-123" and rec.source_id == "photoarchive-123"
    assert rec.kind == "所蔵品情報" and rec.holder_url == "https://photo.example.jp/items/123"
    assert dict(rec.fields) == {"名称": "東京タワー", "提供機関": "写真アーカイブ", "所蔵元のページ": "https://photo.example.jp/items/123"}
    from research_agent.tiers import canonical
    state = {"pages": {rec.url: rec.title}, "verified": [{"source": rec.url, "text": "1989 年の写真"}],
             "db_records": {canonical(rec.url): {"url": rec.url, "database": rec.database, "kind": rec.kind,
                                                 "source_id": rec.source_id, "holder_url": rec.holder_url}}}
    assert sources_text(state, collect_sources(state)).endswith(
        "（所蔵品情報 · 資料 ID photoarchive-123） 所蔵元: https://photo.example.jp/items/123")


def test_only_databases_the_request_asks_for_are_queried():
    called = []
    fake = {k: MagicMock(side_effect=lambda name, k=k: called.append(k) or []) for k in databases.FUNCTIONS}
    with patch.dict(databases.FUNCTIONS, fake):
        height = real_lookup("場所・建造物", "東京タワー", "東京タワーの高さ")
        photo = real_lookup("場所・建造物", "東京タワー", "東京タワーの昔の写真")
        general = real_lookup("場所・建造物", "東京タワー", "東京タワーについて調べて")
        no_request = real_lookup("場所・建造物", "東京タワー")
    assert height[2] == [] and {s["name"]: s["state"] for s in height.statuses
                                if s["name"] in ("国立国会図書館サーチ", "ジャパンサーチ")} == {
        "国立国会図書館サーチ": "skipped", "ジャパンサーチ": "skipped"}
    assert photo[2] == ["ジャパンサーチ"]
    assert general[2] == ["国立国会図書館サーチ"]                           # 所蔵品（写真）は、求めたときだけ
    # 依頼文がなければ、全体を尋ねるときに引くものだけ（求めたときだけ引く所蔵品・論文・EC は引かない。2026-11-28）
    assert no_request[2] == ["国立国会図書館サーチ"]


def test_the_four_outcomes_are_told_apart(monkeypatch):
    monkeypatch.delenv("CINII_APPID", raising=False)
    found = [databases.Record(f"https://orcid.org/000{i}", f"ORCID: x{i}", "x", "ORCID", source_id=f"000{i}",
                              kind="研究者情報") for i in (1, 2)]
    fake = {"orcid": MagicMock(return_value=found), "ndl_creator": MagicMock(side_effect=httpx.ConnectError("x"))}
    with patch.dict(databases.FUNCTIONS, fake):
        result = real_lookup("人物", "山田太郎", "山田太郎の論文")
    states = {s["name"]: (s["state"], s["reason"], s["records"]) for s in result.statuses}
    assert states["CiNii Research"] == ("unconfigured", "CINII_APPID が設定されていない", 0)   # 認証情報がない
    assert states["ORCID"] == ("ok", "", 2)                                                  # 2 件取得
    assert states["国立国会図書館サーチ（著者）"] == ("failed", "ConnectError", 0)            # 通信の失敗
    assert states["researchmap"][0] == "disabled"
    with patch.dict(databases.FUNCTIONS, {"orcid": MagicMock(return_value=[]), "ndl_creator": MagicMock(return_value=[])}):
        empty = real_lookup("人物", "山田太郎")
    assert {s["name"]: s["state"] for s in empty.statuses}["ORCID"] == "empty"                # 正常に応答して 0 件
    # 2 件取得して 1 件だけ引用 → 引用した資料は 1 件
    from research_agent.tiers import canonical
    state = {"pages": {r.url: r.title for r in found}, "verified": [{"source": found[0].url, "text": "所属は A 大学"}],
             "db_records": {canonical(r.url): {"url": r.url, "database": "ORCID", "source_id": r.source_id, "kind": r.kind} for r in found}}
    assert [c["source_id"] for c in cited_db_records(state, SourceTable(collect_sources(state)))] == ["0001"]
