"""論文（Crossref・arXiv・OpenAlex）と EC（Yahoo!ショッピング・楽天市場）のデータベース（2026-11-28）。

- 論文は「論文」「研究」などを頼まれたときだけ引く。題目の論文は、人の業績の照合（同名の別人）に回さない
- Crossref は図・表の部品を読まず、よく引用されている順に、題か要旨に対象の名前があるものだけ
- EC は「価格」「最安」などを頼まれたときだけ引き、価格に取得日時を添える。ID がなければ引かない（理由を残す）
- Yahoo!・楽天を報告に引用したら、出典の下にクレジットを出す
"""

from __future__ import annotations

import json
from unittest.mock import patch

from research_agent.agent.report import SourceTable, assemble
from research_agent.tools import databases
from research_agent.tools.databases import arxiv, crossref_search, openalex, rakuten, wanted, yahoo_shopping

from test_db_expansion import real_lookup


class Reply:
    def __init__(self, body, status: int = 200):
        self.status_code = status
        self.content = body.encode() if isinstance(body, str) else json.dumps(body).encode()

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def cr_item(title, typ="journal-article", cited=0, doi="10.1/x", abstract=""):
    return {"DOI": doi, "title": [title], "type": typ, "is-referenced-by-count": cited, "abstract": abstract,
            "author": [{"given": "Ada", "family": "Lovelace"}], "issued": {"date-parts": [[2021, 3]]}, "container-title": ["J"]}


def test_crossref_keeps_real_papers_about_the_subject_most_cited_first():
    items = [cr_item("Figure 4: CRISPR module", typ="component", cited=99, doi="10.1/fig"),
             cr_item("Unrelated title", cited=500, doi="10.1/u"),
             cr_item("CRISPR in plants", cited=3, doi="10.1/a"),
             cr_item("Genome editing", cited=40, doi="10.1/b", abstract="<jats:p>We use CRISPR to …</jats:p>")]
    with patch.object(databases.http, "get", return_value=Reply({"message": {"items": items}})) as get:
        out = crossref_search("CRISPR")
    assert [r.source_id for r in out] == ["10.1/b", "10.1/a"]                 # 部品・無関係を除き、引用の多い順
    assert out[0].url == "https://doi.org/10.1/b" and out[0].kind == "研究論文の書誌情報"
    assert "要旨: We use CRISPR to …" in out[0].text and "被引用数（Crossref）: 40" in out[0].text
    assert get.call_args.kwargs["params"]["query.bibliographic"] == "CRISPR"


ARXIV = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry>
<id>http://arxiv.org/abs/1706.03762v7</id><published>2017-06-12T17:57:34Z</published>
<title>Attention Is All You
 Need</title><summary>The dominant sequence transduction models …</summary>
<author><name>Ashish Vaswani</name></author><arxiv:primary_category term="cs.CL"/></entry></feed>"""


def test_arxiv_entries_are_preprints_and_spaced():
    with patch.object(databases.http, "get", return_value=Reply(ARXIV)) as get:
        (r,) = arxiv("Transformer")
    f = dict(r.fields)
    assert (f["論文名"], f["公開日"], f["分野"], f["種類"]) == ("Attention Is All You Need", "2017-06-12", "cs.CL", "プレプリント（査読前）")
    assert r.source_id == "1706.03762v7" and get.call_args.kwargs["min_interval"] == 3.0   # arXiv の求め: 3 秒に 1 回


def test_openalex_rebuilds_the_abstract():
    work = {"display_name": "Attention", "doi": "https://doi.org/10.5555/abc", "publication_year": 2017, "cited_by_count": 9,
            "abstract_inverted_index": {"is": [1], "Attention": [0], "all": [2]}, "authorships": [{"author": {"display_name": "A"}}],
            "primary_location": {"source": {"display_name": "NeurIPS"}}}
    with patch.object(databases.http, "get", return_value=Reply({"results": [work]})):
        (r,) = openalex("Attention")
    assert r.url == "https://doi.org/10.5555/abc" and "要旨: Attention is all" in r.text and dict(r.fields)["掲載誌"] == "NeurIPS"


def test_papers_and_shops_are_looked_up_only_when_asked(monkeypatch):
    cfg = {d["name"]: d for d in databases._config()["databases"]}
    assert wanted(cfg["Crossref（論文）"], "深層学習の論文を調べて") and not wanted(cfg["Crossref（論文）"], "深層学習について")
    assert wanted(cfg["Yahoo!ショッピング"], "iPhone 16 の最安値") and not wanted(cfg["Yahoo!ショッピング"], "iPhone 16 について")
    for k in ("YAHOO_CLIENT_ID", "RAKUTEN_APP_ID", "RAKUTEN_ACCESS_KEY", "RAKUTEN_REFERER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RAKUTEN_APP_ID", "id")
    result = real_lookup("製品・機械", "iPhone 16", "iPhone 16 の価格")
    states = {s["name"]: (s["state"], s["reason"]) for s in result.statuses}
    assert states["Yahoo!ショッピング"] == ("unconfigured", "YAHOO_CLIENT_ID が設定されていない")
    assert states["楽天市場"] == ("unconfigured", "RAKUTEN_ACCESS_KEY・RAKUTEN_REFERER が設定されていない")   # 足りないものだけ


YAHOO = {"totalResultsAvailable": 3, "hits": [
    {"name": "Apple iPhone 16 128GB ブラック", "price": 124800, "url": "https://store.shopping.yahoo.co.jp/s/a.html", "code": "s_a",
     "seller": {"name": "A ストア"}, "review": {"rate": 4.5, "count": 12}, "priceLabel": {"taxable": True}, "janCode": "4549995"},
    {"name": "iPhone 15 ケース", "price": 980, "url": "https://store.shopping.yahoo.co.jp/s/b.html", "seller": {"name": "B"}},
]}


def test_yahoo_items_carry_price_shop_and_when(monkeypatch):
    monkeypatch.setenv("YAHOO_CLIENT_ID", "cid")
    with patch.object(databases.http, "get", return_value=Reply(YAHOO)) as get:
        out = yahoo_shopping("iPhone 16")
    assert len(out) == 1                                                     # 名前が合わないもの（ケース）は除く
    f = dict(out[0].fields)
    assert (f["価格"], f["店"], f["レビュー"], f["JAN コード"]) == ("124,800 円（税込）", "A ストア", "4.5（12 件）", "4549995")
    assert f["取得日時"] and "価格は取得日時のもの" in out[0].text and out[0].kind == "商品の販売情報"
    params = get.call_args.kwargs["params"]
    assert params["appid"] == "cid" and params["in_stock"] == "true" and get.call_args.kwargs["min_interval"] == 1.0


def test_rakuten_sends_both_ids_and_referer_and_reads_either_shape(monkeypatch):
    for k, v in (("RAKUTEN_APP_ID", "uuid"), ("RAKUTEN_ACCESS_KEY", "pk_x"), ("RAKUTEN_REFERER", "https://example.org/")):
        monkeypatch.setenv(k, v)
    item = {"itemName": "Apple iPhone 16 SIMフリー", "itemPrice": 129800, "itemUrl": "https://item.rakuten.co.jp/s/1/",
            "shopName": "S 店", "reviewAverage": 4.2, "reviewCount": 3, "taxFlag": 0, "itemCode": "s:1"}
    for body in ({"Items": [item]}, {"Items": [{"Item": item}]}):            # formatVersion 2 と 1
        with patch.object(databases.http, "get", return_value=Reply(body)) as get:
            (r,) = rakuten("iPhone 16")
        assert dict(r.fields)["価格"] == "129,800 円（税込）" and dict(r.fields)["店"] == "S 店"
    kw = get.call_args.kwargs
    assert kw["params"]["applicationId"] == "uuid" and kw["params"]["accessKey"] == "pk_x"
    assert kw["headers"]["Referer"] == "https://example.org/"
    err = Reply({"errors": {"errorCode": 403, "errorMessage": "REQUEST_CONTEXT_BODY_HTTP_REFERRER_MISSING"}}, 403)
    with patch.object(databases.http, "get", return_value=err):
        try:
            rakuten("iPhone 16")
            raise AssertionError("失敗が伝わらない")
        except RuntimeError as e:
            assert "REFERRER_MISSING" in str(e)


def test_the_report_shows_the_shop_credits_when_they_are_cited():
    url = "https://store.shopping.yahoo.co.jp/s/a.html"
    state = {"request": "iPhone 16 の価格", "aspects": [{"name": "価格"}], "pages": {url: "Yahoo!ショッピング: iPhone 16"},
             "verified": [{"text": "124,800 円", "source": url, "aspect": "価格"}],
             "db_records": {databases_key(url): {"url": url, "database": "Yahoo!ショッピング", "kind": "商品の販売情報"}}}
    table = SourceTable([{"title": "Yahoo!ショッピング: iPhone 16", "url": url, "tier": 4}])
    text = assemble(state, "iPhone 16 は 124,800 円 [1]。", table)
    assert "Webサービス by Yahoo! JAPAN" in text and "Rakuten" not in text


def databases_key(url: str) -> str:
    from research_agent.tiers import canonical
    return canonical(url)
