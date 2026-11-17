"""エージェントの状態（State）。"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

Action = Literal["search", "ask_user", "read_pages", "check_conflicts", "find_images", "respond"]

# 上限（設計書 5.2 Q-4、10.3）
MAX_QUESTIONS = 2   # 利用者への質問は最大 2 往復
MAX_STEPS = 12      # 行動の選択は最大 12 回
MAX_CANDIDATES = 8  # 同名の候補は最大 8 件（すべて質問の選択肢に出す）
PAGES_PER_READ = 4  # 1 回の read_pages で読むページ数
MAX_PAGES = 12      # 1 回の調査で読むページの合計
MAX_WEB_FAILURES = 2  # Web 検索の失敗がこの回数続いたら、それ以上検索しない
MAX_ASPECT_SEARCHES = 2  # 埋まっていない観点を狙った検索の上限（設計書 10.1 R-1）
MAX_IMAGE_CHECKS = 6     # 1 回の調査で確かめる画像候補の上限（1 枚ごとに LLM を呼ぶため）
WANTED_IMAGES = 2        # 確認済みの画像がこの枚数そろったら、それ以上確かめない
# 候補の 1 位の sitelinks（記事がある言語版などの数）が 2 位のこの倍数以上なら、
# 一般にはその意味を指すとみなし、聞き返さずに前提を明示して進める（例: 富士山）
DOMINANCE_RATIO = 4


class Candidate(TypedDict):
    id: str            # Wikidata の ID（Web 検索だけで見つけた候補は空文字）
    label: str
    description: str
    sitelinks: int     # どれだけ一般的に知られているかの目安


class Source(TypedDict):
    title: str
    url: str
    tier: int          # 情報源の Tier（設計書 6.3）


class Fact(TypedDict):
    text: str
    source: str        # 根拠の URL（取得した検索結果にない URL は空文字にする）


class Conflict(TypedDict):
    aspect: str        # 観点
    topic: str         # 何について食い違っているか（例:「死者・行方不明者数」）
    facts: list[dict]  # 食い違っている事実（VerifiedFact。別々のサイトのもの）


class VerifiedFact(TypedDict):
    text: str          # 事実（短い文）
    aspect: str        # 観点（観点チェックリストの名前。どれにも当たらなければ「その他」）
    quote: str         # 根拠となる本文の一節（本文に実在することをプログラムで確認済み）
    source: str        # ページの URL


class PageRead(TypedDict):
    url: str
    ok: bool
    reason: str        # 読めなかった理由
    extracted: int     # LLM が抜き出した事実の数
    verified: int      # そのうち引用を照合できた数


class Decision(TypedDict, total=False):
    action: Action
    reason: str
    query: str         # search のとき: Web 検索の言葉
    names: list[str]   # search のとき: Wikidata で引く名前（原語・別名を含む）
    question: str      # ask_user のとき
    aspect: str        # search のとき: 埋まっていない観点を狙った検索なら、その観点
    guard: str         # プログラムが LLM の選択を変えた場合の理由


class Feasibility(TypedDict):
    verdict: Literal["yes", "partial", "no", "unsure"]
    reason: str                       # 判定の理由
    infeasible_parts: list[str]       # 公開情報では調べられない部分
    suggestions: list[str]            # 代わりにできること


class AgentState(TypedDict, total=False):
    request: str                      # 利用者の依頼（原文）
    feasibility: Feasibility          # 公開情報で調べられる依頼かの判定（設計書 4 章 ④）
    clarifications: list[str]         # 質問に対する利用者の回答
    known: list[Fact]                 # わかったこと（検索結果の要約から。次の行動を決める手がかりにだけ使う）
    reading_list: list[str]           # 深掘りするページの URL（Tier の高い順。Tier 5 は入れない）
    tier1_searched: bool              # Tier 1（公的機関・大学）を狙った検索をしたか
    pages_read: list[PageRead]        # 読んだ（読もうとした）ページ
    verified: list[VerifiedFact]      # ページ本文から抜き出し、引用を照合できた事実（回答はこれだけで書く）
    subject_type: str                 # 対象の種類（観点チェックリストを選ぶため）
    aspects: list[dict]               # 調べる観点（観点チェックリストから展開）
    coverage: list[dict]              # 網羅表（観点ごとの出典数・Tier・状態。プログラムが集計）
    aspect_searched: list[str]        # 埋まっていない観点を狙って検索した観点
    conflicts: list[Conflict]         # 出典どうしの食い違い（設計書 7.4 T-3）
    conflicts_checked: bool           # 食い違いを調べたか（応答の前に 1 回だけ）
    wants_images: bool                # 依頼が画像を求めているか
    page_images: list[dict]           # 読んだページに載っていた画像（L4 の候補）
    images_checked: bool              # 画像を探したか（応答の前に 1 回だけ）
    images: list[dict]                # 採用した画像（確認済み・おそらく正しい）
    image_log: list[dict]             # 確かめた候補と判定（不採用の理由も残す。申告に使う）
    image_counts: dict[str, int]      # 段階ごとの候補数（申告に使う）
    unknown: list[str]                # わからないこと
    candidates: list[Candidate]       # 同名の候補
    target: Candidate | None          # 特定できた対象
    assumptions: list[str]            # 聞き返さずに置いた前提（報告の冒頭に書く）
    last_question: str                # 直前の質問（同じ質問の繰り返し防止）
    target_summary: str               # 対象の概要（Wikipedia の冒頭）
    summary_url: str                  # 概要の出典 URL
    pages: dict[str, str]             # 取得した検索結果の URL → タイトル（出典一覧を作るため）
    searched: list[str]               # 実行した検索の言葉（同じ検索の繰り返し防止）
    unresponsive: list[str]           # 応答しなかった検索エンジン
    failures: list[str]               # 使えなかった道具（Web 検索・Wikidata など）と理由。報告で申告する
    web_failures_in_row: int          # Web 検索の失敗が何回続いているか
    questions_asked: int
    steps: int
    decision: Decision                # 直前に選んだ行動
    trace: Annotated[list[dict], operator.add]   # 行動の記録（実行記録用）
    answer: str
