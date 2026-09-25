"""CLI 入口：slowdata run。"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .config import CONFIG


async def amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slowdata", description="慢数据情报 Agent（MVP）")
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="执行一次日度情报流水线")
    p_run.add_argument("--date", default=None, help="报告日期（默认今天）")
    p_run.add_argument("--max-queries", type=int, default=None, help="覆盖检索 query 上限")
    p_imp = sub.add_parser("import-csv", help="导入人工整理线索 CSV（LinkedIn 招聘/岗位动态等，见 config/linkedin_import.example.csv）")
    p_imp.add_argument("path", help="CSV 路径（列：title,url,content,published_date,source,dimensions）")
    p_srv = sub.add_parser("serve", help="启动 Web 看板与定时调度（每日 08:30 自动运行）")
    p_srv.add_argument("--host", default=None, help="监听地址（默认 config.yaml web.host）")
    p_srv.add_argument("--port", type=int, default=None, help="监听端口（默认 config.yaml web.port）")
    p_srv.add_argument("--no-schedule", action="store_true", help="禁用定时任务（调试用）")
    sub.add_parser("stats", help="查看历史运行统计")
    p_db = sub.add_parser("db", help="查看沉淀库数据（items/claims/entities/events）")
    p_db.add_argument("table", nargs="?", default="items", choices=["items", "claims", "entities", "events"])
    p_db.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    if args.cmd == "import-csv":
        return _import_csv(args.path)

    if args.cmd == "serve":
        from .web.app import run_server

        await run_server(host=args.host, port=args.port, no_schedule=args.no_schedule)
        return 0

    if args.cmd == "db":
        return _show_db(args.table, args.limit)

    if args.cmd == "stats":
        from .store import Store

        st = Store()
        rows = st.con.execute(
            "SELECT id, started_at, finished_at, status, stats_json FROM runs ORDER BY started_at DESC LIMIT 10"
        ).fetchall()
        print(f"{'运行ID':<14}{'开始时间':<22}{'状态':<9}{'成本'}")
        for rid, s, f, status, sj in rows:
            import json

            cost = json.loads(sj).get("cost_usd", 0) if sj else 0
            print(f"{rid:<14}{s:<22}{status:<9}${cost:.4f}")
        return 0

    if args.max_queries:
        CONFIG["search"]["max_queries"] = args.max_queries

    from .graph import Pipeline

    p = Pipeline()
    result = await p.run(date=args.date)
    stats = result.get("stats", {})
    print("\n========== 运行完成 ==========")
    print(f"报告: {result.get('report_path')}")
    print(f"query {len(result.get('queries', []))} 条 → 条目 {len(result.get('items', []))} 条 → 簇 {len(result.get('clusters', []))} 个")
    print(f"LLM: {stats.get('calls', 0)} 次调用 · 成本 {stats.get('cost_usd', 0):.4f} USD · 耗时 {result.get('duration_s', 0)}s")
    for w in stats.get("warnings", []):
        print(f"[warn] {w}")
    return 0


def _import_csv(path: str) -> int:
    """导入人工整理线索（LinkedIn 等无法合规自动抓取的渠道）。"""
    import csv

    from .pipeline_nodes import sha1
    from .store import Store

    st = Store()
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            title = (row.get("title") or "").strip()
            url = (row.get("url") or "").strip()
            if not title or not url:
                continue
            st.insert_items(
                [
                    {
                        "title": title,
                        "url": url,
                        "url_hash": sha1(url),
                        "content": (row.get("content") or "")[:4000],
                        "source": (row.get("source") or "linkedin_manual").strip(),
                        "published_date": (row.get("published_date") or "").strip(),
                        "dimensions": [d.strip() for d in (row.get("dimensions") or "talent").split(",") if d.strip()],
                        "importance": 4,
                        "is_new": True,
                    }
                ]
            )
            n += 1
    print(f"已导入 {n} 条人工线索（source 默认 linkedin_manual）")
    return 0


def _show_db(table: str, limit: int) -> int:
    """快速查看沉淀库内容。"""
    from .store import Store

    st = Store()
    queries = {
        "items": ("id, title, source, published_date, importance, is_new", "items ORDER BY id DESC"),
        "claims": ("claim_text, verdict, reason", "claims ORDER BY id DESC"),
        "entities": ("name, type, first_seen, last_seen", "entities ORDER BY id DESC"),
        "events": ("e.event_type, n.name, e.summary, e.happened_at", "events e JOIN entities n ON n.id=e.entity_id ORDER BY e.id DESC"),
    }
    cols, src = queries[table]
    rows = st.con.execute(f"SELECT {cols} FROM {src} LIMIT ?", (limit,)).fetchall()
    if not rows:
        print(f"表 {table} 暂无数据")
        return 0
    for r in rows:
        print(" | ".join(str(x)[:70] for x in r))
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))
