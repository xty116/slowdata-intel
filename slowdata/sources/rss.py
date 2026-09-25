"""RSS 订阅信源（feedparser，L0）。单个源失败仅告警，不影响整体运行。"""
from __future__ import annotations

import httpx
import yaml

from ..config import ROOT


def _feeds() -> list[dict]:
    p = ROOT / "config" / "sources.yaml"
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("feeds", [])


async def search(client: httpx.AsyncClient, limit_per_feed: int = 12) -> list[dict]:
    import feedparser

    items: list[dict] = []
    for feed in _feeds():
        name = feed.get("name", "")
        url = feed.get("url", "")
        if not url:
            continue
        try:
            r = await client.get(url, timeout=20, follow_redirects=True)
            if r.status_code != 200:
                print(f"[collect] RSS {name} HTTP {r.status_code}")
                continue
            d = feedparser.parse(r.content)
            for e in d.entries[:limit_per_feed]:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                if not title or not link:
                    continue
                items.append(
                    {
                        "title": title,
                        "url": link,
                        "content": (e.get("summary") or "")[:1200],
                        "source": f"rss:{name}",
                        "published_date": e.get("published") or e.get("updated") or "",
                    }
                )
        except Exception as ex:  # noqa: BLE001
            print(f"[collect] RSS {name} 失败: {ex}")
    return items
