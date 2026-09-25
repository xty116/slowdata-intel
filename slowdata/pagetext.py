"""页面正文抓取与提取：表格转文本 + 内嵌 JSON（排行榜数据常藏于此）+ 页面日期。"""
from __future__ import annotations

import json
import re

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36 slowdata-intel/0.1"

_SKIP_TAGS = re.compile(
    r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>|<noscript[^>]*>.*?</noscript>"
    r"|<header[^>]*>.*?</header>|<footer[^>]*>.*?</footer>|<nav[^>]*>.*?</nav>",
    re.S | re.I,
)


def _tables_to_text(html: str) -> str:
    def repl(m: re.Match) -> str:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(0), re.S | re.I)
        out = []
        for row in rows[:80]:
            cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S | re.I)
            cleaned = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip() for c in cells]
            if any(cleaned):
                out.append(" | ".join(cleaned))
        return "\n[表格]\n" + "\n".join(out[:250]) + "\n"
    return re.sub(r"<table[^>]*>.*?</table>", repl, html, flags=re.S | re.I)


def _embedded_json(html: str, cap: int = 2500) -> str:
    """提取 <script type="application/json"> 与 __NEXT_DATA__ 内嵌数据（排行榜等）。"""
    parts: list[str] = []
    patterns = [
        r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        r'<script[^>]*id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
    ]
    for pat in patterns:
        for mm in re.finditer(pat, html, re.S | re.I):
            raw = mm.group(1).strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                parts.append(json.dumps(obj, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                parts.append(raw)
            if sum(len(p) for p in parts) >= cap:
                break
        if sum(len(p) for p in parts) >= cap:
            break
    return ("\n".join(parts))[:cap]


async def fetch_page_text(
    client: httpx.AsyncClient,
    url: str,
    max_len: int = 6000,
    render_proxy: str | None = None,
) -> dict:
    """抓取网页，返回 {'text','date','title'}。

    直连抓取内容过少时（JS 客户端渲染页面），自动改用渲染代理（默认 r.jina.ai）
    获取渲染后的 Markdown，解决排行榜类页面的数据提取问题。
    """
    direct = await _direct_fetch(client, url, max_len)
    if len(direct["text"]) >= 300 or direct["date"] or direct["had_table"]:
        return direct
    if not render_proxy:
        return direct
    try:
        r = await client.get(f"{render_proxy}{url}", timeout=60, headers={"User-Agent": UA})
        if r.status_code != 200:
            return direct
        md = r.text
        if not md or "Markdown Content" not in md:
            return direct
        body = md.split("Markdown Content:", 1)[-1]
        title = ""
        m = re.search(r"^Title:\s*(.+)$", md, re.M)
        if m:
            title = m.group(1).strip()[:300]
        date = direct["date"]
        m = re.search(r"Published Time:\s*([\w\d:+.\-]+)", md)
        if m:
            date = m.group(1)[:19]
        return {"text": body.strip()[:max_len], "date": date, "title": title}
    except Exception:  # noqa: BLE001
        return direct


async def _direct_fetch(client: httpx.AsyncClient, url: str, max_len: int) -> dict:
    empty = {"text": "", "date": None, "title": "", "had_table": False}
    try:
        r = await client.get(url, headers={"User-Agent": UA}, timeout=15, follow_redirects=True)
        if r.status_code != 200:
            return empty
        ct = r.headers.get("content-type", "")
        if "html" not in ct and "text" not in ct:
            return empty
        html = r.text[:2_000_000]
    except Exception:  # noqa: BLE001
        return empty

    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()[:300] if m else ""

    date = None
    for pat in (
        r'<meta[^>]+(?:article:published_time|datePublished|pubdate|og:article:published_time)[^>]+content=["\']([^"\']+)',
        r'<time[^>]+datetime=["\']([^"\']+)',
    ):
        mm = re.search(pat, html, re.I)
        if mm:
            date = mm.group(1)[:19]
            break

    embedded = _embedded_json(html)
    had_table = "<table" in html.lower()
    body = _tables_to_text(_SKIP_TAGS.sub(" ", html))
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body)
    text = ("[页面数据]\n" + embedded + "\n" if embedded else "") + body
    return {"text": text.strip()[:max_len], "date": date, "title": title, "had_table": had_table}
