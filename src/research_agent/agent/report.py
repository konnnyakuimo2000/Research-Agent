"""報告の組み立て（再レビュー: 報告生成の分離）。

ノード（agent/nodes.py の respond）は LLM に本文を書かせ、ここで残りの節を付けて報告にする:
本文の整え方（観点の見出し・プログラムが付ける節の重複の除去・材料にない名前の検査）、食い違い、確かめきれなかったこと、
画像、出典。出典の番号は SourceTable で決め、報告の本文の番号・出典一覧・テレメトリーの出典・成果物の source_id は
すべてこの対応表から作る（本文の [3] = 成果物の s3）。
"""

from __future__ import annotations

import re

from research_agent.agent import coverage as cov
from research_agent.agent import imaging
from research_agent.agent import reprints
from research_agent.agent import telemetry
from research_agent.agent.quotes import normalize
from research_agent.agent.state import AgentState, Source
from research_agent.tiers import UrlNumbers, canonical, page_tier


class SourceTable:
    """出典の対応表: 本文の番号 [n]、成果物の id（s{n}）、URL。表記が違うだけの同じ資料は同じ番号。"""

    def __init__(self, sources: list[Source]) -> None:
        self.sources = sources
        self.number = UrlNumbers([s["url"] for s in sources])

    def id_of(self, url: str) -> str:
        return f"s{self.number[url]}" if url and url in self.number else ""

    def entries(self) -> list[dict]:
        """番号の順の出典（id を付けて）。"""
        return [{**s, "id": f"s{i}"} for i, s in enumerate(self.sources, 1)]


def assemble(state: AgentState, body: str, table: SourceTable) -> str:
    """LLM が書いた本文に、プログラムが書く節を付けて報告にする。"""
    parts = [aspect_headings(body, [a["name"] for a in state.get("aspects", [])]).strip()]
    if infeasible := (state.get("feasibility") or {}).get("infeasible_parts"):
        parts.append("\n### 調べられなかったこと（公開情報では調べられないため）\n"
                     + "\n".join(f"- {x}" for x in infeasible))
    # 食い違いと確かめきれなかった観点は、プログラムが書く（LLM に書かせない）
    if state.get("conflicts"):
        number = table.number
        lines = [f"- {c['aspect']}・{c['topic']}: "
                 + " / ".join(f"「{f['text']}」[{number[f['source']]}]" for f in c["facts"] if f["source"] in number)
                 for c in state["conflicts"]]
        parts.append("\n### 出典どうしの食い違い\n" + "\n".join(lines))
    # 確かめきれなかった観点（出典がない・出典が弱い）は、1 つの節にまとめて、読む人の言葉で書く
    if gaps := "\n".join(x for x in (cov.gaps_text(state.get("coverage", [])),
                                     cov.weak_text(state.get("coverage", []))) if x):
        parts.append("\n### 確かめきれなかったこと\n" + gaps)
    if state.get("images_checked"):
        parts.append("\n" + images_section(state))
    # 調査の道具の細かい状態（応答しなかった検索エンジン・調べたデータベース・失敗した処理の名前）は書かない。
    # それらはテレメトリーの画面と実行の記録に残る。検索そのものが使えなかったときだけ、読む人に知らせる
    if any(f.startswith(("Web 検索", "Wikidata の検索")) for f in state.get("failures", [])):
        parts.append("\n※ 検索の一部が使えなかったため、調べきれていない可能性があります。")
    if table.sources:
        parts.append("\n### 出典\n" + sources_text(state, table.sources))
    return "\n".join(parts)


# プログラムが付ける節の見出し。LLM が本文に同じ節を書いたら取り除く（同じ内容が二重に出ないように）
_PROGRAM_SECTIONS = ("食い違い", "確認できなかった", "調べられなかった", "わからなかった", "出典", "参考", "画像")


def _heading_text(line: str) -> str | None:
    """見出しの行（「### 〜」または行全体が太字の「**〜**」）なら、その文字を返す。"""
    s = line.strip()
    if s.startswith("#"):
        return s.lstrip("#").strip()
    if len(s) > 4 and s.startswith("**") and s.endswith("**") and s.count("**") == 2:
        return s[2:-2].strip()
    return None


def aspect_headings(body: str, aspects: list[str]) -> str:
    """観点の名前だけの行（「正式名・型番」「**経歴**」「経歴：」）を、小見出し（### 経歴）に直す。
    LLM が見出しの記号を省くことがあり、そのままでは画面でも Open WebUI でも見出しにならなかった。"""
    names = {a.strip() for a in aspects if a.strip()}
    out = []
    for line in body.splitlines():
        core = line.strip().strip("*#").strip().rstrip(":：").strip()
        out.append(f"### {core}" if core in names and not line.lstrip().startswith("### ") else line)
    return "\n".join(out)


def strip_program_sections(body: str) -> str:
    """LLM の本文から、プログラムが付ける節（食い違い・確認できなかったこと・出典・画像）を取り除く。"""
    out, skipping = [], False
    for line in body.splitlines():
        heading = _heading_text(line)
        if heading is not None:
            skipping = any(w in heading for w in _PROGRAM_SECTIONS)
        if not skipping:
            out.append(line)
    return "\n".join(out)


# 名前らしい表記: 大文字で始まる英字の語、「・」でつないだカタカナ（人名など）
_LATIN_NAME = re.compile(r"[A-Z][A-Za-zÀ-ÿ]{2,}")
_KATAKANA_NAME = re.compile(r"[ァ-ヴー]+(?:・[ァ-ヴー]+)+")


def unsupported_names(body: str, materials: str) -> list[str]:
    # 本文にある名前らしい表記のうち、材料（LLM に渡したもの）にないもの。読みから作った英語名などを見つける
    m = normalize(materials).lower()
    found = set(_LATIN_NAME.findall(body)) | set(_KATAKANA_NAME.findall(body))
    return sorted(t for t in found if normalize(t).lower() not in m)


def drop_sentences_with(body: str, words: list[str]) -> str:
    # 指定の表記を含む文を除く（見出しは残す）
    pieces = re.split(r"(?<=[。\n])", body)
    return "".join(p for p in pieces if p.lstrip().startswith("#") or not any(w in p for w in words))


# 依頼文によく出る決まり文句（対象を絞り込む根拠にならない）


def _image_lines(img: dict, name: str) -> list[str]:
    """報告の画像 1 枚: 画像と、1 行の説明（写っているもの・出典へのリンク）。

    確信度・Tier・ライセンスの項目を毎回並べると、調査の記録のようになって読みにくい（細かい判定は実行の記録に残る）。
    読む人に知らせる必要があるときだけ、注意を添える: 対象かどうか確かめきれていない、対象そのものではない、
    利用に許可が要る。
    """
    desc = img.get("description", "") or name
    source = f"[{img.get('title') or telemetry.host(img['page_url'])}]({img['page_url']})"
    notes = []
    if img["level"] == imaging.REFERENCE:
        related = img.get("related", "")
        notes.append(f"{name}そのものではなく、関係のある「{related}」の画像です" if related
                     else f"{name}そのものの画像ではありません")
    elif img["level"] != imaging.CONFIRMED:
        notes.append(f"{name}の画像かどうかは確かめきれていません")
    if imaging.needs_permission(img.get("license") or ""):
        notes.append("利用には権利者の許可が必要です（確認のために表示しています）")
    caption = f"{desc}（出典: {source}）" + "".join(f" ※ {n}。" for n in notes)
    return [f"![{desc}]({img['src']})", caption, ""]


def images_section(state: AgentState) -> str:
    """報告の「画像」（設計書 9.1, 9.2）。採用した画像と、見つからなかった場合の申告をプログラムが書く。

    参考画像（L5）は対象そのものではないので、対象の画像が見つからなかったことを先に書いてから出す。
    """
    name = (state.get("target") or {}).get("label", "対象")
    found = [i for i in state.get("images", []) if i["level"] != imaging.REFERENCE]
    references = [i for i in state.get("images", []) if i["level"] == imaging.REFERENCE]
    lines = ["### 画像"]
    for img in found:
        lines += _image_lines(img, name)
    if not found:
        lines.append(f"{name}の画像として確かめられるものは見つかりませんでした。")
        if imaging.asks_for_images(state.get("request", "")):
            # 画像を求められていたときは、次にできることを添える（試した候補の内訳は実行の記録に残る）
            lines.append("公式サイトや所蔵機関の資料をあたるか、お持ちの画像があれば、それが対象かを確かめられます。")
    if references:
        lines += ["", "参考として、関係のあるものの画像を載せます。", ""]
        for img in references:
            lines += _image_lines(img, name)
    return "\n".join(lines).rstrip()


def cited_db_records(state: AgentState, table: SourceTable) -> list[dict]:
    """報告に引用した分野別データベースの資料（出典から逆引き。表記が違うだけの同じ資料は 1 つ）。"""
    dbs = state.get("db_records", {})
    cited = dict.fromkeys(canonical(e["url"]) for e in table.entries() if canonical(e["url"]) in dbs)
    return [{"url": dbs[k]["url"], "database": dbs[k]["database"], "source_id": dbs[k].get("source_id", ""),
             "kind": dbs[k].get("kind", "")} for k in cited]


def collect_sources(state: AgentState) -> list[Source]:
    """回答で使う出典を、概要 → わかったことの順に重複なく並べる（出典番号はこの順）。"""
    pages = state.get("pages", {})
    urls = [state["summary_url"]] if state.get("target_summary") and state.get("summary_url") else []
    urls += [k["source"] for k in state.get("verified", [])]
    # 表記が違うだけの同じページ（追跡用パラメータの違いなど）は 1 つの出典にする
    by_key: dict[str, str] = {}
    for u in urls:
        by_key.setdefault(canonical(u), u)
    ordered = list(by_key.values())
    # Tier はページ単位の調整（公式サイト・LLM の判定）を含めたもの
    return [{"title": pages.get(u, u), "url": u, "tier": page_tier(u, state)} for u in ordered]


def sources_text(state: AgentState, sources: list[Source]) -> str:
    """報告の出典一覧（題名と URL）。転載のページには、元のページの番号を添える。

    出典ごとの Tier（信頼度の段階）は書かない。信頼度の低い出典の事実は本文で断定しない書き方にしてあり、
    段階の内訳はテレメトリーの画面と実行の記録に残る。
    """
    number = UrlNumbers([s["url"] for s in sources])
    copies = state.get("reprints", {})
    dbs = state.get("db_records", {})
    lines = []
    for i, s in enumerate(sources, 1):
        line = f"[{i}] {s['title']} — {s['url']}"
        rec = dbs.get(canonical(s["url"]))
        if rec and rec.get("kind"):
            # 構造化データから作った資料には、種類と資料 ID を添える（所蔵元のページがあれば、それも）
            line += f"（{rec['kind']}" + (f" · 資料 ID {rec['source_id']}" if rec.get("source_id") else "") + "）"
            if rec.get("holder_url"):
                line += f" 所蔵元: {rec['holder_url']}"
        original = reprints.original_of(s["url"], copies)
        if canonical(original) != canonical(s["url"]) and original in number and number[original] != i:
            line += f"（[{number[original]}] とほぼ同じ文章）"
        lines.append(line)
    return "\n".join(lines)


def infeasible_report(request: str, f: dict) -> str:
    parts = f.get("infeasible_parts") or [request]
    lines = ["この依頼は、公開されている情報からは調べられないと判断しました。", "",
             "### 調べられないこと"]
    lines += [f"- {p}" for p in parts]
    lines += ["", "### 理由", f.get("reason", "")]
    lines += ["", "### 試したこと",
              "公開情報（Web・百科事典など）の検索は行っていません。公開されていない情報は、"
              "検索しても見つからないか、関係のない情報を誤って示してしまうおそれがあるためです。"]
    if f.get("suggestions"):
        lines += ["", "### 代わりにできること"] + [f"- {s}" for s in f["suggestions"]]
    lines += ["", "公開されている情報で調べられる形（例: 対象の名前や、公開されている資料）があれば、教えてください。"]
    return "\n".join(lines)
