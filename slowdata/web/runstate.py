"""运行管理器：手动触发与定时任务共用的单实例运行锁。"""
from __future__ import annotations

import asyncio
import time

from ..config import CONFIG


class RunManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.status = "idle"  # idle | running | done | failed
        self.run_id: str | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.error: str | None = None
        self.summary: dict | None = None

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "summary": self.summary,
        }

    async def start(self, max_queries: int | None = None) -> bool:
        """触发一次后台运行；已有运行进行中则拒绝。返回是否启动。"""
        async with self._lock:
            if self.status == "running":
                return False
            self.status = "running"
            self.run_id = None
            self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self.finished_at = None
            self.error = None
            self.summary = None
        asyncio.create_task(self._run(max_queries))
        return True

    async def _run(self, max_queries: int | None) -> None:
        from ..graph import Pipeline

        if max_queries:
            CONFIG["search"]["max_queries"] = int(max_queries)
        try:
            p = Pipeline()
            result = await p.run()
            stats = result.get("stats") or {}
            self.run_id = result.get("run_id")
            self.summary = {
                "date": result.get("date"),
                "queries": len(result.get("queries", [])),
                "items": len(result.get("items", [])),
                "clusters": len(result.get("clusters", [])),
                "cost_usd": stats.get("cost_usd", 0),
                "calls": stats.get("calls", 0),
                "report_path": result.get("report_path", ""),
                "critic_scores": result.get("critic_scores", {}),
                "warnings": stats.get("warnings", []),
            }
            self.status = "done"
        except Exception as e:  # noqa: BLE001
            self.status = "failed"
            self.error = str(e)[:2000]
        finally:
            self.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
