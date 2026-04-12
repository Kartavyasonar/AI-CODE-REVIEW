"""
core/pipeline.py  v2  — FIXED
Key fixes vs original:
1. Nodes return plain dicts (what LangGraph expects), not ReviewState objects
2. InitialState uses correct TypedDict defaults
3. Fan-out agents each write ONLY their own findings key → no conflict
4. reflection_chains and synthesize run SEQUENTIALLY after fan-in
5. Removed ThreadPoolExecutor wrapping (LangGraph handles parallelism)
"""
import asyncio
from langgraph.graph import StateGraph, END

from backend.core.ingestion import ingest_repo
from backend.core.state import ReviewState, Finding
from backend.core.memory import get_high_risk_query_hints, get_similar_repo_insights, store_review_patterns
from backend.core.reflection import detect_vulnerability_chains
from backend.agents.bug_agent import run_bug_agent
from backend.agents.security_agent import run_security_agent
from backend.agents.quality_agent import run_quality_agent
from backend.agents.perf_agent import run_perf_agent
from backend.agents.synthesizer import run_synthesizer
from rich.console import Console

console = Console()

# Module-level ChromaDB collection store (can't be serialised into LangGraph state)
_active_collections: dict = {}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _state_to_ns(state: dict):
    """Convert state dict to a simple namespace so agent code can do state.foo."""
    class NS:
        pass
    ns = NS()
    for k, v in state.items():
        setattr(ns, k, v)
    return ns


def _findings_to_dicts(findings: list) -> list:
    """Convert Finding objects → dicts for JSON serialisation in API."""
    return [f.model_dump() if hasattr(f, "model_dump") else f for f in findings]


# ── Nodes ─────────────────────────────────────────────────────────────────────

def _ingest_node(state: dict) -> dict:
    console.log("[bold cyan]PIPELINE[/bold cyan] Ingesting repo...")
    collection, chunks, repo_path = ingest_repo(state["repo_url"])
    _active_collections[collection.name] = collection
    return {
        "repo_path": repo_path,
        "collection_name": collection.name,
        "all_chunks": chunks,
        "status": "analyzing",
    }


def _bug_node(state: dict) -> dict:
    console.log("[bold yellow]AGENT[/bold yellow] Bug (HyDE + rerank + reflect)...")
    collection = _active_collections.get(state["collection_name"])
    if not collection:
        return {"bug_findings": []}
    ns = _state_to_ns(state)
    ns.bug_findings = []
    extra = get_high_risk_query_hints(state["repo_url"])
    result = run_bug_agent(ns, collection, extra_queries=extra)
    return {"bug_findings": result.bug_findings}


def _security_node(state: dict) -> dict:
    console.log("[bold red]AGENT[/bold red] Security (HyDE + rerank + reflect)...")
    collection = _active_collections.get(state["collection_name"])
    if not collection:
        return {"security_findings": []}
    ns = _state_to_ns(state)
    ns.security_findings = []
    extra = get_high_risk_query_hints(state["repo_url"])
    result = run_security_agent(ns, collection, extra_queries=extra)
    return {"security_findings": result.security_findings}


def _quality_node(state: dict) -> dict:
    console.log("[bold blue]AGENT[/bold blue] Quality (HyDE + rerank + reflect)...")
    collection = _active_collections.get(state["collection_name"])
    if not collection:
        return {"quality_findings": []}
    ns = _state_to_ns(state)
    ns.quality_findings = []
    extra = get_high_risk_query_hints(state["repo_url"])
    result = run_quality_agent(ns, collection, extra_queries=extra)
    return {"quality_findings": result.quality_findings}


def _perf_node(state: dict) -> dict:
    console.log("[bold green]AGENT[/bold green] Performance (HyDE + rerank + reflect)...")
    collection = _active_collections.get(state["collection_name"])
    if not collection:
        return {"perf_findings": []}
    ns = _state_to_ns(state)
    ns.perf_findings = []
    extra = get_high_risk_query_hints(state["repo_url"])
    result = run_perf_agent(ns, collection, extra_queries=extra)
    return {"perf_findings": result.perf_findings}


def _reflection_node(state: dict) -> dict:
    """Runs AFTER all 4 agents complete. Detects cross-file vulnerability chains."""
    console.log("[bold magenta]REFLECT[/bold magenta] Multi-hop chain detection...")
    all_findings = (
        state.get("bug_findings", []) +
        state.get("security_findings", []) +
        state.get("quality_findings", []) +
        state.get("perf_findings", [])
    )
    chains = detect_vulnerability_chains(all_findings)
    if chains:
        console.log(f"[bold red]CHAINS[/bold red] {len(chains)} vulnerability chain(s) found")
        return {"security_findings": chains}   # operator.add appends to existing list
    return {"security_findings": []}


def _synthesize_node(state: dict) -> dict:
    console.log("[bold magenta]PIPELINE[/bold magenta] Synthesizing report...")
    ns = _state_to_ns(state)
    memory_insights = get_similar_repo_insights(state["repo_url"])
    result = run_synthesizer(ns, memory_insights=memory_insights)

    all_findings = (
        state.get("bug_findings", []) +
        state.get("security_findings", []) +
        state.get("quality_findings", []) +
        state.get("perf_findings", [])
    )
    store_review_patterns(state["repo_url"], all_findings, result.score)
    _active_collections.pop(state.get("collection_name", ""), None)

    return {
        "summary": result.summary,
        "score": result.score,
        "report_markdown": result.report_markdown,
        "status": "done",
    }


# ── Graph ─────────────────────────────────────────────────────────────────────

def build_graph():
    graph = StateGraph(ReviewState)

    graph.add_node("ingest",            _ingest_node)
    graph.add_node("bug_agent",         _bug_node)
    graph.add_node("security_agent",    _security_node)
    graph.add_node("quality_agent",     _quality_node)
    graph.add_node("perf_agent",        _perf_node)
    graph.add_node("reflection_chains", _reflection_node)
    graph.add_node("synthesize",        _synthesize_node)

    graph.set_entry_point("ingest")

    # Fan-OUT: ingest → 4 parallel agents
    for agent in ["bug_agent", "security_agent", "quality_agent", "perf_agent"]:
        graph.add_edge("ingest", agent)

    # Fan-IN: all 4 agents → reflection (LangGraph waits for all before proceeding)
    for agent in ["bug_agent", "security_agent", "quality_agent", "perf_agent"]:
        graph.add_edge(agent, "reflection_chains")

    graph.add_edge("reflection_chains", "synthesize")
    graph.add_edge("synthesize", END)

    return graph.compile()


async def run_review(repo_url: str) -> dict:
    """Entry point. Returns the final state dict."""
    initial_state: ReviewState = {
        "repo_url": repo_url,
        "repo_path": "",
        "collection_name": "",
        "all_chunks": [],
        "bug_findings": [],
        "security_findings": [],
        "quality_findings": [],
        "perf_findings": [],
        "summary": "",
        "score": 0,
        "report_markdown": "",
        "status": "pending",
        "error": None,
    }
    compiled = build_graph()
    # Run in thread so async callers don't block the event loop
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: compiled.invoke(initial_state))
    return result


def run_review_sync(repo_url: str) -> dict:
    """Synchronous entry point — use this directly from CLI to avoid Windows asyncio issues."""
    initial_state: ReviewState = {
        "repo_url": repo_url,
        "repo_path": "",
        "collection_name": "",
        "all_chunks": [],
        "bug_findings": [],
        "security_findings": [],
        "quality_findings": [],
        "perf_findings": [],
        "summary": "",
        "score": 0,
        "report_markdown": "",
        "status": "pending",
        "error": None,
    }
    compiled = build_graph()
    return compiled.invoke(initial_state)
