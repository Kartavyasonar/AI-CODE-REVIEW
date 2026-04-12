"""agents/synthesizer.py  v2"""
from langchain_core.messages import HumanMessage, SystemMessage
from backend.core.llm import get_llm
from backend.core.state import Finding

# ── Scoring ───────────────────────────────────────────────────────────────────
# Score uses a logarithmic penalty so a large repo doesn't auto-score 0.
# Formula: start at 100, subtract log-scaled penalty per severity bucket.
# Max penalty per bucket: critical=30, high=20, medium=10, low=5
BUCKET_MAX   = {"critical": 30, "high": 20, "medium": 10, "low": 5, "info": 0}
BUCKET_SCALE = {"critical": 2,  "high": 3,  "medium": 5,  "low": 10, "info": 999}
# BUCKET_SCALE = "how many findings before penalty maxes out"

def compute_score(all_findings) -> int:
    """
    Logarithmic scoring: each severity bucket contributes at most BUCKET_MAX points
    of penalty, reached after BUCKET_SCALE findings in that bucket.
    A repo with 60 medium findings still scores ~60, not 0.
    """
    import math
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for f in all_findings:
        sev = getattr(f, "severity", "low")
        if sev in counts:
            counts[sev] += 1

    total_penalty = 0
    for sev, count in counts.items():
        if count == 0:
            continue
        scale = BUCKET_SCALE[sev]
        max_p = BUCKET_MAX[sev]
        # log curve: penalty = max_p * (1 - 1/(1 + count/scale))
        penalty = max_p * (1 - 1 / (1 + count / scale))
        total_penalty += penalty

    return max(0, round(100 - total_penalty))


# ── Dedup ─────────────────────────────────────────────────────────────────────
def deduplicate(findings):
    seen, result = set(), []
    for f in findings:
        key = (getattr(f, "file", ""), getattr(f, "title", "")[:50])
        if key not in seen:
            seen.add(key)
            result.append(f)
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────
def severity_emoji(s):
    return {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}.get(s, "⚪")


def _grade(score):
    if score >= 85: return "A — solid codebase"
    if score >= 70: return "B — good with minor issues"
    if score >= 55: return "C — needs attention"
    if score >= 40: return "D — significant problems"
    return "F — critical issues found"


# ── Summary ───────────────────────────────────────────────────────────────────
SUMMARY_PROMPT = """You are a staff engineer writing a code review executive summary.
Write exactly 3 sentences:
1. Overall health of the codebase (be specific about what's good vs bad)
2. The single most important file/issue to fix first and why
3. One concrete next step the team should take this week
Be direct. No filler words. Max 80 words total."""


def _build_summary(state, llm) -> str:
    bug_f  = getattr(state, "bug_findings", [])
    sec_f  = getattr(state, "security_findings", [])
    qual_f = getattr(state, "quality_findings", [])
    perf_f = getattr(state, "perf_findings", [])
    all_f  = bug_f + sec_f + qual_f + perf_f

    # Send only top 15 findings to stay under token limit
    order = ["critical", "high", "medium", "low", "info"]
    sorted_f = sorted(all_f, key=lambda x: order.index(getattr(x, "severity", "info")) if getattr(x, "severity", "info") in order else 99)
    top = sorted_f[:15]

    lines = [f"[{getattr(f,'severity','?').upper()}] {getattr(f,'file','?')}: {getattr(f,'title','')}" for f in top]
    findings_text = "\n".join(lines) or "No findings."

    try:
        resp = llm.invoke([
            SystemMessage(content=SUMMARY_PROMPT),
            HumanMessage(content=f"Repo: {getattr(state,'repo_url','')}\nTop findings:\n{findings_text}"),
        ])
        return resp.content.strip()
    except Exception as e:
        err = str(e)
        # Give a useful auto-summary when rate-limited
        if "rate_limit" in err or "429" in err:
            criticals = [f for f in all_f if getattr(f,"severity","") == "critical"]
            highs     = [f for f in all_f if getattr(f,"severity","") == "high"]
            top3 = (criticals + highs)[:3]
            top3_titles = ", ".join(getattr(f,"title","") for f in top3) or "see findings below"
            return (
                f"Auto-summary (Groq rate limit hit — try again in a few minutes): "
                f"{len(all_f)} findings across {len(bug_f)} bugs, {len(sec_f)} security, "
                f"{len(qual_f)} quality, {len(perf_f)} performance issues. "
                f"Priority: {top3_titles}."
            )
        return f"Summary unavailable: {err}"


# ── Report generator ──────────────────────────────────────────────────────────
def generate_report(state, summary, memory_insights="") -> str:
    bug_f  = getattr(state, "bug_findings", [])
    sec_f  = getattr(state, "security_findings", [])
    qual_f = getattr(state, "quality_findings", [])
    perf_f = getattr(state, "perf_findings", [])
    all_f  = bug_f + sec_f + qual_f + perf_f
    score  = compute_score(all_f)
    chains = [f for f in sec_f if "[CHAIN]" in getattr(f, "title", "")]

    bar_filled = score // 10
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    grade = _grade(score)

    lines = [
        "# AI Code Review Report — v2 Advanced",
        "",
        f"**Repository:** `{getattr(state,'repo_url','')}`",
        "",
        f"## Score: {score}/100 — {grade}",
        "```",
        f"{bar} {score}/100",
        "```",
        "",
        "| Category | Findings |",
        "|---|---|",
        f"| 🐛 Bugs | {len(bug_f)} |",
        f"| 🔒 Security | {len(sec_f)} |",
        f"| 🧹 Quality | {len(qual_f)} |",
        f"| ⚡ Performance | {len(perf_f)} |",
        f"| **Total** | **{len(all_f)}** |",
        "",
    ]

    if chains:
        lines += [f"⛓ **{len(chains)} Vulnerability Chain(s) Detected**", ""]

    lines += ["## Executive Summary", "", summary]
    if memory_insights:
        lines += ["", memory_insights]

    lines += [
        "",
        "## AI Concepts Used",
        "",
        "- **HyDE** — Hypothetical Document Embedding: generates a fake vulnerable code snippet, embeds it, retrieves semantically closer results than plain keyword search",
        "- **Cross-encoder reranker** — ms-marco-MiniLM rescores 30 candidates → keeps top 10 (higher precision than cosine similarity alone)",
        "- **Self-reflection** — Critic LLM audits every finding, assigns 0–1 confidence, drops hallucinations",
        "- **Multi-hop RAG** — Cross-agent chain detector finds vulnerabilities that span multiple files",
        "- **Memory store** — ChromaDB persists review patterns for cross-repo benchmarking",
        "- **LangGraph** — TypedDict state with `Annotated[list, operator.add]` reducers for parallel fan-out/fan-in",
        "",
    ]

    order = ["critical", "high", "medium", "low", "info"]
    for section, findings in [
        ("🐛 Bug Findings", bug_f),
        ("🔒 Security Findings", sec_f),
        ("🧹 Code Quality", qual_f),
        ("⚡ Performance", perf_f),
    ]:
        if not findings:
            continue
        lines.append(f"## {section}")
        lines.append("")
        sorted_f = sorted(findings, key=lambda x: order.index(getattr(x,"severity","info")) if getattr(x,"severity","info") in order else 99)
        for f in sorted_f:
            sev = getattr(f, "severity", "info")
            file_ = getattr(f, "file", "?")
            line_ = getattr(f, "line", None)
            lines += [
                f"### {severity_emoji(sev)} [{sev.upper()}] {getattr(f,'title','')}",
                "",
                f"**File:** `{file_}`" + (f"  **Line:** {line_}" if line_ else ""),
                "",
                f"**Issue:** {getattr(f,'description','')}",
                "",
                f"**Fix:** {getattr(f,'suggestion','')}",
            ]
            snippet = getattr(f, "code_snippet", None)
            if snippet:
                lines += ["", "```", snippet.strip(), "```"]
            lines.append("")

    lines += [
        "---",
        "*AI Code Review Agent v2 · LangGraph + HyDE + Reranker + Self-Reflection + Memory · Groq LLaMA 3.3 70B*",
    ]
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────
def run_synthesizer(state, memory_insights=""):
    llm = get_llm()

    all_findings = deduplicate(
        getattr(state, "bug_findings", []) +
        getattr(state, "security_findings", []) +
        getattr(state, "quality_findings", []) +
        getattr(state, "perf_findings", [])
    )

    state.bug_findings      = [f for f in all_findings if getattr(f,"agent","") == "bug_agent"]
    state.security_findings = [f for f in all_findings if getattr(f,"agent","") in ("security_agent","reflection_chain")]
    state.quality_findings  = [f for f in all_findings if getattr(f,"agent","") == "quality_agent"]
    state.perf_findings     = [f for f in all_findings if getattr(f,"agent","") == "perf_agent"]

    state.summary         = _build_summary(state, llm)
    state.score           = compute_score(all_findings)
    state.report_markdown = generate_report(state, state.summary, memory_insights)
    state.status          = "done"
    return state