"""论文信源：OpenAlex（覆盖 arXiv 在内的全学科论文，免费无 key，支持精确时间窗）。

注：arXiv 官方 API 在本机网络环境按 TLS 指纹拦截 Python httpx 客户端（curl 可用），
为保证可移植性改用 OpenAlex 作为论文信源（arxiv 论文同样收录，landing_page 指向 arxiv）。
"""
from __future__ import annotations

import httpx

URL = "https://api.openalex.org/works"
# AI 概念 ID + LLM 相关短语，保证高精度（情报场景优先查准）
FILTER = (
    'from_publication_date:{since},concepts.id:C154945302,'
    'title_and_abstract.search:"LLM benchmark" OR title_and_abstract.search:"LLM evaluation" OR '
    'title_and_abstract.search:"large language model benchmark" OR title_and_abstract.search:"large language model evaluation" OR '
    'title_and_abstract.search:"LLM dataset" OR title_and_abstract.search:"language model evaluation"'
)


def _abstract(inv: dict | None, cap: int = 1500) -> str:
    """OpenAlex 摘要以倒排索引存储，重建为文本。"""
    if not inv:
        return ""
    pos = [(p, w) for w, ps in inv.items() for p in ps]
    pos.sort()
    return " ".join(w for _, w in pos)[:cap]


async def search(client: httpx.AsyncClient, since: str, max_results: int = 12) -> list[dict]:
    r = await client.get(
        URL,
        params={
            "filter": FILTER.format(since=since),
            "sort": "publication_date:desc",
            "per-page": max_results,
        },
    )
    r.raise_for_status()
    items: list[dict] = []
    for w in r.json().get("results", []):
        title = (w.get("display_name") or "").strip()
        loc = w.get("primary_location") or {}
        url = loc.get("landing_page_url") or (f"https://doi.org/{w['doi']}" if w.get("doi") else "")
        if not title or not url:
            continue
        items.append(
            {
                "title": title,
                "url": url,
                "content": _abstract(w.get("abstract_inverted_index")),
                "source": "openalex",
                "published_date": (w.get("publication_date") or "")[:10],
                "extra": {"venue": (loc.get("source") or {}).get("display_name"), "cited_by": w.get("cited_by_count")},
            }
        )
    return items
