"""SQLite 沉淀层：items / runs。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import numpy as np

from .config import CONFIG, ROOT

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    content TEXT DEFAULT '',
    url TEXT NOT NULL,
    source TEXT DEFAULT 'tavily',
    published_date TEXT DEFAULT '',
    fetched_at TEXT NOT NULL,
    dimensions TEXT DEFAULT '[]',
    importance INTEGER DEFAULT 3,
    cluster_id INTEGER,
    is_new INTEGER DEFAULT 1,
    summary TEXT DEFAULT '',
    entities TEXT DEFAULT '[]',
    embedding BLOB,
    run_id TEXT,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_fetched ON items(fetched_at);
CREATE INDEX IF NOT EXISTS idx_items_importance ON items(importance);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    stats_json TEXT DEFAULT '{}',
    error TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    item_url_hash TEXT,
    claim_text TEXT,
    verdict TEXT,
    reason TEXT DEFAULT '',
    evidence_json TEXT DEFAULT '[]',
    verdict_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_hash ON claims(item_url_hash);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT,
    name TEXT UNIQUE,
    first_seen TEXT,
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS item_entities (
    item_id INTEGER,
    entity_id INTEGER,
    PRIMARY KEY (item_id, entity_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    entity_id INTEGER,
    event_type TEXT,
    summary TEXT,
    evidence_json TEXT DEFAULT '[]',
    happened_at TEXT DEFAULT '',
    confidence REAL DEFAULT 1.0,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id);
"""


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        path = Path(path or CONFIG["report"].get("db_path", "data/slowdata.db"))
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), check_same_thread=False)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript(_SCHEMA)
        self.con.commit()

    def insert_items(self, items: list[dict]) -> dict[str, tuple[int, bool]]:
        """插入/更新条目（同 URL 再次出现时合并更丰富的信息）；返回 url_hash -> (id, is_new)。"""
        out: dict[str, tuple[int, bool]] = {}
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for it in items:
            h = it["url_hash"]
            emb = it.get("embedding")
            blob = np.asarray(emb, dtype=np.float32).tobytes() if emb is not None else None
            row = self.con.execute("SELECT id FROM items WHERE url_hash=?", (h,)).fetchone()
            if row is None:
                cur = self.con.execute(
                    """
                    INSERT INTO items
                    (url_hash, title, content, url, source, published_date, fetched_at,
                     dimensions, importance, cluster_id, is_new, summary, entities, embedding, run_id, raw_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        h,
                        it["title"][:1000],
                        it.get("content", ""),
                        it["url"],
                        it.get("source", "tavily"),
                        it.get("published_date", ""),
                        now,
                        json.dumps(it.get("dimensions", []), ensure_ascii=False),
                        int(it.get("importance", 3)),
                        it.get("cluster_id"),
                        1,
                        it.get("summary", ""),
                        json.dumps(it.get("entities", []), ensure_ascii=False),
                        blob,
                        it.get("run_id", ""),
                        json.dumps(it.get("raw", {}), ensure_ascii=False),
                    ),
                )
                out[h] = (cur.lastrowid, True)
            else:
                # 已存在：合并更丰富的信息（正文/页面抓取/日期补全/更高重要度）
                self.con.execute(
                    """
                    UPDATE items SET
                      title=:title,
                      content=CASE WHEN :content != '' THEN :content ELSE content END,
                      published_date=CASE WHEN :pd != '' THEN :pd ELSE published_date END,
                      importance=MAX(importance, :importance),
                      summary=CASE WHEN :summary != '' THEN :summary ELSE summary END,
                      entities=CASE WHEN :entities != '[]' THEN :entities ELSE entities END,
                      embedding=COALESCE(:embedding, embedding),
                      raw_json=CASE WHEN :raw != '{}' THEN :raw ELSE raw_json END,
                      fetched_at=:now,
                      run_id=:run_id
                    WHERE url_hash=:h
                    """,
                    {
                        "title": it["title"][:1000],
                        "content": it.get("content", ""),
                        "pd": it.get("published_date", ""),
                        "importance": int(it.get("importance", 3)),
                        "summary": it.get("summary", ""),
                        "entities": json.dumps(it.get("entities", []), ensure_ascii=False),
                        "embedding": blob,
                        "raw": json.dumps(it.get("raw", {}), ensure_ascii=False),
                        "now": now,
                        "run_id": it.get("run_id", ""),
                        "h": h,
                    },
                )
                out[h] = (row[0], False)
        self.con.commit()
        return out

    def recent_embeddings(self, days: int = 7, limit: int = 3000) -> list[tuple[int, np.ndarray]]:
        rows = self.con.execute(
            "SELECT id, embedding FROM items WHERE embedding IS NOT NULL "
            "AND fetched_at >= datetime('now', ?) ORDER BY id DESC LIMIT ?",
            (f"-{days} days", limit),
        ).fetchall()
        return [(i, np.frombuffer(b, dtype=np.float32)) for i, b in rows if b]

    def save_run(self, run_id: str, status: str, stats: dict | None = None, error: str = "") -> None:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        self.con.execute(
            """
            INSERT INTO runs (id, started_at, finished_at, status, stats_json, error)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET finished_at=excluded.finished_at,
                status=excluded.status, stats_json=excluded.stats_json, error=excluded.error
            """,
            (run_id, now, now, status, json.dumps(stats or {}, ensure_ascii=False), error),
        )
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    # ---------------- M2：核验 / 实体 / 事件 ----------------

    def item_id_by_hash(self, url_hash: str) -> int | None:
        row = self.con.execute("SELECT id FROM items WHERE url_hash=?", (url_hash,)).fetchone()
        return row[0] if row else None

    def save_claim(
        self,
        run_id: str,
        url_hash: str,
        claim: str,
        verdict: str,
        reason: str = "",
        evidence: list | None = None,
    ) -> None:
        self.con.execute(
            "INSERT INTO claims (run_id, item_url_hash, claim_text, verdict, reason, evidence_json, verdict_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                url_hash,
                claim,
                verdict,
                reason,
                json.dumps(evidence or [], ensure_ascii=False),
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        self.con.commit()

    def upsert_entity(self, name: str, etype: str) -> int:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        name = (name or "").strip()[:200]
        row = self.con.execute("SELECT id FROM entities WHERE name=?", (name,)).fetchone()
        if row:
            self.con.execute("UPDATE entities SET last_seen=?, type=? WHERE id=?", (now, etype, row[0]))
            return row[0]
        cur = self.con.execute(
            "INSERT INTO entities (type, name, first_seen, last_seen) VALUES (?,?,?,?)",
            (etype, name, now, now),
        )
        self.con.commit()
        return cur.lastrowid

    def link_item_entity(self, item_id: int, entity_id: int) -> None:
        self.con.execute(
            "INSERT OR IGNORE INTO item_entities (item_id, entity_id) VALUES (?,?)", (item_id, entity_id)
        )

    def save_event(
        self,
        run_id: str,
        entity_id: int,
        event_type: str,
        summary: str,
        evidence: list | None = None,
        happened_at: str = "",
        confidence: float = 1.0,
    ) -> None:
        self.con.execute(
            "INSERT INTO events (run_id, entity_id, event_type, summary, evidence_json, happened_at, confidence, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                entity_id,
                event_type,
                summary,
                json.dumps(evidence or [], ensure_ascii=False),
                happened_at,
                confidence,
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        self.con.commit()

    def recent_events(self, days: int = 7, limit: int = 50) -> list[tuple]:
        return self.con.execute(
            "SELECT e.event_type, n.name, e.summary, e.happened_at, e.created_at "
            "FROM events e JOIN entities n ON n.id = e.entity_id "
            "WHERE e.created_at >= datetime('now', ?) ORDER BY e.created_at DESC LIMIT ?",
            (f"-{days} days", limit),
        ).fetchall()
