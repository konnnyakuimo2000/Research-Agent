"""ゲートウェイ（WebUI 側）。エージェントの中身は知らず、契約（Agent Protocol v1）だけで話す。

    uv run uvicorn --factory research_agent.gateway.app:create_app --port 8100

- registry: どのエージェントにつなぐか（config/agents.toml）と、契約の API を呼ぶ部品
- store: 依頼の記録（SQLite の一覧 + イベントの jsonl）
- app: 画面（/ui/）と画面の API（/api/*）。エージェントのイベントを受け取って保存し、画面へ配る
- openai_compat: OpenAI 互換の入口（Open WebUI 用）
"""
