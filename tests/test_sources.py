import json

from research_agent.agent.nodes import collect_sources
from research_agent.agent.prompts import respond_user

WIKI = "https://ja.wikipedia.org/wiki/水星"
STATE = {
    "request": "マーキュリーについて調べて",
    "target_summary": "水星は太陽系の惑星。",
    "summary_url": WIKI,
    # 検索結果の要約から作った手がかり。回答にも出典にも使わない
    "known": [{"text": "要約だけの記述", "source": "https://example.com/snippet-only"}],
    "verified": [
        {"text": "直径は約 4,880 km", "quote": "直径は約4,880km", "source": "https://example.com/a"},
        {"text": "公転周期は 88 日", "quote": "公転周期は88日", "source": WIKI},
        {"text": "表面はクレーターに覆われている", "quote": "表面はクレーターに覆われ", "source": "https://example.com/a"},
    ],
    "pages": {WIKI: "Wikipedia: 水星", "https://example.com/a": "A", "https://example.com/snippet-only": "S"},
}


def test_collect_sources_orders_summary_first_without_duplicates():
    sources = collect_sources(STATE)
    assert [s["url"] for s in sources] == [WIKI, "https://example.com/a"]
    assert sources[0]["title"] == "Wikipedia: 水星"
    assert [s["tier"] for s in sources] == [2, 4]


def test_respond_input_uses_only_verified_facts_with_their_own_numbers():
    data = json.loads(respond_user(STATE, collect_sources(STATE)))
    assert data["対象の概要"].endswith("[1]")
    # 観点が付いていない事実は「その他」にまとまる。
    # example.com は Tier 4（一般のサイト）なので「信頼度: 低」の印が付く。Wikipedia（Tier 2）には付かない
    assert data["確認できた事実（観点ごと）"] == {"その他": [
        "直径は約 4,880 km [2]（出典の信頼度: 低）",
        "公転周期は 88 日 [1]",
        "表面はクレーターに覆われている [2]（出典の信頼度: 低）",
    ]}
    assert "要約だけの記述" not in json.dumps(data, ensure_ascii=False)
