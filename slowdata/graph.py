"""LangGraph 编排：日度流水线 DAG；langgraph 不可用时线性降级运行。"""
from __future__ import annotations

import datetime
import time
import uuid
from functools import partial
from typing import TypedDict

from . import pipeline_nodes as N
from .llm import LLM
from .store import Store


class DailyState(TypedDict, total=False):
    run_id: str
    date: str
    queries: list[str]
    items: list[dict]
    clusters: list[dict]
    synthesis: str
    policy: str
    report_path: str
    persisted: int
    stats: dict
    warnings: list[str]
    # M2：核验 / Critic 回路
    critic_round: int
    critic_pass: bool
    critic_failed_accept: bool
    critic_scores: dict
    critic_history: list
    re_search_queries: list[str]
    today_events: list


_NODE_ORDER = [
    ("generate_queries", N.node_generate_queries),
    ("collect", N.node_collect),
    ("triage", N.node_triage),
    ("fetch_content", N.node_fetch_content),
    ("dedup", N.node_dedup),
    ("summarize", N.node_summarize),
    ("persist", N.node_persist),
    ("verify", N.node_verify),
    ("events", N.node_events),
    ("synthesize", N.node_synthesize),
    ("policy", N.node_policy),
    ("critic", N.node_critic),
    ("render_report", N.node_render_report),
]
# Critic 未达标时插入回搜节点（research → dedup → summarize → … → critic 回路）
_NODE_WITH_STORE = {"dedup", "persist", "verify", "events"}


class Pipeline:
    def __init__(self) -> None:
        self.llm = LLM()
        self.store = Store()

    def _new_state(self, date: str | None) -> DailyState:
        return DailyState(
            run_id=uuid.uuid4().hex[:12],
            date=date or datetime.date.today().isoformat(),
            queries=[],
            items=[],
            clusters=[],
            synthesis="",
            policy="",
            report_path="",
            persisted=0,
            stats={},
            warnings=[],
            critic_round=0,
            critic_pass=False,
            critic_failed_accept=False,
            critic_scores={},
            critic_history=[],
            re_search_queries=[],
            today_events=[],
        )

    def _build_graph(self):
        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError:
            return None
        g = StateGraph(DailyState)
        # 注意：必须用 partial 绑定，lambda 会被 LangGraph 当作同步节点、不 await 协程
        g.add_node("generate_queries", partial(N.node_generate_queries, llm=self.llm))
        g.add_node("collect", partial(N.node_collect, llm=self.llm))
        g.add_node("triage", partial(N.node_triage, llm=self.llm))
        g.add_node("fetch_content", partial(N.node_fetch_content, llm=self.llm))
        g.add_node("dedup", partial(N.node_dedup, llm=self.llm, store=self.store))
        g.add_node("summarize", partial(N.node_summarize, llm=self.llm))
        g.add_node("persist", partial(N.node_persist, llm=self.llm, store=self.store))
        g.add_node("verify", partial(N.node_verify, llm=self.llm, store=self.store))
        g.add_node("events", partial(N.node_events, llm=self.llm, store=self.store))
        g.add_node("synthesize", partial(N.node_synthesize, llm=self.llm))
        g.add_node("policy", partial(N.node_policy, llm=self.llm))
        g.add_node("critic", partial(N.node_critic, llm=self.llm))
        g.add_node("research", partial(N.node_research, llm=self.llm))
        g.add_node("render_report", partial(N.node_render_report, llm=self.llm))
        g.add_edge(START, "generate_queries")
        linear = [n for n, _ in _NODE_ORDER]
        for a, b in zip(linear, linear[1:]):
            if a == "critic":
                continue  # critic 之后由条件边路由，避免双路径写冲突
            g.add_edge(a, b)
        # Critic 条件回路：通过/达上限 → 渲染；未达标 → 回搜 → 去重 → … → 再评审
        def _after_critic(state: DailyState) -> str:
            return "render_report" if state.get("critic_pass") else "research"

        g.add_conditional_edges("critic", _after_critic, {"render_report": "render_report", "research": "research"})
        g.add_edge("research", "dedup")
        g.add_edge("render_report", END)
        return g.compile()

    async def _run_linear(self, state: DailyState) -> DailyState:
        for name, fn in _NODE_ORDER:
            t0 = time.time()
            if asyncio.iscoroutinefunction(fn):
                state = (
                    await fn(state, self.llm, self.store)
                    if name in _NODE_WITH_STORE
                    else await fn(state, self.llm)
                )
            else:
                state = (
                    fn(state, self.llm, self.store)
                    if name in _NODE_WITH_STORE
                    else fn(state, self.llm)
                )
            print(f"[node] {name} 完成（{time.time() - t0:.1f}s）")
            # 线性模式下的 Critic 回路
            if name == "critic" and not state.get("critic_pass"):
                for loop_name, loop_fn in (
                    ("research", N.node_research),
                    ("dedup", N.node_dedup),
                    ("summarize", N.node_summarize),
                    ("persist", N.node_persist),
                    ("verify", N.node_verify),
                    ("events", N.node_events),
                    ("synthesize", N.node_synthesize),
                    ("policy", N.node_policy),
                    ("critic", N.node_critic),
                ):
                    t1 = time.time()
                    if asyncio.iscoroutinefunction(loop_fn):
                        state = (
                            await loop_fn(state, self.llm, self.store)
                            if loop_name in _NODE_WITH_STORE
                            else await loop_fn(state, self.llm)
                        )
                    else:
                        state = (
                            loop_fn(state, self.llm, self.store)
                            if loop_name in _NODE_WITH_STORE
                            else loop_fn(state, self.llm)
                        )
                    print(f"[node] {loop_name} 完成（{time.time() - t1:.1f}s）")
                    if loop_name == "critic" and state.get("critic_pass"):
                        break
        return state

    async def run(self, date: str | None = None) -> DailyState:
        state = self._new_state(date)
        self.store.save_run(state["run_id"], "running")
        started = time.time()
        try:
            graph = self._build_graph()
            if graph is not None:
                print(f"[graph] LangGraph 日度流水线启动（{state['date']}）")
                result = await graph.ainvoke(state)
            else:
                print("[graph] langgraph 不可用，线性降级运行")
                result = await self._run_linear(state)
            result["stats"] = self._stats(result)
            result["duration_s"] = round(time.time() - started, 1)
            self.store.save_run(state["run_id"], "success", result["stats"])
            return result
        except Exception as e:  # noqa: BLE001
            self.store.save_run(state["run_id"], "failed", error=str(e)[:2000])
            raise
        finally:
            await self.llm.aclose()

    def _stats(self, result: DailyState) -> dict:
        s = self.llm.stats
        return {
            "cost_usd": round(s.cost_usd, 4),
            "calls": s.calls,
            "prompt_tokens": s.prompt_tokens,
            "completion_tokens": s.completion_tokens,
            "per_node": {k: {"calls": v["calls"], "cost_usd": round(v["cost_usd"], 4)} for k, v in s.per_node.items()},
            "warnings": result.get("warnings", []) + self.llm.warnings,
            "critic_scores": result.get("critic_scores", {}),
            "critic_round": result.get("critic_round", 0),
            "critic_failed_accept": bool(result.get("critic_failed_accept")),
            "duration_s": result.get("duration_s"),
        }
