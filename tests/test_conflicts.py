from research_agent.agent import coverage as cov
from research_agent.agent.guard import check_decision
from research_agent.agent.nodes import Nodes

A = "https://www.bousai.go.jp/h17"
B = "https://www.bousai.go.jp/h18"          # A と同じサイト
C = "https://ja.wikipedia.org/wiki/x"


def fact(text, source, aspect="被害・影響"):
    return {"text": text, "quote": text, "aspect": aspect, "source": source}


VERIFIED = [
    fact("死者・行方不明者は30万人以上", A),
    fact("死者・行方不明者は約23万人", C),
    fact("邦人35名の死亡を確認", A),
    fact("邦人40名の死亡を確認", B),                    # A と同じサイト → 食い違いとしては扱わない
    fact("2004年12月26日に発生", A, aspect="日時"),     # 日時は 1 サイトだけ → 比べない
]


def test_facts_to_compare_needs_two_sites():
    groups = cov.facts_to_compare(VERIFIED)
    assert list(groups) == ["被害・影響"]
    assert len(groups["被害・影響"]) == 4


class FakeLLM:
    def __init__(self):
        self.calls = []

    def chat_json(self, system, user, schema, temperature=0.2):
        self.calls.append(user)
        return {"conflicts": [
            {"topic": "死者・行方不明者数", "kind": "値が大きく違う", "fact_ids": [1, 2]},  # 別サイト → 認める
            {"topic": "邦人の死者数", "kind": "値が大きく違う", "fact_ids": [3, 4]},        # 同じサイト → 除く
            {"topic": "存在しない番号", "kind": "値が大きく違う", "fact_ids": [1, 9]},      # 実在しない番号 → 除く
            {"topic": "丸めの違い", "kind": "丸め・概数の違い", "fact_ids": [1, 2]},        # 丸めの違い → 除く
            {"topic": "別の時点", "kind": "別の事柄・時点", "fact_ids": [1, 2]},            # 別の事柄 → 除く
        ]}


def test_check_conflicts_validates_llm_output():
    llm = FakeLLM()
    out = Nodes(None, llm).check_conflicts({"verified": VERIFIED})
    assert len(llm.calls) == 1                               # 比べる観点 1 つにつき 1 回
    assert out["conflicts_checked_at"] == len(VERIFIED)
    assert [(c["topic"], [f["text"] for f in c["facts"]]) for c in out["conflicts"]] == [
        ("死者・行方不明者数", ["死者・行方不明者は30万人以上", "死者・行方不明者は約23万人"])]


TARGET = {"id": "Q1", "label": "地震A", "description": "地震", "sitelinks": 80}


def test_guard_checks_conflicts_once_before_responding():
    base = {"request": "地震Aについて", "target": TARGET, "target_summary": "概要", "candidates": [TARGET],
            "searched": ["地震A"], "steps": 4, "reading_list": ["u1"], "pages_read": [{"url": "u1"}],
            "verified": VERIFIED}
    assert check_decision(base, {"action": "respond", "reason": ""})["action"] == "check_conflicts"
    checked = {**base, "conflicts_checked_at": len(VERIFIED)}
    assert check_decision(checked, {"action": "respond", "reason": ""})["action"] == "respond"
    # 調べた後で事実が増えたら、調べ直す
    more = {**checked, "verified": VERIFIED + [fact("新しい事実", C)]}
    assert check_decision(more, {"action": "respond", "reason": ""})["action"] == "check_conflicts"


def test_respond_lists_conflicts():
    class TextLLM:
        def chat_text(self, system, user, temperature=0.5):
            assert "死者・行方不明者数" in user                 # LLM にも食い違いを渡す
            return "本文"
    conflict = {"aspect": "被害・影響", "topic": "死者・行方不明者数", "facts": VERIFIED[:2]}
    state = {"request": "地震A", "verified": VERIFIED[:2], "conflicts": [conflict], "pages": {}}
    answer = Nodes(None, TextLLM()).respond(state)["answer"]
    assert "### 出典どうしの食い違い" in answer
    assert "- 被害・影響・死者・行方不明者数: 「死者・行方不明者は30万人以上」[1] / 「死者・行方不明者は約23万人」[2]" in answer


def test_same_topic_conflicts_are_merged():
    a, b, c = ({"text": t, "source": f"https://s{i}.example/"} for i, t in enumerate(["約23万人", "30万人以上", "22万人超"]))
    merged = cov.merge_conflicts([
        {"aspect": "被害・影響", "topic": "死者数", "facts": [a, b]},
        {"aspect": "被害・影響", "topic": "死者数", "facts": [b, c]},
        {"aspect": "規模", "topic": "死者数", "facts": [a, c]},
    ])
    assert [(m["aspect"], [f["text"] for f in m["facts"]]) for m in merged] == [
        ("被害・影響", ["約23万人", "30万人以上", "22万人超"]), ("規模", ["約23万人", "22万人超"])]
