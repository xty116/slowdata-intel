"""「慢数据」情报台：FastAPI Web 看板 + APScheduler 定时调度。"""
from __future__ import annotations

import html
import json
from contextlib import asynccontextmanager
from pathlib import Path

import markdown as md
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import CONFIG, ROOT
from ..store import Store
from .runstate import RunManager

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

manager = RunManager()
scheduler = AsyncIOScheduler()
_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def render(name: str, **ctx) -> HTMLResponse:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))
    return HTMLResponse(env.get_template(name).render(**ctx))


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = CONFIG.get("schedule", {})
    if cfg.get("enabled", True):
        daily = str(cfg.get("daily_at", "08:30"))
        hh, mm = daily.split(":")
        scheduler.add_job(
            manager.start,
            CronTrigger(hour=int(hh), minute=int(mm)),
            id="daily",
            name="日度情报流水线",
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        print(f"[web] 定时任务已注册：每日 {daily}（本机时区）")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="慢数据情报台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# 概览
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    st = get_store()
    n_items = st.con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    n_entities = st.con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    n_events = st.con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    n_claims = st.con.execute("SELECT COUNT(*) FROM claims").fetchone()[0]

    month_rows = st.con.execute(
        "SELECT stats_json FROM runs WHERE status='success' AND started_at >= date('now','start of month')"
    ).fetchall()
    month_cost = sum((json.loads(r[0]).get("cost_usd", 0) if r[0] else 0) for r in month_rows)

    latest = st.con.execute(
        "SELECT id, started_at, status, stats_json FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    latest_stats = json.loads(latest[3]) if latest and latest[3] else {}
    per_source = []
    if latest:
        per_source = st.con.execute(
            "SELECT source, COUNT(*), ROUND(AVG(importance),2) FROM items WHERE run_id=? GROUP BY source ORDER BY COUNT(*) DESC",
            (latest[0],),
        ).fetchall()

    recent_runs = st.con.execute(
        "SELECT id, started_at, status, stats_json FROM runs ORDER BY started_at DESC LIMIT 8"
    ).fetchall()
    recent = []
    for rid, s, status, sj in recent_runs:
        cost = json.loads(sj).get("cost_usd", 0) if sj else 0
        recent.append({"id": rid, "at": s, "status": status, "cost": cost})

    reports_dir = ROOT / CONFIG["report"].get("output_dir", "reports")
    reports = sorted((f.stem for f in reports_dir.glob("*.md")), reverse=True) if reports_dir.exists() else []

    return render(
        "dashboard.html",
        n_items=n_items,
        n_entities=n_entities,
        n_events=n_events,
        n_claims=n_claims,
        month_cost=round(month_cost, 4),
        latest=latest,
        latest_stats=latest_stats,
        per_source=per_source,
        recent=recent,
        reports=reports[:10],
        snapshot=manager.snapshot(),
    )


# ---------------------------------------------------------------------------
# 日报
# ---------------------------------------------------------------------------
@app.get("/reports", response_class=HTMLResponse)
def reports_list():
    reports_dir = ROOT / CONFIG["report"].get("output_dir", "reports")
    reports = []
    if reports_dir.exists():
        for f in sorted(reports_dir.glob("*.md"), reverse=True):
            reports.append({"date": f.stem, "size": f.stat().st_size, "mtime": f.stat().st_mtime})
    return render("reports.html", reports=reports)


@app.get("/reports/{date}", response_class=HTMLResponse)
def report_view(date: str):
    p = ROOT / CONFIG["report"].get("output_dir", "reports") / f"{date}.md"
    if not p.exists():
        return HTMLResponse("<main><h2>报告不存在</h2><a href='/reports'>返回列表</a></main>")
    text = p.read_text(encoding="utf-8")
    # 先转义原始 HTML 再走 markdown，防止报告内嵌内容注入脚本
    body = md.markdown(html.escape(text), extensions=["tables", "fenced_code", "sane_lists"])
    return render("report.html", date=date, content=body)


# ---------------------------------------------------------------------------
# 条目检索
# ---------------------------------------------------------------------------
@app.get("/items", response_class=HTMLResponse)
def items(source: str = "", dimension: str = "", min_imp: int = 0, q: str = "", verified: str = "", limit: int = 100):
    st = get_store()
    where, params = ["1=1"], []
    if source:
        where.append("source=?"); params.append(source)
    if dimension:
        where.append("dimensions LIKE ?"); params.append(f'%"{dimension}"%')
    if min_imp:
        where.append("importance>=?"); params.append(int(min_imp))
    if q:
        where.append("title LIKE ?"); params.append(f"%{q}%")
    if verified == "1":
        where.append("url_hash IN (SELECT item_url_hash FROM claims)")
    elif verified == "0":
        where.append("url_hash NOT IN (SELECT item_url_hash FROM claims)")
    sql = (
        "SELECT id, title, url, source, published_date, importance, is_new, dimensions, url_hash "
        f"FROM items WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?"
    )
    params.append(min(int(limit), 200))
    rows = st.con.execute(sql, params).fetchall()

    verdict_map: dict[str, dict[str, int]] = {}
    hashes = [r[8] for r in rows]
    if hashes:
        ph = ",".join("?" * len(hashes))
        for h, v in st.con.execute(f"SELECT item_url_hash, verdict FROM claims WHERE item_url_hash IN ({ph})", hashes):
            m = verdict_map.setdefault(h, {"support": 0, "contradict": 0, "unverified": 0})
            m[v] = m.get(v, 0) + 1

    sources = [r[0] for r in st.con.execute("SELECT DISTINCT source FROM items ORDER BY source")]
    return render(
        "items.html",
        rows=rows,
        verdict_map=verdict_map,
        sources=sources,
        f={"source": source, "dimension": dimension, "min_imp": min_imp, "q": q, "verified": verified},
    )


# ---------------------------------------------------------------------------
# 实体档案
# ---------------------------------------------------------------------------
@app.get("/entities", response_class=HTMLResponse)
def entities(etype: str = "", q: str = ""):
    st = get_store()
    where, params = ["1=1"], []
    if etype:
        where.append("type=?"); params.append(etype)
    if q:
        where.append("name LIKE ?"); params.append(f"%{q}%")
    rows = st.con.execute(
        f"SELECT id, name, type, first_seen, last_seen FROM entities WHERE {' AND '.join(where)} ORDER BY last_seen DESC LIMIT 200",
        params,
    ).fetchall()
    types = [r[0] for r in st.con.execute("SELECT DISTINCT type FROM entities ORDER BY type")]
    return render("entities.html", rows=rows, types=types, f={"etype": etype, "q": q})


@app.get("/entities/{eid}", response_class=HTMLResponse)
def entity_detail(eid: int):
    st = get_store()
    ent = st.con.execute("SELECT id, name, type, first_seen, last_seen FROM entities WHERE id=?", (eid,)).fetchone()
    if not ent:
        return HTMLResponse("<main><h2>实体不存在</h2><a href='/entities'>返回</a></main>")
    events = st.con.execute(
        "SELECT event_type, summary, happened_at, created_at FROM events WHERE entity_id=? ORDER BY id DESC LIMIT 50",
        (eid,),
    ).fetchall()
    items = st.con.execute(
        "SELECT i.id, i.title, i.url, i.published_date, i.importance "
        "FROM item_entities ie JOIN items i ON i.id=ie.item_id "
        "WHERE ie.entity_id=? ORDER BY i.id DESC LIMIT 50",
        (eid,),
    ).fetchall()
    return render("entity_detail.html", ent=ent, events=events, items=items)


# ---------------------------------------------------------------------------
# 关注清单
# ---------------------------------------------------------------------------
def _watchlist_path() -> Path:
    return ROOT / "config" / "watchlist.yaml"


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_view():
    with open(_watchlist_path(), encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return render("watchlist.html", topics=data.get("topics", []), path=str(_watchlist_path()))


@app.post("/watchlist/add")
async def watchlist_add(request: Request):
    form = await request.form()
    topic_name = (form.get("topic") or "").strip()
    query = (form.get("query") or "").strip()
    if topic_name and query:
        with open(_watchlist_path(), encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for t in data.get("topics", []):
            if t.get("name") == topic_name:
                t.setdefault("queries", [])
                if query not in t["queries"]:
                    t["queries"].append(query)
                break
        with open(_watchlist_path(), "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=1000)
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/delete")
async def watchlist_delete(request: Request):
    form = await request.form()
    topic_name = (form.get("topic") or "").strip()
    query = (form.get("query") or "").strip()
    with open(_watchlist_path(), encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    for t in data.get("topics", []):
        if t.get("name") == topic_name:
            t["queries"] = [q for q in t.get("queries", []) if q != query]
    with open(_watchlist_path(), "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=1000)
    return RedirectResponse("/watchlist", status_code=303)


# ---------------------------------------------------------------------------
# 运行历史 + 手动触发
# ---------------------------------------------------------------------------
@app.get("/runs", response_class=HTMLResponse)
def runs_view():
    st = get_store()
    rows = st.con.execute(
        "SELECT id, started_at, finished_at, status, stats_json, error FROM runs ORDER BY started_at DESC LIMIT 30"
    ).fetchall()
    runs = []
    for rid, s, f, status, sj, err in rows:
        stats = json.loads(sj) if sj else {}
        runs.append(
            {
                "id": rid,
                "started": s,
                "finished": f,
                "status": status,
                "cost": stats.get("cost_usd", 0),
                "calls": stats.get("calls", 0),
                "prompt_tokens": stats.get("prompt_tokens", 0),
                "completion_tokens": stats.get("completion_tokens", 0),
                "per_node": stats.get("per_node", {}),
                "warnings": stats.get("warnings", []),
                "error": err,
            }
        )
    return render("runs.html", runs=runs, snapshot=manager.snapshot())


@app.get("/api/runs/status")
def api_status():
    return JSONResponse(manager.snapshot())


@app.post("/api/runs")
async def api_trigger(request: Request):
    form = await request.form()
    mq = (form.get("max_queries") or "").strip()
    started = await manager.start(int(mq) if mq.isdigit() else None)
    return JSONResponse({"started": started, **manager.snapshot()})


async def run_server(host: str | None = None, port: int | None = None, no_schedule: bool = False):
    import uvicorn

    cfg = CONFIG.get("web", {})
    if no_schedule:
        CONFIG["schedule"] = {"enabled": False}
    config = uvicorn.Config(
        app,
        host=host or cfg.get("host", "127.0.0.1"),
        port=port or int(cfg.get("port", 8000)),
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()
