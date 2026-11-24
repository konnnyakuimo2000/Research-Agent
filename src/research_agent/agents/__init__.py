"""契約（Agent Protocol v1）を話すエージェントのサービス。1 つのモジュールが 1 つのサービス。

    uv run uvicorn --factory research_agent.agents.research:create_app --port 8101   # 調査エージェント（0・1 段 + research/v1）
    uv run uvicorn --factory research_agent.agents.chat:create_app --port 8102       # チャット（0 段だけ）
"""
