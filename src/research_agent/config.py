"""接続先などの設定。値は .env（git 管理外）から読み込む。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

# Wikimedia などの API は、誰がアクセスしているかわかる User-Agent を求めている
USER_AGENT = "Research-Agent/0.1 (https://github.com/konnnyakuimo2000/Research-Agent)"


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    searxng_url: str
    llm_timeout: float = 300.0
    llm_gateway: bool = False     # True: llm_base_url はモデルの窓口（model-gateway）


# 窓口はモデルにつながるまで最大 3 回やり直す（10・30・60 秒待つ）。1 回 300 秒まで待つので、その合計より長く待つ
GATEWAY_TIMEOUT = 4 * 300.0 + 10 + 30 + 60 + 60


def load_settings() -> Settings:
    """モデルの窓口（MODEL_GATEWAY_URL）があればそれを使う。なければ LLM サーバー（VL_BASE_URL）を直接呼ぶ。"""
    gateway = os.getenv("MODEL_GATEWAY_URL")
    if gateway:
        return Settings(
            llm_base_url=gateway,
            llm_model=os.getenv("LLM_MODEL", "default"),     # 窓口の既定のモデル（config/models.toml）
            llm_api_key=os.getenv("MODEL_GATEWAY_KEY") or "dummy",
            searxng_url=os.getenv("SEARXNG_URL", "http://localhost:8888"),
            llm_timeout=GATEWAY_TIMEOUT,
            llm_gateway=True,
        )
    base_url = os.getenv("VL_BASE_URL")
    if not base_url:
        raise RuntimeError(".env に VL_BASE_URL（または MODEL_GATEWAY_URL）を設定してください")
    return Settings(
        llm_base_url=base_url,
        llm_model=os.getenv("VL_MODEL", "qwen3.8-27b"),
        llm_api_key=os.getenv("VL_API_KEY", "dummy"),
        searxng_url=os.getenv("SEARXNG_URL", "http://localhost:8888"),
    )
