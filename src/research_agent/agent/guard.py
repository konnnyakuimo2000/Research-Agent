"""LLM が選んだ行動をプログラムで検査する。

横の判断（回数・上限・状態の数え上げ）はプログラムが担当する（設計書 3 章）。
特に「対象が決まらないまま答えてしまう」（例 A のパターン 2）を、ここで止める。
応答の前に何を調べ直すか（再帰。設計書 10 章）も、ここでプログラムが決める。
"""

from __future__ import annotations

import re

from research_agent.agent import imaging, query_plan
from research_agent.agent.coverage import MISSING, WEAK
from research_agent.agent.names import fix_spelling, mentions
from research_agent.tiers import by_tier, canonical
from research_agent.agent.state import (EXTRA_PAGES, MAX_ASPECT_SEARCHES, MAX_CANDIDATES, MAX_CONFLICT_SEARCHES,
                                    MAX_FREE_SEARCHES, MAX_PAGE_ATTEMPTS, MAX_QUESTIONS, MAX_STALE_READS, MAX_STEPS,
                                    MAX_WEB_FAILURES, RESERVE_SECONDS, AgentState, Decision, setting)

# 画像を探すための検索の言葉（画像はプログラムが find_images で探すので、LLM の検索ではしない）
_IMAGE_QUERY = re.compile(r"画像|写真|photo|image|picture|jpe?g|png|commons|file:", re.IGNORECASE)


OPEN_QUESTION_TAIL = "名前（フルネーム）や所属・分野など、手がかりを教えてください。"
CHOICE_TAIL = "番号か、具体的な内容で答えてください。上にない場合はその内容を書いてください。"


def build_question(state: AgentState) -> str:
    """候補から、選択肢付きの質問文をプログラムで作る（設計書 5.2 Q-2）。

    候補がどれも依頼文の手がかりに合わない（「九大の松田教授」に、九州大学や同名の町しか出ない）ときは、
    答えのない選択肢を並べず、手がかりを自由に答えてもらう。
    """
    if not candidates_fit(state):
        return f"「{state.get('request', '')}」の対象を、検索結果から特定できませんでした。\n{OPEN_QUESTION_TAIL}"
    if state.get("questions_asked", 0) == 0:
        lines = [f"「{state.get('request', '')}」について、どれのことを指していますか？"]
    else:
        lines = ["回答に当てはまるものが複数あります。どれのことですか？"]
    for i, c in enumerate(state.get("candidates", [])[:MAX_CANDIDATES], 1):
        desc = f"（{c['description']}）" if c.get("description") else ""
        lines.append(f"{i}. {c['label']}{desc}")
    lines.append(CHOICE_TAIL)
    return "\n".join(lines)


def has_choices(question: str) -> bool:
    """プログラムが作った、番号で答えられる質問か（画面は、このときだけ選択肢のボタンを出す）。
    LLM が書いた質問の番号付きの項目（「1. 松田教授のお名前」）は選択肢ではない。"""
    return question.endswith(CHOICE_TAIL) or question.endswith("2. いいえ（どれのことか書いてください）")


def build_confirm_question(state: AgentState) -> str:
    """調査の途中で同名の別の対象が見つかったときの確認（R-5）。"""
    t = state["target"]
    return (f"調べている途中で、同じ名前の別のもの（{state['new_ambiguity']}）が見つかりました。\n"
            f"調べているのは「{t['label']}（{t['description']}）」で合っていますか？\n"
            "1. はい\n2. いいえ（どれのことか書いてください）")


def is_ambiguous(state: AgentState) -> bool:
    return not state.get("target") and len(state.get("candidates", [])) >= 2


def elapsed(state: AgentState) -> float:
    """ここまでに処理にかかった秒数。各ノードの時間の合計なので、利用者の答えを待つ時間は入らない。

    LLM サーバーに接続できずに待った時間（llm_wait）も除く。サーバーが止まっている間に上限を使い切り、
    何も読まずに報告してしまうのを防ぐ。
    """
    return sum(t.get("seconds", 0) - t.get("llm_wait", 0) for t in state.get("trace", []))


def time_over(state: AgentState) -> bool:
    """報告をまとめる時間を残すと、もう調査を続けられないか（時間の上限は依頼の設定。既定は MAX_SECONDS）。"""
    return elapsed(state) >= setting(state, "max_seconds") - RESERVE_SECONDS


def _name(state: AgentState) -> str:
    return (state.get("target") or {}).get("label") or state.get("request", "")


def free_searches(state: AgentState) -> int:
    """LLM が自分で選んだ検索の回数（観点・食い違い・言い換えでプログラムが決めた検索は除く）。"""
    return (len(state.get("searched", [])) - len(state.get("aspect_searched", []))
            - len(state.get("conflict_searched", [])) - len(state.get("rephrase_searched", []))
            - len(state.get("retry_searched", [])))


def candidates_fit(state: AgentState) -> bool:
    """同名の候補のどれかが、依頼文の手がかりに合うか。

    依頼文に名前以外の手がかり（「青山学院大学の情報テクノロジー学科の」など）があるのに、どの候補の説明にも
    それが出てこない → 名前だけで引いた的外れな候補（米国の市の「マーティン」など）。手がかりがない依頼
    （「マーキュリーについて」）は、どれのことか聞くしかないので合うとみなす。
    """
    parts = query_plan.request_parts(state.get("request", ""))
    if len(parts) < 2:
        return True
    for c in state.get("candidates", []):
        label = c.get("label", "")
        others = [p for p in parts if not mentions(label, [p]) and not mentions(p, [label])]
        if not others or any(mentions(c.get("description", ""), [p]) for p in others):
            return True
    return False


def rephrase_step(state: AgentState, d: Decision) -> Decision | None:
    """聞き返す前に、1 回だけ依頼文の言葉で検索し直す（問題 1・2）。

    検索結果は、そのとき応答した検索エンジンと検索の言葉で大きく揺れる（「…マーティン 先生」で関係ない結果ばかりの
    ことがある）。対象が決まらず、候補が依頼文の手がかりに合わないまま聞き返すと、正しい答えのない選択肢を出してしまう。
    """
    if (state.get("target") or state.get("clarifications") or not state.get("searched")
            or state.get("rephrase_searched") or not is_ambiguous(state) or candidates_fit(state)):
        return None
    query = query_plan.request_query(state.get("request", ""))
    if not query or query in state.get("searched", []):
        return None
    return {**d, "action": "search", "query": query, "names": [], "rephrase": True,
            "guard": "候補が依頼文の手がかりに合わないため、聞き返す前に依頼文の言葉で検索し直す"}


MAX_RETRY_SEARCHES = 1


def retry_query(state: AgentState) -> str:
    """対象の名前を含む結果がなかったときに打ち直す検索: 名前の別の表記をそのまま（引用符で 1 語として）。
    英語などの表記（「Agent Memory」）を先に使う。題名の日本語は「AI エージェントのメモリ」のように言葉を挟むが、
    英語の記事の題名には名前がそのまま出ることが多い。すでに打った検索は打たない。"""
    names = state.get("no_relevant_names") or []
    ordered = [n for n in names if re.search(r"[A-Za-z]", n)] + [n for n in names if not re.search(r"[A-Za-z]", n)]
    for n in ordered:
        q = f'"{n.strip()}"'
        if n.strip() and q not in state.get("searched", []):
            return q
    return ""


def retry_step(state: AgentState, d: Decision, unread: list[str]) -> Decision | None:
    """検索の結果に対象の名前を含むページが 1 件もなく、まだ何も読んでいない → 別の表記で 1 回だけ探し直す。
    前は、関係のあるページを見つけられないまま「情報が不足しています」と答えていた（2026-11-28「エージェントメモリ」）。"""
    if (not state.get("no_relevant_names") or state.get("pages_read") or unread
            or len(state.get("retry_searched", [])) >= MAX_RETRY_SEARCHES
            or state.get("web_failures_in_row", 0) >= MAX_WEB_FAILURES):
        return None
    query = retry_query(state)
    if not query:
        return None
    return {**d, "action": "search", "query": query, "names": state["no_relevant_names"], "retry": True,
            "guard": "検索の結果に対象の名前を含むページがなかったため、名前の別の表記で検索し直す"}


def read_keys(state: AgentState) -> set[str]:
    """読んだページ。要求した URL と転送先の URL の両方を、表記をそろえた形で持つ。"""
    return {canonical(u) for p in state.get("pages_read", []) for u in (p["url"], p.get("final_url") or p["url"])}


def page_slots(state: AgentState) -> tuple[int, int, int]:
    """読むページの残りの枠: (通常の枠, 狙って探したページだけが使える追加の枠, 失敗を含む残りの試行回数)。
    枠は読めたページだけで数える（PDF・接続できないページで枠を使わない）。research_step と同じ数え方。"""
    max_pages = setting(state, "max_pages")
    read = state.get("pages_read", [])
    ok = sum(1 for p in read if p.get("ok"))
    attempts = max(MAX_PAGE_ATTEMPTS, 2 * max_pages) - len(read)
    return max(0, max_pages - ok), max(0, max_pages + EXTRA_PAGES - ok), max(0, attempts)


def pages_to_read(state: AgentState, limit: int | None = None) -> list[str]:
    """次に読むページ（狙って探したページが先）。read_pages と、読むかどうかの判断（research_step）で同じものを使う。

    前は判断の側が URL をそのまま比べていたので、転送先として読み終えたページ（研究者データベースの
    kyoinId=… → resid/… など）を「まだ読んでいない」と見て、何も読めない read_pages を上限まで繰り返した。
    選ぶ時点で残りの枠に収める。前は毎回 limit（4）件まで選んだので、max_pages=2 でも 4 件読んでいた（再レビュー）。
    追加の枠を使えるのは、狙って探したページだけ。
    """
    done = read_keys(state)
    normal, extra, attempts = page_slots(state)
    out: list[str] = []
    seen: set[str] = set()
    for priority, urls in ((True, by_tier(state.get("priority_urls", []))), (False, state.get("reading_list", []))):
        cap = min(extra if priority else normal, attempts)
        for u in urls:
            key = canonical(u)
            if key not in done and key not in seen and len(out) < cap:
                seen.add(key)
                out.append(u)
    return out[:limit] if limit else out


def wants_more_images(state: AgentState) -> bool:
    """画像を探すか。確かめた事実がまだ 1 件もなければ探さない（依頼がはっきり画像を求めているときを除く）。
    事実がないまま画像だけを探して載せ、「情報が不足しています」と画像だけの報告になっていた（2026-11-28）。"""
    return (bool(state.get("wants_images")) and not state.get("images_checked")
            and (bool(state.get("verified")) or imaging.asks_for_images(state.get("request", ""))))


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
    done = read_keys(state)                    # read_pages と同じ数え方（転送先・表記ゆれも読んだとみなす）
    unread = [u for u in state.get("reading_list", []) if canonical(u) not in done]
    priority_unread = [u for u in state.get("priority_urls", []) if canonical(u) not in done]
    verified = state.get("verified", [])
    converged = state.get("stale_reads", 0) >= MAX_STALE_READS
    web_ok = state.get("web_failures_in_row", 0) < MAX_WEB_FAILURES
    # 読むページの枠は、読めたページだけで数える（PDF・接続できないページで枠を使わない）
    ok_read = sum(1 for p in state.get("pages_read", []) if p.get("ok"))
    max_pages = setting(state, "max_pages")    # 依頼の設定（既定は MAX_PAGES）
    can_try = len(read) < max(MAX_PAGE_ATTEMPTS, 2 * max_pages)
    can_read = ok_read < max_pages and can_try
    can_read_extra = ok_read < max_pages + EXTRA_PAGES and can_try   # 狙って探したページだけに使える枠

    # R-5: 聞き返さずに決めた対象について、途中で同名の別の対象が見つかった → 利用者に確認する
    if state.get("new_ambiguity") and not state.get("ambiguity_asked") and asked < MAX_QUESTIONS:
        return {**d, "action": "ask_user", "question": build_confirm_question(state), "confirm_target": True,
                "guard": "調査の途中で同名の別の対象が見つかったため、対象を確認する"}

    # 0. 対象の名前を含むページが 1 件も見つかっていない → 別の表記で探し直す（読むものがなければ、何も確かめられない）
    if step := retry_step(state, d, unread):
        return step

    # 1. まずページを読んで事実を確かめる（まだ読んでいない、事実がない）
    if not converged and unread and can_read and (not read or not verified):
        return {**d, "action": "read_pages", "guard": "応答の前に、ページ本文を読んで事実を確かめる"}

    # 2. 画像。求められていれば、調べ直し（時間がかかる）より先に探す。画像を出さずに時間切れにならないように
    if wants_more_images(state):
        return {**d, "action": "find_images", "guard": "応答の前に、対象の画像を探して確かめる"}

    # 3. さらにページを読む（埋まらない・弱い観点がある、狙って探したページがある）
    #    狙って探したページは、収束していても読む（無関係なページや JavaScript のページで空振りが続いた後に、
    #    狙った検索で見つけたページを読まないまま検索だけを繰り返さないように）
    if (priority_unread and can_read_extra) or (not converged and unread and can_read and (missing or weak)):
        reason = ("狙って探したページを読む" if priority_unread
                  else "網羅表に埋まっていない・弱い観点があるため、さらにページを読む")
        return {**d, "action": "read_pages", "guard": reason}

    # 4. 埋まっていない・弱い観点（R-1, R-6）のために検索し直す。どの検索を打つかは query_plan が点数で選ぶ
    #    （検索結果にある名前の表記 × 観点、事実が取れたドメインへの site: 検索）。点数が低ければ打たない。
    #    見つけたページを読む枠が残っていなければ、検索しても無駄なのでしない
    if web_ok and can_read_extra and len(state.get("aspect_searched", [])) < MAX_ASPECT_SEARCHES:
        plan = query_plan.next_search(state)
        if plan:
            aspect = plan["aspect"]
            if plan["site"]:
                why = f"事実が取れた・信頼度の高いサイト（{plan['site']}）の別のページを探す"
            else:
                kind = "見つかっていない" if any(r["aspect"] == aspect for r in missing) else "弱い"
                why = f"観点「{aspect}」の出典が{kind}ため、その観点で検索し直す"
            return {**d, "action": "search", "query": plan["query"], "names": plan["names"], "aspect": aspect,
                    "site": plan["site"], "plan": {"score": plan["score"], "parts": plan["parts"],
                                                   "alternatives": plan["alternatives"]},
                    "guard": f"{why}（点数 {plan['score']}）"}

    # 5. 食い違いを調べる。前に調べた後で事実が増えていれば調べ直す
    if verified and state.get("conflicts_checked_at", -1) < len(verified):
        return {**d, "action": "check_conflicts", "guard": "応答の前に、出典どうしの食い違いを調べる"}

    # 6. 食い違いがあれば、その事柄を狙って検索し直す（R-6: より信頼度の高い出典を探す）
    if web_ok and can_read_extra:
        done = state.get("conflict_searched", [])
        todo = [c["topic"] for c in state.get("conflicts", []) if c["topic"] not in done]
        if todo and len(done) < MAX_CONFLICT_SEARCHES:
            query = f"{_name(state)} {todo[0]}"
            if query not in state.get("searched", []):
                return {**d, "action": "search", "query": query, "names": [_name(state)],
                        "conflict_topic": todo[0], "guard": f"「{todo[0]}」が出典によって食い違うため、確かめ直す"}
    return None


def check_decision(state: AgentState, decision: Decision) -> Decision:
    """LLM の選択を検査し、必要なら変える。変えた場合は guard に理由を入れる。"""
    d: Decision = dict(decision)  # type: ignore[assignment]
    action = d.get("action")
    asked = state.get("questions_asked", 0)

    # 上限: 行動の回数・時間を使い切ったら、わかった範囲で答える（設計書 10.3 S-2, S-5）
    if state.get("steps", 0) >= MAX_STEPS:
        # LLM が応答を選んでいても、ここで確定する（下の research_step が読み込みなどに変えないように）
        return {**d, "action": "respond", "guard": "行動の上限に達したため、わかった範囲で応答する"}
    if time_over(state):
        if (not state.get("pages_read") and pages_to_read(state) and action != "read_pages"
                and elapsed(state) < setting(state, "max_seconds")):          # 上限そのものを過ぎていれば読まない
            # 報告のために残した時間には、読み込み 1 回が入っている。1 ページも読まずに時間を使い切ったら、
            # 読む一覧の先頭を 1 回だけ読んでから応答する（2026-11-25 の「松田教授について」は、本人のページを 8 件
            # 見つけたのに 1 件も読まず、「確認できた事実がありません」とだけ答えていた）
            return {**d, "action": "read_pages",
                    "guard": "時間の上限が近いが、まだ 1 ページも読んでいないため、読む一覧の先頭を読んでから応答する"}
        if wants_more_images(state):
            return {**d, "action": "find_images", "guard": "時間の上限が近いため、画像を探してから応答する"}
        if action != "respond":
            return {**d, "action": "respond", "guard": "時間の上限が近いため、わかった範囲で応答する"}

    if action == "search" and state.get("searched") and not is_ambiguous(state):
        query = d.get("query") or ""
        why = ("画像はプログラムが別に探すため、画像のための検索はしない" if _IMAGE_QUERY.search(query)
               else "自由な検索の上限に達したため、プログラムが決める調査に移る"
               if free_searches(state) >= MAX_FREE_SEARCHES else "")
        if why:
            # 応答に切り替える。応答の前に必要な調査（ページを読む・画像・調べ直し）は下の research_step が決める
            d, action = {**d, "action": "respond", "guard": why}, "respond"

    if action in ("ask_user", "respond", "search") and not d.get("confirm_target"):
        step = rephrase_step(state, d)
        if step:
            return step

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
        if target and not d.get("aspect") and not d.get("site"):
            # 対象の名前が決まった後は、LLM が書き写した名前の綴りの誤り（Dürrst）を、確かめた表記に直す（問題 3）
            names = [target.get("label", "")] + sorted(state.get("name_variants", {}),
                                                       key=lambda v: -state["name_variants"][v])
            query, fixed = fix_spelling(query, [n for n in names if n])
            if fixed:
                d["guard"] = "名前の綴りを直した（" + "、".join(fixed) + "）"
        if query in state.get("searched", []):
            why = f"同じ検索（{query}）の繰り返しを防ぐ"
            if is_ambiguous(state) and asked < MAX_QUESTIONS:
                return {**d, "question": build_question(state), "action": "ask_user", "guard": why}
            # 報告を選んだときと同じ、報告の前の判定に戻す（未読のページ・画像の確認が残っていれば、そちらを先に）。
            # 前はここで即座に報告していたので、LLM が respond を選ぶか同じ語の search を選ぶかで結果が変わった（再レビュー 3）
            if not is_ambiguous(state) and not time_over(state) and (step := research_step(state, d)):
                return {**step, "guard": f"{why}。{step.get('guard', '')}"}
            return {**d, "action": "respond", "guard": why}
        d["query"] = query

    return d
