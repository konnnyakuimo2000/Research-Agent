"""エージェントの状態（State）。"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

Action = Literal["search", "ask_user", "read_pages", "check_conflicts", "find_images", "respond"]

# 上限（設計書 5.2 Q-4、10.3）
MAX_QUESTIONS = 2   # 利用者への質問は最大 2 往復
MAX_STEPS = 16      # 行動の選択は最大 16 回
MAX_CANDIDATES = 8  # 同名の候補は最大 8 件（すべて質問の選択肢に出す）
PAGES_PER_READ = 4  # 1 回の read_pages で読むページ数
MAX_PAGES = 12      # 1 回の調査で読むページの合計（読めたページだけで数える）
MAX_PAGE_ATTEMPTS = 24  # 読めなかったページも含めた、読もうとした回数の上限
EXTRA_PAGES = 4     # 観点や食い違いを狙って探したページは、MAX_PAGES を超えてもこの数まで読む
MAX_WEB_FAILURES = 2  # Web 検索の失敗がこの回数続いたら、それ以上検索しない
MAX_ASPECT_SEARCHES = 3  # 未発見・出典の弱い観点を狙った検索の上限（設計書 10.1 R-1, 10.2 R-6）
MAX_CONFLICT_SEARCHES = 2  # 食い違いを確かめ直す検索の上限（R-6）
MAX_STALE_READS = 2      # 新しい事実が出ない読み込みがこの回数続いたら、それ以上読まない（収束。10.3 S-4）
MAX_REF_DEPTH = 2        # 参照（出典欄のリンク）をたどる深さ（R-2。10.3 S-3）
REFS_PER_PAGE = 3        # 1 ページからたどる参照の数
MAX_SECONDS = 900        # 1 回の調査の時間の上限（15 分。10.3 S-5）。利用者の答えを待つ時間は数えない
RESERVE_SECONDS = 300    # 報告をまとめる時間（読み込み 1 回 + 画像 + 応答）。残りがこれを切ったら調査をやめる
MAX_FREE_SEARCHES = 3    # LLM が自分で選ぶ検索の上限。これを超えたら、プログラムが決める調査（再帰）に移る
MAX_IMAGE_FETCHES = 20   # 画像の候補を取りに行く回数の上限（取れない・小さすぎるものは確かめる回数に数えない）
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
    confirm_target: bool  # ask_user のとき: 途中で見つかった同名の別対象について、対象を確認する質問か（R-5）
    conflict_topic: str   # search のとき: 食い違いを確かめ直す検索なら、その事柄（R-6）
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
    subject_type: str                 # 対象の種類（観点のお手本のチェックリストを選ぶため）
    name_variants: dict[str, int]     # 検索結果の題名にあった対象の名前の別の表記 → 出てきた題名の数（query_plan）
    seen_sites: list[str]             # 対象の名前を含む結果が出た、Tier 1〜2 のドメイン（query_plan）
    query_log: list[dict]             # 検索ごとの成果（query, aspect, site, new = 初めて見つかった対象のページ数）
    aspects_origin: str              # 観点を作ったもの（"LLM" か、使えなかったときの "チェックリスト"）
    aspects: list[dict]               # 調べる観点（観点チェックリストから展開）
    coverage: list[dict]              # 網羅表（観点ごとの出典数・Tier・状態。プログラムが集計）
    aspect_searched: list[str]        # 埋まっていない観点を狙って検索した観点
    conflicts: list[Conflict]         # 出典どうしの食い違い（設計書 7.4 T-3）
    conflicts_checked_at: int         # 食い違いを調べたときの事実の数（増えていれば調べ直す）
    conflict_searched: list[str]      # 確かめ直す検索をした食い違いの事柄（R-6）
    priority_urls: list[str]          # 観点や食い違いを狙った検索で見つけたページ（先に読む）
    stale_reads: int                  # 新しい事実が出なかった読み込みが何回続いているか（収束の判定）
    link_depth: dict[str, int]        # 参照をたどって見つけたページの深さ（R-2）
    new_ambiguity: str                # 調査の途中で見つかった同名の別の対象（R-5）
    ambiguity_asked: bool             # それについて利用者に確認したか
    wants_images: bool                # 画像を探すか（求められていなくても探す。断られたときだけ False）
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
