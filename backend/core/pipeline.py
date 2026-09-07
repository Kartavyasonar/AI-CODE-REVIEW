"""
core/pipeline.py  v5 — PARALLEL AGENTS + PROPER CLEANUP
Fixes: Added chroma_temp_dir cleanup to prevent disk space leaks.
"""
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeout

from langgraph.graph import StateGraph, END

from backend.core.ingestion import ingest_repo
from backend.core.state import ReviewState
from backend.core.memory import get_high_risk_query_hints, get_similar_repo_insights, store_review_patterns
from backend.core.reflection import detect_vulnerability_chains
from backend.agents.bug_agent import run_bug_agent
from backend.agents.security_agent import run_security_agent
from backend.agents.quality_agent import run_quality_agent
from backend.agents.perf_agent import run_perf_agent
from backend.agents.synthesizer import run_synthesizer
from rich.console import Console

console = Console()

_active_collections: dict = {}
_collections_lock = threading.Lock()
AGENT_TIMEOUT = 120

def _state_to_ns(state: dict):
    class NS: pass
    ns = NS()
    for k, v in state.items(): setattr(ns, k, v)
    return ns

def _get_col(name: str):
    with _collections_lock: return _active_collections.get(name)

def _set_col(name: str, col):
    with _collections_lock: _active_collections[name] = col

def _del_col(name: str):
    with _collections_lock: _active_collections.pop(name, None)

def _cleanup(state: dict):
    _del_col(state.get("collection_name", ""))
    for dir_key in ("repo_path", "chroma_temp_dir"):
        dir_path = state.get(dir_key, "")
        if dir_path and os.path.exists(dir_path):
            try: shutil.rmtree(dir_path, ignore_errors=True)
            except Exception: pass

def _ingest_node(state: dict) -> dict:
    console.log("[bold cyan]PIPELINE[/bold cyan] Cloning & ingesting repo...")
    collection, chunks, repo_path, chroma_temp_dir = ingest_repo(state["repo_url"])
    _set_col(collection.name, collection)
    console.log(f"[green]INGEST[/green] {len(chunks)} chunks embedded")
    return {
        "repo_path": repo_path, "chroma_temp_dir": chroma_temp_dir,
        "collection_name": collection.name, "all_chunks": chunks, "status": "analyzing",
        "bug_findings": [], "security_findings": [], "quality_findings": [], "perf_findings": [],
    }

def _run_agent_safe(name: str, fn, state: dict, collection, extra: list) -> list:
    try:
        ns = _state_to_ns(state)
        setattr(ns, f"{name}_findings", [])
        result = fn(ns, collection, extra_queries=extra)
        return getattr(result, f"{name}_findings", [])
    except Exception as e:
        console.log(f"[red]AGENT {name} failed:[/red] {e}")
        return []

def _agents_node(state: dict) -> dict:
    collection = _get_col(state["collection_name"])
    extra = get_high_risk_query_hints(state["repo_url"])
    bug_findings, security_findings, quality_findings, perf_findings = [], [], [], []

    try:
        if not collection or collection.count() == 0:
            console.log("[yellow]No chunks in collection, skipping agents[/yellow]")
            return {"bug_findings": [], "security_findings": [], "quality_findings": [], "perf_findings": [], "status": "synthesizing"}

        console.log("[bold yellow]AGENTS[/bold yellow] Running all 4 agents in parallel...")
        agent_map = {"bug": (run_bug_agent, "bug"), "security": (run_security_agent, "security"),
                     "quality": (run_quality_agent, "quality"), "perf": (run_perf_agent, "perf")}

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(_run_agent_safe, name, fn, state, collection, extra): name for name, (fn, _) in agent_map.items()}
            results = {}
            for future in as_completed(futures, timeout=AGENT_TIMEOUT * 4):
                name = futures[future]
                try:
                    results[name] = future.result(timeout=AGENT_TIMEOUT)
                    console.log(f"[green]AGENT {name}[/green] done — {len(results[name])} findings")
                except (FutureTimeout, Exception) as e:
                    console.log(f"[red]AGENT {name} error/timeout:[/red] {e}")
                    results[name] = []

        bug_findings, security_findings, quality_findings, perf_findings = (
            results.get("bug", []), results.get("security", []), results.get("quality", []), results.get("perf", [])
        )

        console.log("[bold magenta]REFLECT[/bold magenta] Chain detection...")
        all_findings = bug_findings + security_findings + quality_findings + perf_findings
        chains = detect_vulnerability_chains(all_findings)
        if chains:
            console.log(f"[bold red]CHAINS[/bold red] {len(chains)} chain(s) found")
            security_findings = security_findings + chains
    finally:
        _del_col(state.get("collection_name", ""))

    return {"bug_findings": bug_findings, "security_findings": security_findings,
            "quality_findings": quality_findings, "perf_findings": perf_findings, "status": "synthesizing"}

def _synthesize_node(state: dict) -> dict:
    console.log("[bold magenta]PIPELINE[/bold magenta] Synthesizing report...")
    ns = _state_to_ns(state)
    memory_insights = get_similar_repo_insights(state["repo_url"])
    result = run_synthesizer(ns, memory_insights=memory_insights)

    all_findings = state.get("bug_findings", []) + state.get("security_findings", []) + state.get("quality_findings", []) + state.get("perf_findings", [])
    store_review_patterns(state["repo_url"], all_findings, result.score)

    # Cleanup cloned repo and chroma temp dir
    _cleanup(state)

    return {"summary": result.summary, "score": result.score, "report_markdown": result.report_markdown, "status": "done"}

def build_graph():
    graph = StateGraph(ReviewState)
    graph.add_node("ingest", _ingest_node)
    graph.add_node("agents", _agents_node)
    graph.add_node("synthesize", _synthesize_node)
    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "agents")
    graph.add_edge("agents", "synthesize")
    graph.add_edge("synthesize", END)
    return graph.compile()

def run_review_sync(repo_url: str) -> dict:
    initial_state = {
        "repo_url": repo_url, "repo_path": "", "chroma_temp_dir": "", "collection_name": "", "all_chunks": [],
        "bug_findings": [], "security_findings": [], "quality_findings": [], "perf_findings": [],
        "summary": "", "score": 0, "report_markdown": "", "status": "pending", "error": None,
    }
    return build_graph().invoke(initial_state)

async def run_review(repo_url: str) -> dict:
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, run_review_sync, repo_url)
