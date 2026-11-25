"""読み取りに送るページの本文を切ったときは、その印を別の項目に書く（LLM とのやり取りのログで分かるように）。
本文そのものは変えない（引用の照合は、送った本文で行うため）。"""

from __future__ import annotations

import json

from research_agent.agent import prompts


def test_a_cut_page_says_how_much_was_left_out():
    state = {"request": "東京タワーの高さ", "aspects": [{"name": "高さ"}], "coverage": []}
    cut = json.loads(prompts.extract_user(state, "東京タワー", "あ" * 8000, 2, full_chars=31234))
    assert cut["ページの本文"] == "あ" * 8000
    assert cut["本文の切り詰め"] == "先頭 8,000 文字だけを送った（全体 31,234 文字。残り 23,234 文字は送っていない）"
    whole = json.loads(prompts.extract_user(state, "東京タワー", "短い本文", 2, full_chars=4))
    assert "本文の切り詰め" not in whole
