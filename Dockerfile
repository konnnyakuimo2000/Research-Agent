# ゲートウェイ（画面・記録・OpenAI 互換の入口）と、エージェントのサービスで同じイメージを使う。
# 何を動かすかは docker-compose.yml の command で選ぶ（既定はゲートウェイ）
FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# 依存パッケージだけ先に入れる（コードを変えても、ここはキャッシュが効く）
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev

EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "--factory", "my_llm_app.gateway.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
