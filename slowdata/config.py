"""全局配置加载：config.yaml / watchlist.yaml / .env。"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

# ROOT 定位策略：包内 config 存在（本地可编辑安装）→ 用包路径；否则回退到工作目录（容器部署）
_PKG_ROOT = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("SLOWDATA_ROOT", _PKG_ROOT if (_PKG_ROOT / "config" / "config.yaml").exists() else Path.cwd()))
load_dotenv(ROOT / ".env")


def _load_yaml(name: str) -> dict:
    with open(ROOT / "config" / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


CONFIG = _load_yaml("config.yaml")
WATCHLIST = _load_yaml("watchlist.yaml")

# 云部署：SLOWDATA_DATA_DIR 指向持久化磁盘（Render 等），覆盖数据与报告目录
_DATA_DIR = os.environ.get("SLOWDATA_DATA_DIR", "").strip()
if _DATA_DIR:
    CONFIG.setdefault("report", {})["db_path"] = os.path.join(_DATA_DIR, "slowdata.db")
    CONFIG.setdefault("report", {})["output_dir"] = os.path.join(_DATA_DIR, "reports")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

if not DEEPSEEK_API_KEY:
    raise RuntimeError("缺少 DEEPSEEK_API_KEY，请检查 .env 文件")
if not TAVILY_API_KEY:
    raise RuntimeError("缺少 TAVILY_API_KEY，请检查 .env 文件")
