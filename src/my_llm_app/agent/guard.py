"""LLM が選んだ行動をプログラムで検査する。

横の判断（回数・上限・状態の数え上げ）はプログラムが担当する（設計書 3 章）。
特に「対象が決まらないまま答えてしまう」（例 A のパターン 2）を、ここで止める。
応答の前に何を調べ直すか（再帰。設計書 10 章）も、ここでプログラムが決める。
"""

from __future__ import annotations

from my_llm_app.agent.coverage import MISSING, WEAK
from my_llm_app.agent.state import (EXTRA_PAGES, MAX_ASPECT_SEARCHES, MAX_CANDIDATES, MAX_CONFLICT_SEARCHES,
                                    MAX_PAGE_ATTEMPTS, MAX_PAGES, MAX_QUESTIONS, MAX_SECONDS, MAX_STALE_READS,
                                    MAX_STEPS, MAX_WEB_FAILURES, RESERVE_SECONDS, AgentState, Decision)


def build_question(state: AgentState) -> str:
    """候補から、選択肢付きの質問文をプログラムで作る（設計書 5.2 Q-2）。"""
    if state.get("questions_asked", 0) == 0:
        lines = [f"「{state.get('request', '')}」について、どれのことを指していますか？"]
    else:
        lines = ["回答に当てはまるものが複数あります。どれのことですか？"]
    for i, c in enumerate(state.get("candidates", [])[:MAX_CANDIDATES], 1):
        desc = f"（{c['description']}）" if c.get("description") else ""
        lines.append(f"{i}. {c['label']}{desc}")
    lines.append("番号か、具体的な内容で答えてください。上にない場合はその内容を書いてください。")
    return "\n".join(lines)


def build_confirm_question(state: AgentState) -> str:
    """調査の途中で同名の別の対象が見つかったときの確認（R-5）。"""
    t = state["target"]
    return (f"調べている途中で、同じ名前の別のもの（{state['new_ambiguity']}）が見つかりました。\n"
            f"調べているのは「{t['label']}（{t['description']}）」で合っていますか？\n"
            "1. はい\n2. いいえ（どれのことか書いてください）")


def is_ambiguous(state: AgentState) -> bool:
    return not state.get("target") and len(state.get("candidates", [])) >= 2


def elapsed(state: AgentState) -> float:
    """ここまでに処理にかかった秒数。各ノードの時間の合計なので、利用者の答えを待つ時間は入らない。"""
    return sum(t.get("seconds", 0) for t in state.get("trace", []))


def time_over(state: AgentState) -> bool:
    """報告をまとめる時間を残すと、もう調査を続けられないか。"""
    return elapsed(state) >= MAX_SECONDS - RESERVE_SECONDS


def _name(state: AgentState) -> str:
    return (state.get("target") or {}).get("label") or state.get("request", "")


def research_step(state: AgentState, d: Decision) -> Decision | None:
    """応答の前にやるべき調査を、優先順に 1 つ返す。なければ None（応答してよい）。

    縦と横の再帰（設計書 10 章）をここでプログラムが決める。止める条件（10.3）もここで判定する:
    読むページの上限・新しい事実が出なくなった（収束）・検索の回数の上限・検索の失敗の連続。
    """
    asked = state.get("questions_asked", 0)
    rows = state.get("coverage", [])
    missing = [r for r in rows if r["status"] == MISSING]
    weak = [r for r in rows if r["status"] == WEAK]
    read = [p["url"] for p in state.get("pages_read", [])]
    unread = [u for u in state.get("reading_list", []) if u not in read]
    priority_unread = [u for u in state.get("priority_urls", []) if u not in read]
    verified = state.get("verified", [])
    converged = state.get("stale_reads", 0) >= MAX_STALE_READS
    web_ok = state.get("web_failures_in_row", 0) < MAX_WEB_FAILURES
    # 読むページの枠は、読めたページだけで数える（PDF・接続できないページで枠を使わない）
    ok_read = sum(1 for p in state.get("pages_read", []) if p.get("ok"))
    can_try = len(read) < MAX_PAGE_ATTEMPTS
    can_read = ok_read < MAX_PAGES and can_try
    can_read_extra = ok_read < MAX_PAGES + EXTRA_PAGES and can_try   # 狙って探したページだけに使える枠

    # R-5: 聞き返さずに決めた対象について、途中で同名の別の対象が見つかった → 利用者に確認する
    if state.get("new_ambiguity") and not state.get("ambiguity_asked") and asked < MAX_QUESTIONS:
        return {**d, "action": "ask_user", "question": build_confirm_question(state), "confirm_target": True,
                "guard": "調査の途中で同名の別の対象が見つかったため、対象を確認する"}

    # 1. ページを読む（まだ読んでいない、事実がない、埋まらない・弱い観点がある、狙って探したページがある）
    if not converged and ((priority_unread and can_read_extra) or (
            unread and can_read and (not read or not verified or missing or weak))):
        reason = ("応答の前に、ページ本文を読んで事実を確かめる" if not read or not verified
                  else "狙って探したページを読む" if priority_unread
                  else "網羅表に埋まっていない・弱い観点があるため、さらにページを読む")
        return {**d, "action": "read_pages", "guard": reason}

    # 2. 観点を狙って検索し直す。未発見の観点（R-1）を先に、出典が弱い観点（R-6）を後に。
    #    見つけたページを読む枠が残っていなければ、検索しても無駄なのでしない
    if web_ok and can_read_extra:
        searched = state.get("aspect_searched", [])
        todo = [r["aspect"] for r in missing + weak if r["aspect"] not in searched]
        if todo and len(searched) < MAX_ASPECT_SEARCHES:
            aspect = todo[0]
            words = next((a["words"] for a in state.get("aspects", []) if a["name"] == aspect), aspect)
            query = f"{_name(state)} {words}"
            if query not in state.get("searched", []):
                kind = "見つかっていない" if any(r["aspect"] == aspect for r in missing) else "弱い"
                return {**d, "action": "search", "query": query, "names": [_name(state)], "aspect": aspect,
                        "guard": f"観点「{aspect}」の出典が{kind}ため、その観点で検索し直す"}

    # 3. 食い違いを調べる。前に調べた後で事実が増えていれば調べ直す
    if verified and state.get("conflicts_checked_at", -1) < len(verified):
        return {**d, "action": "check_conflicts", "guard": "応答の前に、出典どうしの食い違いを調べる"}

    # 4. 食い違いがあれば、その事柄を狙って検索し直す（R-6: より信頼度の高い出典を探す）
    if web_ok and can_read_extra:
        done = state.get("conflict_searched", [])
        todo = [c["topic"] for c in state.get("conflicts", []) if c["topic"] not in done]
        if todo and len(done) < MAX_CONFLICT_SEARCHES:
            query = f"{_name(state)} {todo[0]}"
            if query not in state.get("searched", []):
                return {**d, "action": "search", "query": query, "names": [_name(state)],
                        "conflict_topic": todo[0], "guard": f"「{todo[0]}」が出典によって食い違うため、確かめ直す"}

    # 5. 画像
    if state.get("wants_images") and not state.get("images_checked"):
        return {**d, "action": "find_images", "guard": "画像を求められているので、応答の前に画像を探して確かめる"}
    return None


def check_decision(state: AgentState, decision: Decision) -> Decision:
    """LLM の選択を検査し、必要なら変える。変えた場合は guard に理由を入れる。"""
    d: Decision = dict(decision)  # type: ignore[assignment]
    action = d.get("action")
    asked = state.get("questions_asked", 0)

    # 上限: 行動の回数・時間を使い切ったら、わかった範囲で答える（設計書 10.3 S-2, S-5）
    if state.get("steps", 0) >= MAX_STEPS and action != "respond":
        return {**d, "action": "respond", "guard": "行動の上限に達したため、わかった範囲で応答する"}
    if time_over(state) and action != "respond":
        return {**d, "action": "respond", "guard": "時間の上限が近いため、わかった範囲で応答する"}

    target = state.get("target")
    if action == "respond" and target and target.get("id") and not state.get("target_summary"):
        # 対象が決まったのに、その対象自体をまだ調べていない（聞き返しの回答で決まった場合など）
        query = target["label"]
        if query not in state.get("searched", []):
            return {**d, "action": "search", "query": query, "names": [target["label"]],
                    "guard": "対象が決まったので、応答の前にその対象を調べる"}

    if action == "respond" and not is_ambiguous(state) and not time_over(state):
        step = research_step(state, d)
        if step:
            return step

    if action == "respond" and is_ambiguous(state) and asked < MAX_QUESTIONS:
        # 例 A のパターン 2（適当な対象で答える・全部を平たく述べる）を防ぐ
        return {**d, "action": "ask_user", "question": build_question(state),
                "guard": "対象が特定できていないため、応答ではなく聞き返す"}

    if action == "ask_user":
        if asked >= MAX_QUESTIONS:
            return {**d, "action": "respond", "guard": "質問の上限に達したため、前提を明示して応答する"}
        if not state.get("searched"):
            # 調べればわかることは聞かない（設計書 5.2 Q-3）。まず調べる
            return {**d, "action": "search", "query": " ".join(d.get("names") or [state.get("request", "")]),
                    "guard": "まだ何も調べていないため、聞き返す前に調べる"}
        if is_ambiguous(state):
            # 質問文は候補一覧からプログラムが作る（選択肢を必ず付ける）
            d = {**d, "question": build_question(state)}
        if not d.get("question"):
            return {**d, "action": "respond", "guard": "質問する内容がないため応答する"}
        if d["question"] == state.get("last_question"):
            return {**d, "action": "respond", "guard": "同じ質問の繰り返しを防ぐため、前提を明示して応答する"}

    if action == "search" and state.get("web_failures_in_row", 0) >= MAX_WEB_FAILURES:
        # 検索が使えない状態で検索を繰り返しても時間を使うだけ。わかった範囲で答え、使えなかったことを申告する
        return {**d, "action": "respond", "guard": "Web 検索の失敗が続いたため、わかった範囲で応答する"}

    if action == "search":
        if is_ambiguous(state) and asked == 0 and asked < MAX_QUESTIONS:
            # 候補が複数見つかった後にさらに調べても、利用者がどれを指すかはわからない。先に聞き返す
            return {**d, "action": "ask_user", "question": build_question(state),
                    "guard": "候補が複数見つかったので、さらに調べる前に聞き返す"}
        query = (d.get("query") or "").strip() or state.get("request", "")
        if query in state.get("searched", []):
            fallback = "ask_user" if is_ambiguous(state) and asked < MAX_QUESTIONS else "respond"
            extra = {"question": build_question(state)} if fallback == "ask_user" else {}
            return {**d, **extra, "action": fallback, "guard": f"同じ検索（{query}）の繰り返しを防ぐ"}
        d["query"] = query

    return d
