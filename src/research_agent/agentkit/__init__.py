"""エージェントの契約（Agent Protocol v1）の共通の部品（docs/telemetry_enhancement_plan.md 4 章）。

- protocol: イベントの形（段と拡張）、SSE の読み書き
- runs: 依頼の実行・一時停止・答え・止める（どのエージェントでも同じ動き）
- service: 契約の API（/agent/v1/*）を出す FastAPI アプリ
"""
