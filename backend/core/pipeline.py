"""
core/pipeline.py  v3 — SEQUENTIAL (fixes LangGraph fan-in state write error)

The fan-out/fan-in pattern (4 parallel agents → reflection) caused:
  "Must write to at least one of [repo_url, repo_path, ...]"
because older LangGraph versions are strict about which keys each node
can write during a parallel fan-in merge.

Fix: run all agents sequentially in a single node. Same result, zero
LangGraph version compatibility issues.
"""
import os
import shutil
import threading

from langgraph.graph import StateGraph, END

from backend.core.ingestion import ingest_repo
from backend.core.state import ReviewState
from backend.core.memory import (
    get_high_risk_query_hints,
    get_similar_repo_insights,
    store_review_patterns,
)
from backend.core.reflection import detect_vulnerability_chains
from backend.agents.bug_agent import run_bug_agent
from backend.agents.security_agent import run_security_agent
from backend.agents.quality_agent import run_quality_agent
from backend.agents.perf_agent import run_perf_agent
from backend.agents.synthesizer import run_synthesizer
from rich.console import Console

console = Console()

# Thread-safe collection store (collections can't live in LangGraph state)
_active_collections: dict = {}
_collections_lock = threading.Lock()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _state_to_ns(state: dict):
    """Dict → simple namespace so agent code can use state.foo syntax."""
    class NS:
        pass
    ns = NS()
    for k, v in state.items():
        setattr(ns, k, v)
    return ns


def _get_col(name: str):
    with _collections_lock:
        return _active_collections.get(name)


def _set_col(name: str, col):
    with _collections_lock:
        _active_collections[name] = col


def _del_col(name: str):
    with _collections_lock:
        _active_collections.pop(name, None)


# ── Node 1: Ingest ─────────────────────────────────────────────────────────────

def _ingest_node(state: dict) -> dict:
    console.log("[bold cyan]PIPELINE[/bold cyan] Cloning & ingesting repo...")
    collection, chunks, repo_path = ingest_repo(state["repo_url"])
    _set_col(collection.name, collection)
    console.log(f"[green]INGEST[/green] {len(chunks)} chunks embedded")
    return {
        "repo_path":       repo_path,
        "collection_name": collection.name,
        "all_chunks":      chunks,
        "status":          "analyzing",
        # initialise finding lists so downstream nodes always see lists
        "bug_findings":      [],
        "security_findings": [],
        "quality_findings":  [],
        "perf_findings":     [],
    }


# ── Node 2: All agents (sequential) ───────────────────────────────────────────

def _agents_node(state: dict) -> dict:
    """
    Runs all 4 agents one after another inside a single LangGraph node.
    Sequential execution avoids all fan-in state-merge issues entirely.
    """
    collection = _get_col(state["collection_name"])
    extra      = get_high_risk_query_hints(state["repo_url"])

    # ── Bug agent ──────────────────────────────────────────────────────────────
    console.log("[bold yellow]AGENT[/bold yellow] Bug (HyDE + rerank + reflect)...")
    if collection and collection.count() > 0:
        ns = _state_to_ns(state)
        ns.bug_findings = []
        ns = run_bug_agent(ns, collection, extra_queries=extra)
        bug_findings = ns.bug_findings
    else:
        bug_findings = []

    # ── Security agent ─────────────────────────────────────────────────────────
    console.log("[bold red]AGENT[/bold red] Security (HyDE + rerank + reflect)...")
    if collection and collection.count() > 0:
        ns = _state_to_ns(state)
        ns.security_findings = []
        ns = run_security_agent(ns, collection, extra_queries=extra)
        security_findings = ns.security_findings
    else:
        security_findings = []

    # ── Quality agent ──────────────────────────────────────────────────────────
    console.log("[bold blue]AGENT[/bold blue] Quality (HyDE + rerank + reflect)...")
    if collection and collection.count() > 0:
        ns = _state_to_ns(state)
        ns.quality_findings = []
        ns = run_quality_agent(ns, collection, extra_queries=extra)
        quality_findings = ns.quality_findings
    else:
        quality_findings = []

    # ── Perf agent ─────────────────────────────────────────────────────────────
    console.log("[bold green]AGENT[/bold green] Performance (HyDE + rerank + reflect)...")
    if collection and collection.count() > 0:
        ns = _state_to_ns(state)
        ns.perf_findings = []
        ns = run_perf_agent(ns, collection, extra_queries=extra)
        perf_findings = ns.perf_findings
    else:
        perf_findings = []

    # ── Reflection: multi-hop chain detection ──────────────────────────────────
    console.log("[bold magenta]REFLECT[/bold magenta] Multi-hop chain detection...")
    all_findings = bug_findings + security_findings + quality_findings + perf_findings
    chains = detect_vulnerability_chains(all_findings)
    if chains:
        console.log(f"[bold red]CHAINS[/bold red] {len(chains)} chain(s) found")
        security_findings = security_findings + chains

    return {
        "bug_findings":      bug_findings,
        "security_findings": security_findings,
        "quality_findings":  quality_findings,
        "perf_findings":     perf_findings,
        "status":            "synthesizing",
    }


# ── Node 3: Synthesize ─────────────────────────────────────────────────────────

def _synthesize_node(state: dict) -> dict:
    console.log("[bold magenta]PIPELINE[/bold magenta] Synthesizing report...")
    ns             = _state_to_ns(state)
    memory_insights = get_similar_repo_insights(state["repo_url"])
    result         = run_synthesizer(ns, memory_insights=memory_insights)

    all_findings = (
        state.get("bug_findings",      []) +
        state.get("security_findings", []) +
        state.get("quality_findings",  []) +
        state.get("perf_findings",     [])
    )
    store_review_patterns(state["repo_url"], all_findings, result.score)

    # Cleanup ChromaDB collection
    _del_col(state.get("collection_name", ""))

    # Cleanup cloned temp directory
    repo_path = state.get("repo_path", "")
    if repo_path and os.path.exists(repo_path):
        try:
            shutil.rmtree(repo_path, ignore_errors=True)
        except Exception:
            pass

    return {
        "summary":         result.summary,
        "score":           result.score,
        "report_markdown": result.report_markdown,
        "status":          "done",
    }


# ── Graph ──────────────────────────────────────────────────────────────────────

def build_graph():
    graph = StateGraph(ReviewState)

    graph.add_node("ingest",     _ingest_node)
    graph.add_node("agents",     _agents_node)
    graph.add_node("synthesize", _synthesize_node)

    graph.set_entry_point("ingest")
    graph.add_edge("ingest",     "agents")
    graph.add_edge("agents",     "synthesize")
    graph.add_edge("synthesize", END)

    return graph.compile()


# ── Public entry points ────────────────────────────────────────────────────────

def run_review_sync(repo_url: str) -> dict:
    """Synchronous entry point — called by main.py background thread and CLI."""
    initial_state = {
        "repo_url":          repo_url,
        "repo_path":         "",
        "collection_name":   "",
        "all_chunks":        [],
        "bug_findings":      [],
        "security_findings": [],
        "quality_findings":  [],
        "perf_findings":     [],
        "summary":           "",
        "score":             0,
        "report_markdown":   "",
        "status":            "pending",
        "error":             None,
    }
    return build_graph().invoke(initial_state)


async def run_review(repo_url: str) -> dict:
    """Async entry point — wraps run_review_sync in a thread executor."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, run_review_sync, repo_url)