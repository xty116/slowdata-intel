"""HuggingFace 信源：每日论文（daily_papers）+ 趋势数据集（trending）。"""
from __future__ import annotations

import httpx

BASE = "https://huggingface.co"


async def daily_papers(client: httpx.AsyncClient, limit: int = 10) -> list[dict]:
    r = await client.get(f"{BASE}/api/daily_papers")
    r.raise_for_status()
    data = r.json()
    rows = data if isinstance(data, list) else (data.get("papers") or [])
    items: list[dict] = []
    for row in rows[:limit]:
        p = row.get("paper") or {}
        pid = p.get("id") or ""
        title = (p.get("title") or "").strip()
        if not title or not pid:
            continue
        items.append(
            {
                "title": title,
                "url": f"{BASE}/papers/{pid}",
                "content": (p.get("summary") or "").strip()[:1500],
                "source": "huggingface",
                "published_date": ((p.get("publishedAt") or row.get("submittedOnDailyAt") or "") or "")[:10],
            }
        )
    return items


async def trending_datasets(client: httpx.AsyncClient, limit: int = 10) -> list[dict]:
    r = await client.get(f"{BASE}/api/trending", params={"type": "dataset", "limit": limit})
    r.raise_for_status()
    data = r.json()
    rows = data.get("recentlyTrending") if isinstance(data, dict) else data
    items: list[dict] = []
    for row in rows or []:
        rd = row.get("repoData") or {}
        rid = rd.get("id") or row.get("repo_id") or ""
        if not rid:
            continue
        card = rd.get("cardData") or {}
        items.append(
            {
                "title": rid,
                "url": f"{BASE}/datasets/{rid}",
                "content": (card.get("description") or card.get("title") or "")[:800],
                "source": "huggingface",
                "published_date": ((rd.get("lastModified") or rd.get("createdAt") or "") or "")[:10],
            }
        )
    return items
