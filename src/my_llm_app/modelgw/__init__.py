"""モデルの窓口（model-gateway。三層の第 3 層。docs/telemetry_enhancement_plan.md 6 章）。

    uv run uvicorn --factory my_llm_app.modelgw.app:create_app --port 8103

- registry: モデルの一覧（config/models.toml）
- app: OpenAI 互換の API。どのモデルに送るか、やり直し、モデルごとの同時実行の上限、使用量
- status: エージェントが「モデルにつながるか」を画面に出すための小さな部品
"""
