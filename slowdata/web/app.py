"""「慢数据」情报台：FastAPI Web 看板 + APScheduler 定时调度。"""
from __future__ import annotations

import html
import json
import os
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
        tz = (os.environ.get("SLOWDATA_TZ") or "").strip() or cfg.get("timezone") or None
        scheduler.add_job(
            manager.start,
            CronTrigger(hour=int(hh), minute=int(mm), timezone=tz),
            id="daily",
            name="日度情报流水线",
            max_instances=1,
            coalesce=True,
        )
        # 云端首次预热：库为空时启动 90 秒后自动跑一次（免费档文件系统会在重新部署时清空，
        # 该机制让看板"上线即有数据"；只触发一次，不增加长期成本）
        if os.environ.get("SLOWDATA_WARMUP", "").strip() == "1":
            from datetime import datetime, timedelta

            from apscheduler.triggers.date import DateTrigger

            n_items = get_store().con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            if n_items == 0:
                scheduler.add_job(
                    manager.start,
                    DateTrigger(run_date=datetime.now() + timedelta(seconds=90)),
                    id="warmup",
                    name="首次预热运行",
                    max_instances=1,
                )
                print("[web] 云端首次预热：检测到空库，90 秒后自动执行一次流水线")
        scheduler.start()
        print(f"[web] 定时任务已注册：每日 {daily}（时区 {tz or '本机'}）")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="慢数据情报台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 公网访问口令（DASHBOARD_TOKEN 环境变量；为空则开放访问）
PUBLIC_TOKEN = (os.environ.get("DASHBOARD_TOKEN") or "").strip()
# 公开只读模式（SLOWDATA_READONLY=1）：禁止触发运行与修改关注清单
READONLY = os.environ.get("SLOWDATA_READONLY", "").strip() == "1"
READONLY_POST_PATHS = {"/api/runs", "/watchlist/add", "/watchlist/delete"}


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    if PUBLIC_TOKEN:
        path = request.url.path
        if path.startswith("/static") or path in ("/login",):
            return await call_next(request)
        if request.cookies.get("slowdata_token") != PUBLIC_TOKEN:
            if path.startswith("/api"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
    if READONLY and request.method == "POST" and request.url.path in READONLY_POST_PATHS:
        return JSONResponse({"error": "readonly mode"}, status_code=403)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return render("login.html")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    if (form.get("token") or "").strip() == PUBLIC_TOKEN:
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("slowdata_token", PUBLIC_TOKEN, httponly=True, max_age=30 * 24 * 3600)
        return resp
    return render("login.html", error="口令错误，请重试")


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
    return render("watchlist.html", topics=data.get("topics", []), path=str(_watchlist_path()), readonly=READONLY)


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
    return JSONResponse({**manager.snapshot(), "readonly": READONLY})


@app.post("/api/runs")
async def api_trigger(request: Request):
    form = await request.form()
    mq = (form.get("max_queries") or "").strip()
    started = await manager.start(int(mq) if mq.isdigit() else None)
    return JSONResponse({"started": started, **manager.snapshot()})


# ---------------------------------------------------------------------------
# 看板数据 API（供前端 ECharts / SPA 使用）
# ---------------------------------------------------------------------------
@app.get("/api/overview")
def api_overview():
    st = get_store()
    totals = {
        "items": st.con.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "entities": st.con.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
        "events": st.con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "claims": st.con.execute("SELECT COUNT(*) FROM claims").fetchone()[0],
    }
    month_rows = st.con.execute(
        "SELECT stats_json FROM runs WHERE status='success' AND started_at >= date('now','start of month')"
    ).fetchall()
    month_cost = round(sum((json.loads(r[0]).get("cost_usd", 0) if r[0] else 0) for r in month_rows), 4)

    latest = st.con.execute(
        "SELECT id, started_at, status, stats_json FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    latest_stats = json.loads(latest[3]) if latest and latest[3] else {}
    per_source = []
    if latest:
        per_source = [
            [s, c, a]
            for s, c, a in st.con.execute(
                "SELECT source, COUNT(*), ROUND(AVG(importance),2) FROM items WHERE run_id=? GROUP BY source ORDER BY COUNT(*) DESC",
                (latest[0],),
            ).fetchall()
        ]

    rows = st.con.execute(
        "SELECT substr(started_at,1,10) d, stats_json FROM runs WHERE status='success' AND started_at >= date('now','-13 days')"
    ).fetchall()
    by_day: dict[str, float] = {}
    for d, sj in rows:
        cost = json.loads(sj).get("cost_usd", 0) if sj else 0
        by_day[d] = by_day.get(d, 0) + round(cost, 4)
    days = sorted(by_day)
    cost_series = {"dates": days, "costs": [by_day[d] for d in days]}

    recent_runs = st.con.execute(
        "SELECT id, started_at, status, stats_json FROM runs ORDER BY started_at DESC LIMIT 8"
    ).fetchall()
    recent = []
    for rid, s, status, sj in recent_runs:
        stats = json.loads(sj) if sj else {}
        recent.append(
            {
                "id": rid,
                "at": s,
                "status": status,
                "cost": stats.get("cost_usd", 0),
                "calls": stats.get("calls", 0),
                "critic_scores": stats.get("critic_scores", {}),
            }
        )

    reports_dir = ROOT / CONFIG["report"].get("output_dir", "reports")
    reports = sorted((f.stem for f in reports_dir.glob("*.md")), reverse=True) if reports_dir.exists() else []

    return JSONResponse(
        {
            "totals": totals,
            "month_cost": month_cost,
            "latest_run": {
                "id": latest[0] if latest else None,
                "at": latest[1] if latest else None,
                "status": latest[2] if latest else None,
                **{k: latest_stats.get(k) for k in ("cost_usd", "calls", "critic_scores", "critic_round", "critic_failed_accept")},
            },
            "per_source": per_source,
            "cost_series": cost_series,
            "recent_runs": recent,
            "reports": reports[:10],
            "snapshot": {**manager.snapshot(), "readonly": READONLY},
        }
    )


@app.get("/api/items")
def api_items(
    source: str = "", dimension: str = "", min_imp: int = 0, q: str = "", verified: str = "", offset: int = 0, limit: int = 50
):
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
    cond = " AND ".join(where)
    total = st.con.execute(f"SELECT COUNT(*) FROM items WHERE {cond}", params).fetchone()[0]
    rows = st.con.execute(
        f"SELECT id, title, url, source, published_date, importance, is_new, dimensions, url_hash "
        f"FROM items WHERE {cond} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [min(int(limit), 100), int(offset)],
    ).fetchall()
    hashes = [r[8] for r in rows]
    verdict_map: dict[str, dict[str, int]] = {}
    if hashes:
        ph = ",".join("?" * len(hashes))
        for h, v in st.con.execute(f"SELECT item_url_hash, verdict FROM claims WHERE item_url_hash IN ({ph})", hashes):
            m = verdict_map.setdefault(h, {"support": 0, "contradict": 0, "unverified": 0})
            m[v] = m.get(v, 0) + 1
    return JSONResponse(
        {
            "total": total,
            "rows": [
                {
                    "id": r[0], "title": r[1], "url": r[2], "source": r[3], "date": r[4] or "",
                    "importance": r[5], "is_new": bool(r[6]), "dims": r[7],
                    "verdicts": verdict_map.get(r[8]),
                }
                for r in rows
            ],
        }
    )


@app.get("/api/entity/{eid}/timeline")
def api_entity_timeline(eid: int):
    st = get_store()
    events = st.con.execute(
        "SELECT event_type, summary, happened_at, created_at FROM events WHERE entity_id=? ORDER BY id DESC LIMIT 200",
        (eid,),
    ).fetchall()
    by_day: dict[str, int] = {}
    detail = []
    for et, sm, ha, ca in events:
        d = (ha or ca or "")[:10]
        if d:
            by_day[d] = by_day.get(d, 0) + 1
        detail.append({"type": et, "summary": sm, "date": d})
    days = sorted(by_day)
    return JSONResponse({"days": days, "counts": [by_day[d] for d in days], "detail": detail})


@app.get("/api/entities-list")
def api_entities_list(etype: str = "", q: str = "", limit: int = 200):
    st = get_store()
    where, params = ["1=1"], []
    if etype:
        where.append("type=?"); params.append(etype)
    if q:
        where.append("name LIKE ?"); params.append(f"%{q}%")
    rows = st.con.execute(
        f"SELECT id, name, type, first_seen, last_seen FROM entities WHERE {' AND '.join(where)} ORDER BY last_seen DESC LIMIT ?",
        params + [min(int(limit), 500)],
    ).fetchall()
    types = [r[0] for r in st.con.execute("SELECT DISTINCT type FROM entities ORDER BY type")]
    return JSONResponse(
        {
            "rows": [{"id": r[0], "name": r[1], "type": r[2], "first_seen": r[3], "last_seen": r[4]} for r in rows],
            "types": types,
        }
    )


@app.get("/api/runs/latest")
def api_runs_latest():
    st = get_store()
    row = st.con.execute(
        "SELECT stats_json FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return JSONResponse(json.loads(row[0]) if row and row[0] else {})


async def run_server(host: str | None = None, port: int | None = None, no_schedule: bool = False):
    import os
    import subprocess
    import sys

    import uvicorn

    cfg = CONFIG.get("web", {})
    if no_schedule:
        CONFIG["schedule"] = {"enabled": False}
    host = host or cfg.get("host", "127.0.0.1")
    # 云平台（Render 等）要求监听其分配的 PORT 环境变量
    port = int(os.environ.get("PORT") or (port or cfg.get("port", 8000)))
    if sys.platform == "darwin" and not os.environ.get("SLOWDATA_NO_OPEN"):
        subprocess.Popen(["open", f"http://{host}:{port}"])
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
