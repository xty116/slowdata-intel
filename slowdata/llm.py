"""DeepSeek 分层 LLM 客户端。

L1 = deepseek-chat（判断/抽取/筛选，JSON 模式，temperature 0）
L2 = deepseek-reasoner（综合/方针/探测）
含：并发信号量、指数退避重试、逐节点用量与美元成本追踪、预算降级。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import CONFIG, DEEPSEEK_API_KEY

# 官方价格（美元 / 百万 token），KV 缓存命中按 0.1 计（以官方最新价为准）
PRICING: dict[str, dict[str, float]] = {
    "deepseek-chat": {"in": 0.28, "out": 0.42, "cache_hit_ratio": 0.1},
    "deepseek-reasoner": {"in": 0.28, "out": 2.19, "cache_hit_ratio": 0.1},
}


@dataclass
class UsageStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    per_node: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, node: str, model: str, usage: dict) -> None:
        p = PRICING.get(model, {"in": 0.28, "out": 0.42, "cache_hit_ratio": 0.1})
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_tokens") or 0) - hit
        comp = int(usage.get("completion_tokens") or 0)
        cost = (hit * p["in"] * p["cache_hit_ratio"] + miss * p["in"] + comp * p["out"]) / 1e6
        self.calls += 1
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += comp
        self.cost_usd += cost
        n = self.per_node.setdefault(node, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0})
        n["calls"] += 1
        n["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        n["completion_tokens"] += comp
        n["cost_usd"] += cost

    def budget_frac(self, budget: float) -> float:
        return self.cost_usd / budget if budget > 0 else 0.0


def parse_json(text: str) -> Any:
    """从模型输出中鲁棒地提取 JSON（兼容 ```json 围栏与前后杂讯）。"""
    if not text:
        raise ValueError("empty text")
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", t, re.S)
    if fence:
        t = fence.group(1)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}|\[.*\]", t, re.S)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"no JSON found: {text[:200]!r}")


class LLM:
    def __init__(self) -> None:
        cfg = CONFIG["llm"]
        self.base_url = cfg["base_url"]
        self.l1 = cfg["tier_l1"]
        self.l2 = cfg["tier_l2"]
        self.timeout = float(cfg.get("timeout_s", 180))
        self.max_retries = int(cfg.get("max_retries", 4))
        self.sem = asyncio.Semaphore(int(cfg.get("concurrency", 4)))
        self.budget = float(cfg.get("daily_budget_usd", 0.5))
        self.stats = UsageStats()
        self.warnings: list[str] = []
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            timeout=self.timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _model_for(self, tier: str) -> str:
        return self.l1 if tier == "l1" else self.l2

    def pick_tier(self, node: str, want: str) -> str:
        """预算压力大时，允许综合/方针类 L2 节点降级为 L1（并告警）。"""
        degradable = {"synthesize", "policy"}
        if want == "l2" and node in degradable and self.stats.budget_frac(self.budget) > 0.6:
            self.warnings.append(f"[预算降级] 节点 {node} 由 reasoner 降级为 chat（已用预算 {self.stats.budget_frac(self.budget):.0%}）")
            return "l1"
        return want

    async def _complete(
        self,
        tier: str,
        system: str,
        user: str,
        node: str,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        model = self._model_for(tier)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        if json_mode and tier == "l1":
            payload["response_format"] = {"type": "json_object"}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens:
            payload["max_tokens"] = max_tokens

        last_err: Exception | None = None
        async with self.sem:
            for attempt in range(self.max_retries):
                try:
                    r = await self._client.post("/chat/completions", json=payload)
                    if r.status_code == 200:
                        data = r.json()
                        self.stats.add(node, model, data.get("usage") or {})
                        return data["choices"][0]["message"]["content"] or ""
                    if r.status_code in (429, 500, 502, 503):
                        last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                    else:
                        raise RuntimeError(f"DeepSeek HTTP {r.status_code}: {r.text[:300]}")
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    last_err = e
                wait = min(2 ** attempt + random.uniform(0, 1), 30)
                await asyncio.sleep(wait)
        raise RuntimeError(f"LLM 调用失败（{node}/{model}）: {last_err}")

    async def chat(self, node: str, system: str, user: str, json_mode: bool = False) -> str:
        """L1 轻量调用。"""
        return await self._complete("l1", system, user, node, json_mode=json_mode, temperature=0.0)

    async def reason(self, node: str, system: str, user: str, max_tokens: int | None = None) -> str:
        """L2 推理调用（自动预算降级）。"""
        tier = self.pick_tier(node, "l2")
        return await self._complete(tier, system, user, node, max_tokens=max_tokens)

    async def chat_json(self, node: str, system: str, user: str) -> Any:
        return parse_json(await self.chat(node, system, user, json_mode=True))
