"""GitHub 开源社区信源（REST 搜索 API，免费、免鉴权低频可用）。"""
from __future__ import annotations

import httpx

URL = "https://api.github.com/search/repositories"


async def search(client: httpx.AsyncClient, since: str, max_results: int = 10) -> list[dict]:
    """按创建时间窗口搜索评测/数据集相关仓库。"""
    q = f"(llm benchmark) OR (llm evaluation) OR (ai training dataset) OR (eval harness) created:>{since}"
    r = await client.get(
        URL,
        params={"q": q, "sort": "stars", "order": "desc", "per_page": max_results},
        headers={"Accept": "application/vnd.github+json", "User-Agent": "slowdata-agent/0.1"},
    )
    r.raise_for_status()
    items: list[dict] = []
    for repo in r.json().get("items", []):
        desc = repo.get("description") or ""
        title = repo.get("full_name", "") + (f" — {desc}" if desc else "")
        if not title or not repo.get("html_url"):
            continue
        items.append(
            {
                "title": title[:300],
                "url": repo["html_url"],
                "content": desc,
                "source": "github",
                "published_date": (repo.get("created_at") or "")[:10],
                "extra": {"stars": repo.get("stargazers_count"), "language": repo.get("language")},
            }
        )
    return items
