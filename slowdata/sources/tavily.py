"""Tavily 检索连接器（主动搜索信源）。"""
from __future__ import annotations

import httpx

from ..config import CONFIG, TAVILY_API_KEY

URL = "https://api.tavily.com/search"


async def search(
    client: httpx.AsyncClient,
    query: str,
    max_results: int | None = None,
    depth: str | None = None,
    days: int | None = None,
) -> list[dict]:
    """执行一次检索，返回归一化条目列表。失败抛出异常由上层捕获。

    days: 只返回最近 N 天内的结果（时间窗硬约束的第一道过滤）。
    """
    cfg = CONFIG["search"]
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": depth or cfg.get("search_depth", "advanced"),
        "max_results": max_results or int(cfg.get("max_results_per_query", 6)),
        "include_answer": False,
        "include_raw_content": False,
    }
    if days:
        payload["days"] = days
    r = await client.post(URL, json=payload)
    r.raise_for_status()
    data = r.json()
    items = []
    for res in data.get("results", []):
        title = (res.get("title") or "").strip()
        url = (res.get("url") or "").strip()
        if not title or not url:
            continue
        items.append(
            {
                "title": title,
                "url": url,
                "content": (res.get("content") or "").strip()[:4000],
                "score": res.get("score"),
                "published_date": res.get("published_date") or "",
            }
        )
    return items
