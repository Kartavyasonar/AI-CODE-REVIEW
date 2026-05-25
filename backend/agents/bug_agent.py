"""
agents/bug_agent.py  v3

Improvements vs v2:
  1. Test file filtering — intentional exception patterns in test files are skipped
  2. File type context in LLM prompt — LLM knows if file is test/production/example
  3. Tighter system prompt — explicitly tells LLM to ignore test assertion patterns
  4. Better dedup using normalized title to catch near-duplicates
"""
import json
from langchain_core.messages import HumanMessage, SystemMessage

from backend.core.llm import get_llm
from backend.core.hyde import hyde_queries
from backend.core.reranker import retrieve_and_rerank
from backend.core.reflection import score_and_reflect
from backend.core.state import Finding

SYSTEM_PROMPT = """You are an expert software engineer finding real bugs in PRODUCTION code.

Analyze for:
- Logic errors and off-by-one errors
- Null/None reference issues and missing null checks
- Unhandled exceptions and bare except clauses hiding real errors
- Incorrect return values or missing returns
- Infinite loops or unreachable code
- Race conditions in async code
- Incorrect variable scoping

IMPORTANT RULES:
- IGNORE files marked as [TEST FILE] — exceptions in tests are intentional
- IGNORE files marked as [EXAMPLE CODE] — these are tutorials, not production
- Only flag bare except in PRODUCTION files where it hides real errors
- Only flag uncaught exceptions in PRODUCTION files
- Do NOT flag pytest fixtures, conftest patterns, or test helper functions
- Only include findings with strong evidence in the actual code shown

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
Return [] if nothing found. Only JSON. No markdown fences.
"""

BASE_QUERIES = [
    "null reference none check missing attribute error",
    "exception handling bare except pass error swallowed",
    "logic error off by one index return value wrong",
    "infinite loop async race condition shared state",
]

# Directories and filename patterns that indicate non-production code
_TEST_MARKERS = {"test_", "_test", "tests/", "/tests/", "conftest", "fixtures"}
_EXAMPLE_MARKERS = {"examples/", "/examples/", "tutorial/", "demo/", "sample/"}


def _classify_file(filepath: str) -> str:
    """Classify file as PRODUCTION, TEST FILE, or EXAMPLE CODE."""
    fp = filepath.lower().replace("\\", "/")
    if any(m in fp for m in _TEST_MARKERS):
        return "TEST FILE"
    if any(m in fp for m in _EXAMPLE_MARKERS):
        return "EXAMPLE CODE"
    return "PRODUCTION"


def _should_skip_finding(fd: dict) -> bool:
    """Return True if this finding should be filtered out."""
    filepath = fd.get("file", "").lower().replace("\\", "/")
    # Skip any finding from test or example files
    if any(m in filepath for m in _TEST_MARKERS):
        return True
    if any(m in filepath for m in _EXAMPLE_MARKERS):
        return True
    return False


def _normalize_title(title: str) -> str:
    """Normalize title for dedup — remove line numbers and minor variations."""
    import re
    return re.sub(r'\s+', ' ', title.lower().strip())


def run_bug_agent(state, collection, extra_queries=None):
    llm = get_llm()
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

        # Add file type context so LLM knows what kind of file it is looking at
        code_context = "\n\n---\n\n".join(
            f"File: {c.get('filepath','?')} [{_classify_file(c.get('filepath',''))}] "
            f"(line {c.get('start_line','?')})\n{c['content']}"
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

                # Filter out test and example file findings
                if _should_skip_finding(fd):
                    continue

                snippet = fd.get("code_snippet", "")
                key = (fd.get("file", ""), _normalize_title(fd.get("title", "")))
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