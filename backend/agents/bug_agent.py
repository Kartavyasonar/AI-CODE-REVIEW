"""
agents/bug_agent.py  v2 — FIXED

Changes vs original:
  - No logic changes needed in this agent (it was correct).
  - hyde_queries is now parallelised upstream (hyde.py fix) so sequential
    latency from this agent is already resolved.
  - seen_snippets dedup key now also includes filepath to prevent cross-file
    false-positive deduplication (two different files could have identical snippets).
"""
import json
from langchain_core.messages import HumanMessage, SystemMessage

from backend.core.llm import get_llm
from backend.core.hyde import hyde_queries
from backend.core.reranker import retrieve_and_rerank
from backend.core.reflection import score_and_reflect
from backend.core.state import Finding

SYSTEM_PROMPT = """You are an expert software engineer specializing in finding bugs.
Analyze the given code chunks for:
- Logic errors and off-by-one errors
- Null/None reference issues and missing null checks
- Unhandled exceptions and bare except clauses
- Incorrect return values or missing returns
- Infinite loops or unreachable code
- Race conditions in async code
- Incorrect variable scoping

Respond ONLY with a valid JSON array. Each finding:
{
  "severity": "critical|high|medium|low",
  "category": "bug",
  "file": "<filepath>",
  "line": <line number or null>,
  "title": "<short title>",
  "description": "<what the bug is and why it matters>",
  "suggestion": "<exactly how to fix it>",
  "code_snippet": "<the problematic code, max 3 lines>"
}
Only include findings with strong evidence in the code shown.
Return [] if nothing found. Only JSON. No markdown fences.
"""

BASE_QUERIES = [
    "null reference none check missing attribute error",
    "exception handling bare except pass error swallowed",
    "logic error off by one index return value wrong",
    "infinite loop async race condition shared state",
]


def run_bug_agent(state, collection, extra_queries=None):
    llm        = get_llm()
    all_queries = BASE_QUERIES + (extra_queries or [])

    if collection.count() == 0:
        state.bug_findings = []
        return state

    hyde_expanded    = hyde_queries(all_queries)
    all_findings     = []
    seen_snippets    = set()
    all_code_context = []

    for original_q, hyde_q in zip(all_queries, hyde_expanded):
        chunks = retrieve_and_rerank(
            collection=collection,
            original_query=original_q,
            hyde_query=hyde_q,
            n_retrieve=min(30, collection.count()),
            top_k=10,
        )
        if not chunks:
            continue

        code_context = "\n\n---\n\n".join(
            f"File: {c.get('filepath','?')} (line {c.get('start_line','?')})\n{c['content']}"
            for c in chunks
        )
        all_code_context.append(code_context)

        try:
            response = llm.invoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=f"Find bugs in these code chunks:\n\n{code_context}"),
            ])
            raw = response.content.strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            for fd in json.loads(raw):
                if not isinstance(fd, dict):
                    continue
                # FIXED: include filepath in dedup key — same snippet in two files is two findings
                snippet = fd.get("code_snippet", "")
                key = (fd.get("file", ""), snippet)
                if key in seen_snippets:
                    continue
                seen_snippets.add(key)
                all_findings.append(Finding(
                    agent="bug_agent",
                    severity    =fd.get("severity",     "low"),
                    category    =fd.get("category",     "bug"),
                    file        =fd.get("file",         "unknown"),
                    line        =fd.get("line"),
                    title       =fd.get("title",        "Untitled"),
                    description =fd.get("description",  ""),
                    suggestion  =fd.get("suggestion",   ""),
                    code_snippet=fd.get("code_snippet"),
                ))
        except Exception:
            pass

    combined_context = "\n\n".join(all_code_context)[:4000]
    all_findings     = score_and_reflect(all_findings, combined_context)
    state.bug_findings = all_findings
    return state
