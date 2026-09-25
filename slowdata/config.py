"""全局配置加载：config.yaml / watchlist.yaml / .env。"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _load_yaml(name: str) -> dict:
    with open(ROOT / "config" / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


CONFIG = _load_yaml("config.yaml")
WATCHLIST = _load_yaml("watchlist.yaml")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

if not DEEPSEEK_API_KEY:
    raise RuntimeError("缺少 DEEPSEEK_API_KEY，请检查 .env 文件")
if not TAVILY_API_KEY:
    raise RuntimeError("缺少 TAVILY_API_KEY，请检查 .env 文件")
