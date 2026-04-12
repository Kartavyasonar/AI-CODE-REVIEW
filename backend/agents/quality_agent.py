"""agents/quality_agent.py  v2"""
import json
from pathlib import Path
from langchain_core.messages import HumanMessage, SystemMessage

from backend.core.llm import get_llm
from backend.core.hyde import hyde_queries
from backend.core.reranker import retrieve_and_rerank
from backend.core.reflection import score_and_reflect
from backend.core.state import Finding

try:
    from radon.complexity import cc_visit
    from radon.metrics import mi_visit
    RADON_AVAILABLE = True
except ImportError:
    RADON_AVAILABLE = False

SYSTEM_PROMPT = """You are a senior engineer focused on code quality.
Analyze for: missing docstrings, functions >50 lines, deep nesting (>3 levels),
magic numbers, poor naming, duplicate code, unused imports, missing type annotations.

Respond ONLY with valid JSON array:
{
  "severity": "high|medium|low|info",
  "category": "quality",
  "file": "<filepath>",
  "line": <line or null>,
  "title": "<issue>",
  "description": "<why it matters>",
  "suggestion": "<fix>",
  "code_snippet": "<code, max 3 lines>"
}
Only JSON. Return [].
"""

BASE_QUERIES = [
    "function missing docstring no documentation",
    "magic number hardcoded string constant",
    "long function deep nesting if else",
    "unused variable import dead code",
    "duplicate code repeated logic",
]


def _complexity_scan(repo_path):
    if not RADON_AVAILABLE or not repo_path:
        return []
    findings = []
    try:
        for py_file in Path(repo_path).rglob("*.py"):
            if any(p in py_file.parts for p in [".git","venv","__pycache__","node_modules"]):
                continue
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
                rel = str(py_file.relative_to(repo_path))
                for block in cc_visit(content):
                    if block.complexity >= 10:
                        sev = "high" if block.complexity >= 15 else "medium"
                        findings.append(Finding(
                            agent="quality_agent", severity=sev, category="quality",
                            file=rel, line=block.lineno,
                            title=f"High complexity: {block.name} (CC={block.complexity})",
                            description=f"Cyclomatic complexity {block.complexity}. Above 10 is hard to test.",
                            suggestion="Break into smaller functions. Target CC < 5.",
                            code_snippet=f"def {block.name}(...):  # CC={block.complexity}",
                        ))
                mi = mi_visit(content, multi=True)
                if isinstance(mi, (int, float)) and mi < 20:
                    findings.append(Finding(
                        agent="quality_agent", severity="medium", category="quality",
                        file=rel, line=None,
                        title=f"Low maintainability index: {mi:.1f}/100",
                        description=f"MI={mi:.1f}. Below 20 is considered unmaintainable.",
                        suggestion="Add docstrings, reduce function size.",
                        code_snippet=None,
                    ))
            except Exception:
                pass
    except Exception:
        pass
    return findings


def run_quality_agent(state, collection, extra_queries=None):
    llm = get_llm()
    repo_path = getattr(state, "repo_path", "")
    static_findings = _complexity_scan(repo_path)

    if collection.count() == 0:
        state.quality_findings = static_findings
        return state

    all_queries = BASE_QUERIES + (extra_queries or [])
    hyde_expanded = hyde_queries(all_queries)
    llm_findings = []
    seen = set()
    all_code_context = []

    for original_q, hyde_q in zip(all_queries, hyde_expanded):
        chunks = retrieve_and_rerank(collection, original_q, hyde_q,
                                     n_retrieve=min(25, collection.count()), top_k=10)
        if not chunks:
            continue
        code_context = "\n\n---\n\n".join(f"File: {c.get('filepath','?')}\n{c['content']}" for c in chunks)
        all_code_context.append(code_context)
        try:
            response = llm.invoke([SystemMessage(content=SYSTEM_PROMPT),
                                    HumanMessage(content=f"Review quality:\n\n{code_context}")])
            raw = response.content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            for fd in json.loads(raw):
                if not isinstance(fd, dict):
                    continue
                key = (fd.get("file",""), fd.get("title",""))
                if key in seen:
                    continue
                seen.add(key)
                llm_findings.append(Finding(
                    agent="quality_agent",
                    severity=fd.get("severity","low"), category=fd.get("category","quality"),
                    file=fd.get("file","unknown"), line=fd.get("line"),
                    title=fd.get("title","Untitled"), description=fd.get("description",""),
                    suggestion=fd.get("suggestion",""), code_snippet=fd.get("code_snippet"),
                ))
        except Exception:
            pass

    combined_context = "\n\n".join(all_code_context)[:4000]
    llm_findings = score_and_reflect(llm_findings, combined_context)
    state.quality_findings = static_findings + llm_findings
    return state
