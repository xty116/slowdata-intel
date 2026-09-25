"""Markdown 日报渲染。"""
from __future__ import annotations


def _esc(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def _fmt_cost(c: float) -> str:
    return f"${c:.4f}" if c >= 0.01 else f"${c:.6f}"


def render(state: dict) -> str:
    date = state["date"]
    stats = state.get("stats", {})
    warnings = stats.get("warnings", [])
    clusters = state.get("clusters", [])

    L: list[str] = []
    L.append(f"# 慢数据情报日报 · {date}\n")
    new_cnt = sum(1 for c in clusters if c.get("is_new"))
    L.append(
        f"> 采集 query {len(state.get('queries', []))} 条 · 相关条目 {len(state.get('items', []))} 条 · "
        f"事件簇 {len(clusters)} 个（其中新事件 {new_cnt} 个）\n"
    )

    L.append("\n---\n\n## 情报正文\n")
    L.append(state.get("synthesis", "") or "_（无内容）_")

    L.append("\n\n---\n\n## 数据方针\n")
    L.append(state.get("policy", "") or "_（无内容）_")

    # ---- M2：证据核验与矛盾裁决 ----
    L.append("\n\n---\n\n## 证据核验与矛盾裁决\n")
    verified = [c for c in clusters if c["rep"].get("claims")]
    if verified:
        marks = {"support": "✅ 证实", "contradict": "❌ 冲突", "unverified": "❓ 未证实"}
        for c in verified:
            rep = c["rep"]
            L.append(f"\n**{_esc(rep['title'])[:100]}**  [{rep['url']}]({rep['url']})")
            for x in rep.get("claims", []):
                ev = "、".join(f"[{e['title'][:40]}]({e['url']})" for e in x.get("evidence", [])) or "—"
                L.append(f"- {marks.get(x['verdict'], x['verdict'])} {_esc(x['claim'])[:120]}（{_esc(x.get('reason', ''))[:40]}）证据: {ev}")
            if rep.get("resolution"):
                r = rep["resolution"]
                L.append(f"- ⚖️ 裁决（{r.get('adopt')}，置信度 {r.get('confidence')}）：{_esc(r.get('resolution', ''))}")
    else:
        L.append("_今日无核验记录（无重要度≥4 的新簇）。_")

    # ---- M2：实体事件（今日提取） ----
    today_events = state.get("today_events") or []
    if today_events:
        L.append("\n\n---\n\n## 实体事件（今日提取）\n")
        L.append("| 主体 | 事件类型 | 事件 |")
        L.append("|------|----------|------|")
        for ev in today_events:
            L.append(f"| {_esc(ev.get('entity', ''))} | {_esc(ev.get('event_type', ''))} | {_esc(ev.get('summary', ''))} |")

    # ---- M2：Critic 评审 ----
    L.append("\n\n---\n\n## Critic 评审\n")
    history = state.get("critic_history") or []
    scores = state.get("critic_scores") or {}
    dims = ["factuality", "completeness", "citations", "timeliness", "actionability"]
    dim_names = {"factuality": "事实性", "completeness": "完整性", "citations": "引用可靠性", "timeliness": "时效性", "actionability": "可操作性"}
    if scores:
        L.append(f"最终评分（第 {state.get('critic_round', 1)} 轮）：")
        L.append("| 维度 | 分数 |")
        L.append("|------|------|")
        for d in dims:
            if d in scores:
                L.append(f"| {dim_names.get(d, d)} | {scores[d]} |")
        for h in history:
            if h.get("issues"):
                L.append(f"\n第 {h['round']} 轮问题：")
                for iss in h["issues"]:
                    L.append(f"- [{dim_names.get(iss.get('dimension', ''), iss.get('dimension', ''))}] {_esc(iss.get('issue', ''))}")
        if state.get("critic_failed_accept"):
            L.append("\n> ⚠️ 未达标已降级接受（达到回搜轮次/预算上限），以上评分仅供参考，请人工复核相关结论。")
    else:
        L.append("_无评审记录。_")

    L.append("\n\n---\n\n## 附录：今日采集线索\n")
    if clusters:
        L.append("| # | 标题 | 维度 | 重要度 | 状态 | 核验 | 信源 | 日期 |")
        L.append("|---|------|------|--------|------|------|------|------|")
        for n, c in enumerate(clusters, 1):
            rep = c["rep"]
            dims = "、".join(rep.get("dimensions", []))
            status = "🆕 新" if c.get("is_new") else "复现"
            if rep.get("date_unknown"):
                status += " ⏱️未知"
            claims = rep.get("claims") or []
            if claims:
                ok = sum(1 for x in claims if x["verdict"] == "support")
                ct = sum(1 for x in claims if x["verdict"] == "contradict")
                un = sum(1 for x in claims if x["verdict"] == "unverified")
                verif = f"{ok}✅{ct}❌{un}❓"
            else:
                verif = "—"
            src = rep.get("source", "tavily")
            pd = rep.get("published_date") or rep.get("page_date") or "—"
            title = _esc(rep["title"])[:80]
            L.append(
                f"| {n} | [{title}]({rep['url']}) | {dims} | {rep.get('importance', 3)} | {status} | {verif} | {src} | {pd} |"
            )
    else:
        L.append("_今日无相关线索。_")

    L.append("\n---\n\n## 运行统计\n")
    L.append(f"- LLM 调用 {stats.get('calls', 0)} 次 · 输入 {stats.get('prompt_tokens', 0):,} tokens · "
             f"输出 {stats.get('completion_tokens', 0):,} tokens · 估算成本 {_fmt_cost(stats.get('cost_usd', 0))}")
    if stats.get("per_node"):
        L.append("\n| 节点 | 调用 | 成本 |")
        L.append("|------|------|------|")
        for node, v in stats["per_node"].items():
            L.append(f"| {node} | {v['calls']} | {_fmt_cost(v['cost_usd'])} |")
    if warnings:
        L.append("\n### 运行告警\n")
        for w in warnings:
            L.append(f"- {w}")
    L.append("\n---\n*本报告由「慢数据」情报 Agent 自动生成。重要决策前请依据附录链接人工复核原文。*\n")
    return "\n".join(L)
