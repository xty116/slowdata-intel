"""日度流水线节点（纯函数，供 LangGraph 编排或线性降级运行器调用）。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import date, timedelta
from typing import Any

import httpx

from . import prompts
from .config import CONFIG, WATCHLIST
from .dates import in_window
from .llm import LLM, parse_json
from .embeddings import cluster_by_similarity, embed
from .pagetext import fetch_page_text
from .sources import github, huggingface, openalex, rss, tavily
from .store import Store

DIMS = {"benchmark", "competitor", "talent", "supplier"}
# 榜单/排行榜类 URL 特征（抓取与摘要均优先）
LB_KEYS = ("leaderboard", "benchmark", "ranking", "swe-bench", "swebench", "gpqa", "livebench", "arena")


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# ① 查询生成：Watchlist 模板展开（L0） + 今日热点探测（L2，best-effort）
# ---------------------------------------------------------------------------
async def node_generate_queries(state: dict, llm: LLM) -> dict:
    cfg = CONFIG["search"]
    cap = int(cfg.get("max_queries", 12))
    queries: list[str] = []
    for topic in sorted(WATCHLIST.get("topics", []), key=lambda t: t.get("priority", 3)):
        queries.extend(topic.get("queries", []))
        if len(queries) >= cap - 4:
            break
    # L2 热点探测
    hot: list[str] = []
    try:
        sys_p = prompts.load("queries").replace("{date}", state["date"]).replace("{n}", "4")
        out = await llm.reason("generate_queries", sys_p, "请输出今天的检索建议。")
        obj = parse_json(out)
        for q in obj.get("queries", []):
            q = str(q).strip()
            if q and q not in queries and q not in hot:
                hot.append(q)
    except Exception as e:  # noqa: BLE001
        llm.warnings.append(f"[queries] 热点探测失败，使用基础词表: {e}")
    queries.extend(hot[:4])
    seen: set[str] = set()
    final: list[str] = []
    for q in queries:
        k = q.strip().lower()
        if k and k not in seen:
            seen.add(k)
            final.append(q.strip())
    state["queries"] = final[:cap]
    return state


# ---------------------------------------------------------------------------
# ② 并行收集：Tavily + arXiv + GitHub + HuggingFace（L0，无 LLM）
#    时间窗硬约束：仅保留提需时间前 N 天（默认 7 天）内的资料；
#    无日期条目按 undated_policy 处理（默认保留并标记"时间未知"）。
# ---------------------------------------------------------------------------
async def node_collect(state: dict, llm: LLM) -> dict:
    cfg = CONFIG["search"]
    src = CONFIG["sources"]
    days = int(src.get("time_window_days", 7))
    run_date = date.fromisoformat(state["date"])
    enabled = set(src.get("enabled", ["tavily", "papers", "github", "huggingface"]))
    sem = asyncio.Semaphore(int(cfg.get("collect_concurrency", 3)))

    async def _tavily_one(client: httpx.AsyncClient, q: str) -> list[dict]:
        async with sem:
            try:
                return await tavily.search(client, q, days=days)
            except Exception as e:  # noqa: BLE001
                print(f"[collect] Tavily 检索失败 {q!r}: {e}")
                return []

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "slowdata-agent/0.1"}) as client:
        tav_batches = (
            await asyncio.gather(*(_tavily_one(client, q) for q in state["queries"]))
            if "tavily" in enabled
            else []
        )
        extra: dict[str, list[dict]] = {}
        for name in ("papers", "github", "huggingface", "rss"):
            if name not in enabled:
                continue
            try:
                if name == "papers":
                    extra[name] = await openalex.search(
                        client, since=(run_date - timedelta(days=days)).isoformat()
                    )
                elif name == "github":
                    extra[name] = await github.search(
                        client, since=(run_date - timedelta(days=days)).isoformat()
                    )
                elif name == "rss":
                    extra[name] = await rss.search(client)
                else:
                    extra[name] = await huggingface.daily_papers(client) + await huggingface.trending_datasets(client)
            except Exception as e:  # noqa: BLE001
                print(f"[collect] {name} 信源失败: {e}")
                extra[name] = []

    items: list[dict] = []
    seen_hash: set[str] = set()
    dropped_dated = 0
    undated = 0
    per_source: dict[str, int] = {}

    def _add(it: dict, query: str = "") -> None:
        nonlocal dropped_dated, undated
        url = it.get("url", "")
        if not url:
            return
        h = sha1(url)
        if h in seen_hash:
            return
        pd = (it.get("published_date") or "").strip()
        w = in_window(pd, run_date, days) if pd else None
        if w is False:
            dropped_dated += 1
            return
        it["url_hash"] = h
        it["query"] = query
        if w is None:
            undated += 1
            it["date_unknown"] = True
        seen_hash.add(h)
        items.append(it)
        per_source[it["source"]] = per_source.get(it["source"], 0) + 1

    for q, batch in zip(state["queries"], tav_batches):
        for it in batch:
            it["source"] = "tavily"
            _add(it, query=q)
    for batch in extra.values():
        for it in batch:
            _add(it)

    state["items"] = items
    state.setdefault("warnings", []).append(
        f"[collect] 各信源: {per_source or {'无': 0}} · 时间窗(前{days}天)丢弃超窗 {dropped_dated} 条 · 时间未知 {undated} 条"
    )
    return state


# ---------------------------------------------------------------------------
# ③ 初筛：相关性 + 四维度分类 + 重要度打分（L1，批量 JSON）
# ---------------------------------------------------------------------------
async def node_triage(state: dict, llm: LLM) -> dict:
    items = state["items"]
    if not items:
        state["items"] = []
        return state
    sys_p = prompts.load("triage")
    batch = int(CONFIG["triage"].get("batch_size", 8))
    judged: dict[int, dict] = {}
    for start in range(0, len(items), batch):
        chunk = items[start : start + batch]
        payload = [
            {
                "id": i,
                "title": it["title"][:300],
                "content": it.get("content", "")[:1200],
                "published_date": it.get("published_date", ""),
            }
            for i, it in enumerate(chunk)
        ]
        try:
            obj = await llm.chat_json("triage", sys_p, json.dumps(payload, ensure_ascii=False))
            for r in obj.get("results", []):
                idx = int(r.get("id", -1))
                if 0 <= idx < len(chunk):
                    dims = [d for d in r.get("dimensions", []) if d in DIMS]
                    judged[start + idx] = {
                        "relevant": bool(r.get("relevant", True)),
                        "dimensions": dims or ["benchmark"],
                        "importance": max(1, min(5, int(r.get("importance", 3)))),
                        "date_hint": str(r.get("date_hint", "") or "").strip()[:10],
                        "reason": str(r.get("reason", ""))[:40],
                    }
        except Exception as e:  # noqa: BLE001
            llm.warnings.append(f"[triage] 批次 {start}-{start + batch} 解析失败，保守保留: {e}")
            for i in range(len(chunk)):
                judged[start + i] = {"relevant": True, "dimensions": ["benchmark"], "importance": 3, "date_hint": "", "reason": "解析失败保守保留"}
    # 时间窗复核：初筛提取到的日期线索超窗 → 丢弃；窗内 → 补全日期
    days = int(CONFIG["sources"].get("time_window_days", 7))
    run_date = date.fromisoformat(state["date"])
    kept: list[dict] = []
    dropped_hint = 0
    filled = 0
    for i, it in enumerate(items):
        j = judged.get(i)
        if j is None:
            j = {"relevant": True, "dimensions": ["benchmark"], "importance": 3, "date_hint": "", "reason": "未覆盖"}
        if not j["relevant"]:
            continue
        hint = j.pop("date_hint", "")
        if hint:
            w = in_window(hint, run_date, days)
            if w is False:
                dropped_hint += 1
                continue
            if w is True and not it.get("published_date"):
                it["published_date"] = hint
                it.pop("date_unknown", None)
                filled += 1
        kept.append({**it, **j})
    if dropped_hint or filled:
        state.setdefault("warnings", []).append(
            f"[triage] 日期线索复核：补全 {filled} 条、超窗丢弃 {dropped_hint} 条"
        )
    kept.sort(key=lambda x: -x["importance"])
    state["items"] = kept
    state.setdefault("warnings", []).append(f"[triage] 保留 {len(kept)}/{len(items)} 条相关条目")
    return state


# ---------------------------------------------------------------------------
# ③.5 网页正文抓取（L0）：高重要度条目抓原文 → 提取正文/表格/内嵌JSON/页面日期；
#     页面日期证实超窗 → 丢弃；证实窗内 → 补齐日期（消除"时间未知"）。
# ---------------------------------------------------------------------------
async def node_fetch_content(state: dict, llm: LLM) -> dict:
    src = CONFIG["sources"]
    if not src.get("fetch_pages", True):
        return state
    days = int(src.get("time_window_days", 7))
    run_date = date.fromisoformat(state["date"])
    items = state["items"]
    # 抓取优先级：榜单/排行榜类 URL 优先（页面内嵌数据价值最高），其次重要度

    def _priority(it: dict) -> tuple:
        url = (it.get("url") or "").lower()
        is_lb = 1 if any(k in url for k in LB_KEYS) else 0
        return (is_lb, int(it.get("importance", 3)), -len(it.get("content", "")))

    targets = sorted(
        [it for it in items if int(it.get("importance", 3)) >= 3],
        key=_priority,
        reverse=True,
    )[: int(src.get("fetch_max", 20))]
    if not targets:
        return state
    sem = asyncio.Semaphore(int(src.get("fetch_concurrency", 4)))

    async def one(client: httpx.AsyncClient, it: dict):
        async with sem:
            return it, await fetch_page_text(
                client, it["url"], render_proxy=src.get("render_proxy") or None
            )

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        results = await asyncio.gather(*(one(client, it) for it in targets))

    dropped = 0
    dated_now = 0
    got_text = 0
    for it, info in results:
        if info["text"]:
            it["page_text"] = info["text"]
            got_text += 1
        if info["title"] and not it.get("title"):
            it["title"] = info["title"]
        it["page_date"] = info["date"] or ""
        d = info["date"]
        if d:
            w = in_window(d, run_date, days)
            if w is True:
                it["published_date"] = it["published_date"] or d[:10]
                it.pop("date_unknown", None)
                dated_now += 1
            elif w is False:
                it["out_of_window"] = True
                dropped += 1
    state["items"] = [it for it in items if not it.get("out_of_window")]
    state.setdefault("warnings", []).append(
        f"[fetch] 抓取正文 {len(targets)} 条（成功 {got_text}）· 页面日期确认窗内 {dated_now} 条 · 页面证实超窗丢弃 {dropped} 条"
    )
    return state


# ---------------------------------------------------------------------------
# ④ 语义去重 + 跨日比对（L0：本地嵌入 + 并查集聚类）
# ---------------------------------------------------------------------------
def node_dedup(state: dict, llm: LLM, store: Store) -> dict:
    items = state["items"]
    if not items:
        state["clusters"] = []
        return state
    thr = float(CONFIG["dedup"].get("cosine_threshold", 0.92))
    # 去重嵌入只取核心文本（标题+正文开头+页面开头），控制序列长度，避免 CPU 嵌入过慢
    texts = [f"{it['title']}. {it.get('content', '')[:200]}. {it.get('page_text', '')[:200]}" for it in items]
    t0 = time.time()
    embs = embed(texts)
    for i, it in enumerate(items):
        it["embedding"] = embs[i] if embs is not None else None
    state.setdefault("warnings", []).append(
        f"[dedup] 嵌入方式: {CONFIG['dedup'].get('embedding_model', 'local') if embs is not None else '标题相似度兜底'}（{time.time() - t0:.1f}s）"
    )
    groups = cluster_by_similarity(items, embs, thr)

    # 跨日比对：簇代表 vs 近 N 天存量条目（"慢数据"跨天去重的关键）
    cross_days = int(CONFIG["dedup"].get("cross_run_days", 7))
    old = store.recent_embeddings(days=cross_days)
    clusters: list[dict] = []
    for gi, idxs in enumerate(groups):
        members = [items[i] for i in idxs]
        members.sort(key=lambda x: (-int(x.get("importance", 3)), -len(x.get("content", "")), -(x.get("score") or 0)))
        rep = members[0]
        is_new = True
        rep_emb = rep.get("embedding")
        if rep_emb is not None and old:
            from .embeddings import cosine

            for _, o_emb in old:
                if cosine(rep_emb, o_emb) >= thr:
                    is_new = False
                    break
        for m in members:
            m["cluster_id"] = gi
            m["is_new"] = 1 if is_new else 0
        clusters.append({"id": gi, "items": members, "rep": rep, "is_new": is_new})
    clusters.sort(key=lambda c: (-int(c["rep"].get("importance", 3)), int(c["is_new"])))
    state["clusters"] = clusters
    state.setdefault("warnings", []).append(f"[dedup] {len(items)} 条 → {len(clusters)} 个事件簇（跨 {cross_days} 天比对 {'已执行' if old else '无存量'}）")
    return state


# ---------------------------------------------------------------------------
# ⑤ 簇摘要 + 实体抽取（L1；仅重要簇走 LLM，其余用标题）
# ---------------------------------------------------------------------------
async def node_summarize(state: dict, llm: LLM) -> dict:
    min_imp = int(CONFIG["triage"].get("min_importance_report", 3))
    cap = int(CONFIG["triage"].get("max_clusters_summarized", 16))
    sys_p = prompts.load("summarize")
    clusters = state["clusters"]

    def _sum_priority(c: dict) -> tuple:
        url = (c["rep"].get("url") or "").lower()
        is_lb = 1 if any(k in url for k in LB_KEYS) else 0
        return (is_lb, int(c["rep"].get("importance", 3)), int(c.get("is_new")))

    def _needs_summary(c: dict) -> bool:
        # 已摘要且本轮无新增回搜条目的簇跳过，避免 Critic 回搜后重复烧钱
        if c["rep"].get("summary") and not any(it.get("from_research") for it in c["items"]):
            return False
        return True

    pool = sorted([c for c in clusters if _needs_summary(c)], key=_sum_priority, reverse=True)
    top = [c for c in pool if int(c["rep"].get("importance", 3)) >= min_imp][:cap]
    # 配额未满时补入榜单类（重要度≥3）簇，保证排行榜数据进入摘要
    if len(top) < cap:
        for c in pool:
            if c not in top and _sum_priority(c)[0] == 1 and int(c["rep"].get("importance", 3)) >= 3:
                top.append(c)
                if len(top) >= cap:
                    break
    todo = [c for c in clusters if c not in top]
    for c in top:
        rep = c["rep"]
        content = rep.get("content", "")[:1500]
        page = rep.get("page_text", "")
        if page:
            content += "\n[网页正文]\n" + page[:2500]
        # 注意：模板内含 JSON 示例花括号，不能用 str.format，用 replace 精确填充
        user = sys_p.replace("{title}", rep["title"][:300]).replace("{content}", content)
        try:
            obj = await llm.chat_json("summarize", "你是情报摘要员。", user)
            rep["summary"] = str(obj.get("summary", "")).strip()[:600]
            ents: list[dict] = []
            for e in obj.get("entities", []):
                if isinstance(e, str) and e.strip():
                    ents.append({"name": e.strip(), "type": "company"})
                elif isinstance(e, dict) and e.get("name"):
                    ents.append({"name": str(e["name"]).strip(), "type": str(e.get("type", "company"))})
            rep["entities"] = ents[:6]
        except Exception as e:  # noqa: BLE001
            llm.warnings.append(f"[summarize] 簇 {c['id']} 摘要失败: {e}")
            rep["summary"] = rep["title"]
            rep["entities"] = []
    for c in todo:
        if not c["rep"].get("summary"):
            c["rep"]["summary"] = ""
            c["rep"]["entities"] = []
    state["clusters"] = clusters
    return state


# ---------------------------------------------------------------------------
# ⑥ 趋势综合（L2，可预算降级） / ⑦ 数据方针（L2，可预算降级）
# ---------------------------------------------------------------------------
def _brief_for_llm(clusters: list[dict]) -> str:
    marks = {"support": "✅", "contradict": "❌", "unverified": "❓"}
    lines = []
    for n, c in enumerate(clusters, 1):
        rep = c["rep"]
        dims = ",".join(rep.get("dimensions", []))
        new = "新" if c["is_new"] else "复现"
        pd = rep.get("published_date") or ("时间未知" if rep.get("date_unknown") else "")
        lines.append(
            f"#{n} [{dims}|重要度{rep.get('importance', 3)}|{new}|{pd}] {rep['title']}"
            + (f"\n   摘要: {rep['summary']}" if rep.get("summary") else "")
            + f"\n   来源: {rep['url']}"
        )
        if rep.get("claims"):
            parts = [f"{marks.get(x['verdict'], '?')}{x['claim'][:70]}" for x in rep["claims"]]
            lines.append(f"   核验: {' | '.join(parts)}")
            if rep.get("resolution"):
                lines.append(
                    f"   裁决: {rep['resolution'].get('resolution', '')} (置信度 {rep['resolution'].get('confidence', '?')})"
                )
    return "\n".join(lines)


async def node_synthesize(state: dict, llm: LLM) -> dict:
    brief = _brief_for_llm(state["clusters"])
    if not brief.strip():
        state["synthesis"] = "今日未采集到相关信号。"
        return state
    sys_p = prompts.load("synthesize")
    user = f"今天是 {state['date']}。今日采集线索：\n\n{brief}"
    state["synthesis"] = await llm.reason("synthesize", sys_p, user)
    return state


async def node_policy(state: dict, llm: LLM) -> dict:
    sys_p = prompts.load("policy")
    user = f"今天是 {state['date']}。\n\n# 今日情报\n\n{state['synthesis']}"
    state["policy"] = await llm.reason("policy", sys_p, user)
    return state


# ---------------------------------------------------------------------------
# ⑧.5 M2 节点：Claim 核验 → 实体事件 → 综合 → 方针 → Critic 回路 → 回搜
# ---------------------------------------------------------------------------
async def node_verify(state: dict, llm: LLM, store: Store) -> dict:
    """Claim 级证据核验：L1 拆解断言 → 逐条 Tavily 搜证 → L1 判定 → L2 矛盾裁决。"""
    cfg = CONFIG.get("verify", {})
    min_imp = int(cfg.get("min_importance", 4))
    max_clusters = int(cfg.get("max_clusters", 6))
    max_claims = int(cfg.get("max_claims_per_cluster", 3))
    days = int(CONFIG["sources"].get("time_window_days", 7))
    targets = [
        c
        for c in state["clusters"]
        if int(c["rep"].get("importance", 3)) >= min_imp and not c["rep"].get("claims")
    ][:max_clusters]
    if not targets:
        return state
    sys_claims = prompts.load("verify_claims")
    sys_verdict = prompts.load("verify_verdict")
    sys_resolve = prompts.load("resolve")
    sem = asyncio.Semaphore(3)
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "slowdata-agent/0.1"}) as client:
        for c in targets:
            rep = c["rep"]
            content = f"标题：{rep['title']}\n摘要：{rep.get('summary', '')}\n正文：{rep.get('page_text', '')[:1200]}"
            claims: list[str] = []
            try:
                obj = await llm.chat_json(
                    "verify_decompose", "你是事实核验员。", sys_claims.replace("{content}", content)
                )
                claims = [str(x).strip() for x in obj.get("claims", []) if str(x).strip()][:max_claims]
            except Exception as e:  # noqa: BLE001
                llm.warnings.append(f"[verify] 拆解失败 {rep['title'][:30]}: {e}")
            rep["claims"] = []
            for claim in claims:
                try:
                    results = await tavily.search(
                        client,
                        claim,
                        max_results=int(cfg.get("search_results", 3)),
                        depth=cfg.get("depth", "basic"),
                        days=days,
                    )
                except Exception as e:  # noqa: BLE001
                    llm.warnings.append(f"[verify] 检索失败: {e}")
                    results = []
                evidence = [{"title": r["title"][:100], "url": r["url"]} for r in results[:3]]
                verdict, reason = "unverified", "检索无结果"
                if results:
                    ev_text = "\n".join(
                        f"- {r['title'][:120]}（{r.get('published_date') or '无日期'}）{r.get('content', '')[:200]}"
                        for r in results[:3]
                    )
                    try:
                        obj2 = await llm.chat_json(
                            "verify_verdict",
                            "你是事实核验员。",
                            sys_verdict.replace("{claim}", claim).replace("{evidence}", ev_text),
                        )
                        v = str(obj2.get("verdict", "unverified"))
                        verdict = v if v in ("support", "contradict", "unverified") else "unverified"
                        reason = str(obj2.get("reason", ""))[:60]
                    except Exception as e:  # noqa: BLE001
                        llm.warnings.append(f"[verify] 判定失败: {e}")
                rep["claims"].append({"claim": claim, "verdict": verdict, "reason": reason, "evidence": evidence})
                store.save_claim(state["run_id"], rep["url_hash"], claim, verdict, reason, evidence)
            # 矛盾裁决（L2）：存在与证据冲突/未证实的断言时
            conflicted = [x for x in rep["claims"] if x["verdict"] in ("contradict", "unverified")]
            if conflicted:
                try:
                    ev = "\n".join(
                        f"- [{x['claim'][:80]}] 判定 {x['verdict']}："
                        + "; ".join(e["title"][:80] for e in x["evidence"])
                        for x in conflicted
                    )
                    obj3 = await llm.reason(
                        "resolve",
                        sys_resolve,
                        sys_resolve.replace("{claim}", rep.get("summary", "")[:300]).replace("{evidence}", ev[:2500]),
                    )
                    res = parse_json(obj3)
                    rep["resolution"] = {
                        "adopt": str(res.get("adopt", "uncertain")),
                        "resolution": str(res.get("resolution", ""))[:200],
                        "confidence": float(res.get("confidence", 0.5)),
                    }
                except Exception as e:  # noqa: BLE001
                    llm.warnings.append(f"[verify] 裁决失败: {e}")
    n_claims = sum(len(c["rep"].get("claims", [])) for c in targets)
    n_res = sum(1 for c in targets if c["rep"].get("resolution"))
    state.setdefault("warnings", []).append(f"[verify] 核验 {len(targets)} 簇 / {n_claims} 条断言 · 矛盾裁决 {n_res} 起")
    return state


async def node_events(state: dict, llm: LLM, store: Store) -> dict:
    """实体/事件沉淀：摘要实体入实体库并链接条目；L1 提取事件入事件表。"""
    min_imp = int(CONFIG.get("verify", {}).get("min_importance", 4))
    sys_p = prompts.load("events")
    clusters = [c for c in state["clusters"] if int(c["rep"].get("importance", 3)) >= min_imp][:10]
    today_events: list[dict] = []
    for c in clusters:
        rep = c["rep"]
        item_id = store.item_id_by_hash(rep["url_hash"])
        for ent in rep.get("entities") or []:
            try:
                eid = store.upsert_entity(str(ent.get("name", "")).strip(), str(ent.get("type", "company")))
                if item_id:
                    store.link_item_entity(item_id, eid)
            except Exception:  # noqa: BLE001
                continue
        content = f"标题：{rep['title']}\n摘要：{rep.get('summary', '')}"
        try:
            obj = await llm.chat_json("events", "你是情报结构化员。", sys_p.replace("{content}", content))
            for ev in obj.get("events", [])[:2]:
                name = str(ev.get("entity", "")).strip()
                if not name:
                    continue
                eid = store.upsert_entity(name, str(ev.get("type", "company")))
                if item_id:
                    store.link_item_entity(item_id, eid)
                summary = str(ev.get("summary", ""))[:200]
                store.save_event(
                    state["run_id"],
                    eid,
                    str(ev.get("event_type", "org_news")),
                    summary,
                    [{"title": rep["title"], "url": rep["url"]}],
                    str(ev.get("happened_at", ""))[:10],
                    0.9,
                )
                today_events.append({"entity": name, "event_type": ev.get("event_type", "org_news"), "summary": summary})
        except Exception as e:  # noqa: BLE001
            llm.warnings.append(f"[events] 提取失败: {e}")
    state["today_events"] = today_events
    return state


async def node_critic(state: dict, llm: LLM) -> dict:
    """Critic 评审（L2 五维打分，含时效性）；未达标生成补充 query，回路有轮次/预算上限。"""
    cfg = CONFIG.get("critic", {})
    min_score = float(cfg.get("min_score", 7))
    max_rounds = int(cfg.get("max_rounds", 2))
    max_queries = int(cfg.get("max_extra_queries", 4))
    rnd = int(state.get("critic_round", 0)) + 1
    state["critic_round"] = rnd
    brief = _brief_for_llm(state["clusters"])
    sys_p = prompts.load("critic")
    user = f"日期：{state['date']}\n\n# 证据线索清单\n{brief}\n\n# 报告正文\n{state['synthesis']}\n\n# 数据方针\n{state['policy']}"
    try:
        obj = parse_json(await llm.reason("critic", sys_p, user))
        scores = {k: max(0, min(10, int(v))) for k, v in (obj.get("scores") or {}).items()}
        issues = obj.get("issues") or []
        verdict = str(obj.get("verdict", "fail"))
    except Exception as e:  # noqa: BLE001
        llm.warnings.append(f"[critic] 输出解析失败，视为通过（防止无限回搜）: {e}")
        scores = {"factuality": 7, "completeness": 7, "citations": 7, "timeliness": 7, "actionability": 7}
        issues, verdict = [], "pass"
    state.setdefault("critic_history", []).append({"round": rnd, "scores": scores, "issues": issues, "verdict": verdict})
    state["critic_scores"] = scores
    fails = [d for d, s in scores.items() if s < min_score] or (["overall"] if verdict == "fail" else [])
    if not fails:
        state["critic_pass"] = True
        state.setdefault("warnings", []).append(f"[critic] 第{rnd}轮通过 {scores}")
        return state
    queries: list[str] = []
    for iss in issues:
        for q in iss.get("queries", []):
            q = str(q).strip()
            if q and q not in queries:
                queries.append(q)
    state["re_search_queries"] = queries[:max_queries]
    if rnd >= max_rounds or not queries or llm.stats.budget_frac(llm.budget) > 0.85:
        state["critic_pass"] = True
        state["critic_failed_accept"] = True
        state.setdefault("warnings", []).append(
            f"[critic] 第{rnd}轮未达标且触发上限（轮次/预算/无query），降级接受：{scores}"
        )
    else:
        state.setdefault("warnings", []).append(f"[critic] 第{rnd}轮未达标 {scores}，回搜 {len(queries)} 条 query")
    return state


async def node_research(state: dict, llm: LLM) -> dict:
    """Critic 定向回搜：执行补充 query → 初筛 → 并入条目流，回到去重节点。"""
    qs = state.get("re_search_queries", [])
    if not qs:
        return state
    days = int(CONFIG["sources"].get("time_window_days", 7))
    sem = asyncio.Semaphore(3)

    async def one(client: httpx.AsyncClient, q: str) -> list[dict]:
        async with sem:
            try:
                return await tavily.search(client, q, max_results=4, depth="basic", days=days)
            except Exception as e:  # noqa: BLE001
                print(f"[research] 检索失败 {q!r}: {e}")
                return []

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "slowdata-agent/0.1"}) as client:
        batches = await asyncio.gather(*(one(client, q) for q in qs))
    existing = {it["url_hash"] for it in state["items"]}
    new_items: list[dict] = []
    for q, batch in zip(qs, batches):
        for it in batch:
            h = sha1(it["url"])
            if h in existing:
                continue
            existing.add(h)
            new_items.append({**it, "url_hash": h, "query": q, "source": "tavily", "from_research": True})
    if new_items:
        sub = await node_triage({"items": new_items, "date": state["date"]}, llm)
        state["items"].extend(sub["items"])
    state.setdefault("warnings", []).append(f"[research] 回搜 {len(qs)} 条 query → 新增 {len(new_items)} 条")
    state["re_search_queries"] = []
    return state



def node_persist(state: dict, llm: LLM, store: Store) -> dict:
    rows: list[dict] = []
    for c in state["clusters"]:
        for it in c["items"]:
            row = dict(it)
            row["run_id"] = state["run_id"]
            row["is_new"] = bool(it.get("is_new", True))
            if it.get("page_text"):
                row["raw"] = {**(it.get("raw") or {}), "page_text": (it.get("page_text") or "")[:2000]}
            if it is c["rep"]:
                row["summary"] = c["rep"].get("summary", "")
                row["entities"] = c["rep"].get("entities", [])
            else:
                row["summary"] = ""
                row["entities"] = []
                row["embedding"] = None
            rows.append(row)
    store.insert_items(rows)
    state["persisted"] = len(rows)
    return state


def _render_report(state: dict) -> str:
    from .report import render  # 延迟导入避免循环

    return render(state)


def node_render_report(state: dict, llm: LLM, store: Store | None = None) -> dict:
    # 渲染前注入最终运行统计（此时全部 LLM 调用已完成）
    s = llm.stats
    state["stats"] = {
        "cost_usd": round(s.cost_usd, 4),
        "calls": s.calls,
        "prompt_tokens": s.prompt_tokens,
        "completion_tokens": s.completion_tokens,
        "per_node": {k: {"calls": v["calls"], "cost_usd": round(v["cost_usd"], 4)} for k, v in s.per_node.items()},
        "warnings": state.get("warnings", []) + llm.warnings,
    }
    md = _render_report(state)
    out_dir = CONFIG["report"].get("output_dir", "reports")
    import os

    from .config import ROOT

    d = ROOT / out_dir
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{state['date']}.md"
    path.write_text(md, encoding="utf-8")
    state["report_path"] = str(path)
    return state
