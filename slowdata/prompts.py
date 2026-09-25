"""Prompt 加载：优先 config/prompts/*.md，缺失时用内置默认。"""
from __future__ import annotations

from functools import lru_cache

from .config import ROOT

_DEFAULTS: dict[str, str] = {}

with open(ROOT / "config" / "prompts" / "triage.md", encoding="utf-8") as _f:
    _DEFAULTS["triage"] = _f.read()


@lru_cache(maxsize=None)
def load(name: str) -> str:
    p = ROOT / "config" / "prompts" / f"{name}.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return _DEFAULTS.get(name, "")
