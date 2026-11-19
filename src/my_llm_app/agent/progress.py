"""途中経過の文（Open WebUI の画面に流す）。

LLM には書かせず、各ノードの出力からプログラムが作る（速く、内容が実際の動きと必ず一致する）。
"""

from __future__ import annotations

from my_llm_app.agent.coverage import CONFIRMED

_ACTIONS = {
    "search": "検索する",
    "read_pages": "ページ本文を読む",
    "check_conflicts": "出典どうしの食い違いを調べる",
    "find_images": "画像を探して確かめる",
    "ask_user": "利用者に確認する",
    "respond": "報告をまとめる",
}

_VERDICTS = {"yes": "調べられます", "partial": "一部は調べられません", "no": "調べられません", "unsure": "調べてみます"}


def describe(node: str, update: dict) -> str:
    """ノード 1 回分の途中経過。表示するものがなければ空文字。"""
    if not update:
        return ""
    if node == "check_feasibility":
        f = update["feasibility"]
        return f"- 依頼の確認: {_VERDICTS.get(f['verdict'], f['verdict'])}（{f['reason']}）"
    if node == "decide":
        d = update["decision"]
        s = f"- 次: {_ACTIONS.get(d['action'], d['action'])}"
        if d["action"] == "search" and d.get("query"):
            s += f"「{d['query']}」"
        if d.get("guard"):
            s += f" — {d['guard']}"
        return s
    if node == "search":
        t = update.get("target")
        n = len((update.get("trace") or [{}])[0].get("web", []))
        s = f"  - 検索結果 {n} 件"
        if t:
            s += f"。対象: {t['label']}（{t.get('description', '')}）"
        elif len(update.get("candidates", [])) >= 2:
            s += f"。候補が {len(update['candidates'])} 件あります"
        return s
    if node == "read_pages":
        pages = update["pages_read"][-len(update["trace"][0]["pages"]):]
        ok = [p for p in pages if p["ok"]]
        facts = sum(p["verified"] for p in ok)
        lines = [f"  - {len(pages)} ページ中 {len(ok)} ページを読み、本文と照合できた事実 {facts} 件"]
        if update.get("subject_type"):
            names = "、".join(a["name"] for a in update.get("aspects", []))
            origin = update.get("aspects_origin", "チェックリスト")
            lines.insert(0, f"  - 対象の種類: {update['subject_type']}。調べる観点（{origin}が作成）: {names}")
        if update.get("databases_checked"):
            used = "、".join(update.get("databases_used", [])) or "なし（この種類に使えるものがない）"
            lines.insert(1 if update.get("subject_type") else 0,
                         f"  - 分野別データベース: {used}。見つかった記録 {len(update.get('db_records', {}))} 件")
        followed = update["trace"][0].get("followed", [])
        if followed:
            lines.append(f"  - 参考文献のリンクを {len(followed)} 件たどる")
        read = update["trace"][0]["pages"]
        copies = [p for p in read if p.get("reprint_of")]
        if copies:
            lines.append(f"  - ほかのページとほぼ同じ文章のページ {len(copies)} 件（独立した出典として数えない）")
        moved = [p for p in read if p.get("tier_reason")]
        if moved:
            lines.append("  - 信頼度を調整したページ: " + "、".join(
                f"Tier {p['base_tier']}→{p['tier']}（{p['tier_reason']}）" for p in moved))
        rows = update.get("coverage", [])
        if rows:
            done = sum(1 for r in rows if r["status"] == CONFIRMED)
            lines.append(f"  - 網羅表: {done}/{len(rows)} 観点を確認済み（"
                         + "、".join(f"{r['aspect']}: {r['status']}" for r in rows) + "）")
        return "\n".join(lines)
    if node == "check_conflicts":
        cs = update["conflicts"]
        return f"  - 食い違い {len(cs)} 件" + ("".join(f"\n    - {c['topic']}" for c in cs))
    if node == "find_images":
        imgs = update.get("images", [])
        ref = sum(1 for i in imgs if i.get("level") == "参考画像")
        s = f"  - 確かめた候補 {len(update.get('image_log', []))} 件、採用 {len(imgs) - ref} 件"
        return s + (f"（ほかに参考画像 {ref} 件）" if ref else "")
    return ""
