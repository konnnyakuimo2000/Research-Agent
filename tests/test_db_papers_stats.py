"""分野別データベースの充実化（.claude/database-expansion.md）の 2・3: 論文（CiNii Research・ORCID・Crossref）と統計（e-Stat）。

- CiNii の文献を 1 本ずつにし、DOI があれば Crossref の登録と照合する。対象の ORCID iD と違う ORCID の同名の著者の
  論文は外す。手掛かりがなければ「同定: 未確定」。CiNii と Crossref が同じ DOI を返したら論文 1 本（出典は双方を残す）
- e-Stat: 指定の地域・時点の値と単位を、統計表 ID・時点・集計区分と一緒に返す。秘匿値・欠測値を 0 にしない
API の応答は固定のもの（実際の API には送らない）。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from research_agent.agent.report import SourceTable, collect_sources
from research_agent.tiers import canonical
from research_agent.tools import databases
from research_agent.tools.databases import lookup as real_lookup   # conftest が差し替える前の本物

OWN = "0000-0001-1111-1111"
OTHER = "0000-0002-2222-2222"


def replies(url: str, params: dict | None = None) -> dict:
    if "orcid.org" in url:
        return {"expanded-result": [{"orcid-id": OWN, "given-names": "Taro", "family-names": "Yamada",
                                     "credit-name": "山田太郎", "institution-name": ["青山学院大学"]}]}
    if "cir.nii.ac.jp" in url:
        item = lambda i, doi, title: {"@id": f"https://cir.nii.ac.jp/crid/{i}", "title": title,  # noqa: E731
                                      "dc:creator": ["山田太郎", "佐藤花子"], "prism:publicationName": "情報処理学会論文誌",
                                      "prism:publicationDate": "2024-04", "dc:type": "Article",
                                      "dc:identifier": [{"@type": "cir:DOI", "@value": doi}] if doi else []}
        return {"items": [item(1, "10.1000/OWN.1", "本人の論文"), item(2, "https://doi.org/10.1000/own.1", "本人の論文（別の記録）"),
                          item(3, "10.1000/other.9", "同名の別人の論文"), item(4, "", "DOI のない論文")]}
    if "api.crossref.org/works/10.1000/own.1" in url:
        return {"message": {"DOI": "10.1000/OWN.1", "title": ["本人の論文"], "container-title": ["情報処理学会論文誌"],
                            "published": {"date-parts": [[2024, 4, 15]]},
                            "author": [{"given": "太郎", "family": "山田", "ORCID": f"https://orcid.org/{OWN}"}]}}
    if "api.crossref.org/works/10.1000/other.9" in url:
        return {"message": {"DOI": "10.1000/other.9", "title": ["同名の別人の論文"], "container-title": ["別の雑誌"],
                            "published": {"date-parts": [[2019]]},
                            "author": [{"given": "太郎", "family": "山田", "ORCID": f"https://orcid.org/{OTHER}"}]}}
    raise AssertionError(url)


def test_papers_are_matched_with_crossref_and_another_person_is_left_out(monkeypatch):
    monkeypatch.setenv("CINII_APPID", "x")
    with patch.object(databases, "_json", side_effect=replies), \
            patch.dict(databases.FUNCTIONS, {"ndl_creator": lambda name: []}):
        result = real_lookup("人物", "山田太郎", "山田太郎の論文")
    papers = [r for r in result[0] if r.kind == "論文の書誌情報"]
    assert [r.title for r in papers] == ["CiNii Research: 本人の論文", "CiNii Research: DOI のない論文"]   # 同じ DOI は 1 本、別人は外す
    own = papers[0]
    assert own.url == "https://cir.nii.ac.jp/crid/1" and own.source_id == "10.1000/own.1"
    assert f"同定: ORCID iD {OWN} が著者に登録されている" in own.text
    assert "照合に使った出典: https://cir.nii.ac.jp/crid/1 と https://doi.org/10.1000/own.1" in own.text
    assert dict(own.fields)["Crossref の公開日"] == "2024-4-15" and own.holder_url == "https://doi.org/10.1000/own.1"
    assert "同定: 未確定" in papers[1].text                                  # DOI がなく、照合の手掛かりがない
    assert "Crossref" in result[2] and {s["name"]: s["records"] for s in result.statuses}["Crossref"] == 2
    # 同じ論文の事実を 2 つ確かめても、出典（独立した裏付け）は 1 つ
    state = {"pages": {own.url: own.title}, "verified": [{"source": own.url, "text": "2024 年に発表"},
                                                         {"source": own.url, "text": "掲載誌は情報処理学会論文誌"}],
             "db_records": {canonical(own.url): {"url": own.url, "database": own.database}}}
    assert len(SourceTable(collect_sources(state)).sources) == 1


def test_without_a_single_orcid_the_author_is_left_undetermined(monkeypatch):
    monkeypatch.setenv("CINII_APPID", "x")

    def two_people(url, params=None):
        if "orcid.org" in url:                                               # 同姓同名が 2 人: どちらか決められない
            return {"expanded-result": [{"orcid-id": OWN, "credit-name": "山田太郎"}, {"orcid-id": OTHER, "credit-name": "山田太郎"}]}
        return replies(url, params)
    with patch.object(databases, "_json", side_effect=two_people), \
            patch.dict(databases.FUNCTIONS, {"ndl_creator": lambda name: []}):
        papers = [r for r in real_lookup("人物", "山田太郎")[0] if r.kind == "論文の書誌情報"]
    assert len(papers) == 3 and all("同定: 未確定" in r.text for r in papers)   # 外さず、未確定と書く


STATS_META = {"GET_META_INFO": {"METADATA_INF": {
    "TABLE_INF": {"@id": "0003445078", "STATISTICS_NAME": "国勢調査", "TITLE": {"@no": "1", "$": "男女別人口"}},
    "CLASS_INF": {"CLASS_OBJ": [
        {"@id": "cat01", "CLASS": [{"@code": "0", "@name": "総数"}]},
        {"@id": "area", "CLASS": [{"@code": "14100", "@name": "横浜市"}, {"@code": "14130", "@name": "川崎市"}]},
        {"@id": "time", "CLASS": [{"@code": "2015000000", "@name": "2015年"}, {"@code": "2020000000", "@name": "2020年"}]}]}}}}
STATS_DATA = {"GET_STATS_DATA": {"STATISTICAL_DATA": {"DATA_INF": {
    "NOTE": [{"@char": "-", "$": "該当数値なし"}, {"@char": "x", "$": "秘匿"}],
    "VALUE": [{"@cat01": "0", "@area": "14100", "@time": "2015000000", "@unit": "人", "$": "3724844"},
              {"@cat01": "0", "@area": "14100", "@time": "2020000000", "@unit": "人", "$": "3777491"},
              {"@cat01": "0", "@area": "14130", "@time": "2020000000", "@unit": "人", "$": "1538262"}]}}}}


def stats(url, params=None):
    return STATS_META if url.endswith("getMetaInfo") else STATS_DATA


def test_the_requested_area_and_year_are_taken_with_their_unit():
    with patch.object(databases, "_json", side_effect=stats):
        (rec,) = databases.estat_population("横浜市", "横浜市の2020年国勢調査人口", "0003445078")
    f = dict(rec.fields)
    assert (f["地域"], f["時点"], f["値"], f["単位"], f["集計区分"]) == ("横浜市", "2020年", "3777491", "人", "総数")
    assert f["統計表 ID"] == "0003445078" and f["統計"] == "国勢調査" and rec.kind == "統計表の値"
    assert rec.url == "https://www.e-stat.go.jp/dbview?sid=0003445078" and rec.source_id == "0003445078:14100:2020000000"
    with patch.object(databases, "_json", side_effect=stats):
        (older,) = databases.estat_population("横浜市", "横浜市の2015年の人口", "0003445078")
        (latest,) = databases.estat_population("横浜市", "横浜市の人口", "0003445078")      # 年がなければいちばん新しい時点
    assert dict(older.fields)["値"] == "3724844" and dict(latest.fields)["時点"] == "2020年"


@pytest.mark.parametrize("raw,shown", [("x", "x（秘匿）"), ("-", "-（該当数値なし）"), ("***", "***（数値なし）")])
def test_suppressed_and_missing_values_are_not_turned_into_zero(raw, shown):
    data = {"GET_STATS_DATA": {"STATISTICAL_DATA": {"DATA_INF": {**STATS_DATA["GET_STATS_DATA"]["STATISTICAL_DATA"]["DATA_INF"],
        "VALUE": [{"@cat01": "0", "@area": "14100", "@time": "2020000000", "@unit": "人", "$": raw}]}}}}
    with patch.object(databases, "_json", side_effect=lambda url, params=None: STATS_META if url.endswith("getMetaInfo") else data):
        (rec,) = databases.estat_population("横浜市", "2020年の人口", "0003445078")
    assert dict(rec.fields)["値"] == shown and "値: 0" not in rec.text


def test_estat_needs_an_app_id_and_a_table(monkeypatch):
    monkeypatch.delenv("ESTAT_APPID", raising=False)
    states = {s["name"]: s for s in real_lookup("場所・建造物", "横浜市", "横浜市の人口").statuses}
    assert states["e-Stat（国勢調査の人口）"]["state"] == "unconfigured" and "ESTAT_APPID" in states["e-Stat（国勢調査の人口）"]["reason"]
    monkeypatch.setenv("ESTAT_APPID", "x")
    states = {s["name"]: s for s in real_lookup("場所・建造物", "横浜市", "横浜市の人口").statuses}
    assert states["e-Stat（国勢調査の人口）"]["reason"] == "使う統計表 ID（stats_data_id）が設定されていない"
    assert "e-Stat（国勢調査の人口）" not in {s["name"] for s in real_lookup("場所・建造物", "横浜市", "横浜市の面積").statuses
                                         if s["state"] != "skipped"}                                 # 人口を尋ねなければ引かない
