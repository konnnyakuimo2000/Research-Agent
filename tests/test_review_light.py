"""再レビュー（.claude/review-fixes.md）の「軽量化と整理」。

- SSE の配信: 新しいイベントを読むたびに全件をなめていた（1 件ずつ 1,000 件で延べ 500,500 件）。二分探索で読む位置を
  見つけ、新しい分だけを見る
- 止まっている画面の描き直しを休む: ブラウザで確かめた（再生の後 2 秒で描いたフレーム 0・休んだフレーム 120。
  拡大・表示の切り替え・画面の大きさで再開）
画像の取得（先読み）は、利用者の指示で今のまま変えない。
"""

from __future__ import annotations

from research_agent.gateway.store import Feed


def test_delivering_one_event_at_a_time_does_not_rescan_everything():
    feed = Feed()
    delivered, seq = [], 0
    for i in range(1, 1001):
        feed.add({"seq": i})
        new = feed.wait_after(seq, 0)                 # 購読側は、届くたびに続きから読む
        delivered += [e["seq"] for e in new]
        seq = new[-1]["seq"]
    assert delivered == list(range(1, 1001))
    assert feed.scanned == 1000                       # 前は延べ 500,500 件
    assert feed.wait_after(1000, 0) == []


def test_reconnecting_from_the_middle_has_no_gaps_or_repeats():
    feed = Feed()
    for i in range(1, 51):
        feed.add({"seq": i})
    assert [e["seq"] for e in feed.wait_after(37, 0)] == list(range(38, 51))
    assert [e["seq"] for e in feed.wait_after(0, 0)] == list(range(1, 51))
    assert feed.wait_after(50, 0) == []


# ---- CLI（research-agent コマンドと scripts/agent.py が同じ処理を使う） ----

def test_both_command_lines_share_the_same_arguments(capsys):
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    from research_agent import cli

    with pytest.raises(SystemExit) as stop:
        cli.main(["--help"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert "request" in out and "--verbose" in out                    # 前は「Hello from research-agent!」だけ
    script = Path(__file__).resolve().parents[1] / "scripts" / "agent.py"
    r = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and "--verbose" in r.stdout and "request" in r.stdout
    from importlib.metadata import entry_points
    assert any(e.name == "research-agent" and e.value == "research_agent.cli:main"
               for e in entry_points(group="console_scripts"))
