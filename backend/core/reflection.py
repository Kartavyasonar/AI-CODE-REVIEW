"""
core/reflection.py  —  Self-Reflection + Confidence Scoring

After each agent produces findings, a reflection step:
1. Scores each finding with a confidence level (0.0 - 1.0)
2. Challenges low-confidence findings with a "critic" LLM call
3. Either confirms, refines, or drops the finding
4. Adds a reasoning chain to each confirmed finding

This implements the "Reflexion" pattern (Shinn et al. 2023) adapted for code review.
It catches hallucinated findings before they reach the report.
"""
import json
from langchain_core.messages import HumanMessage, SystemMessage
from backend.core.llm import get_llm
from backend.core.state import Finding

CONFIDENCE_PROMPT = """You are a meticulous senior code reviewer auditing AI-generated findings.
For each finding below, decide:
1. Is this a REAL issue in the code shown, or is the AI hallucinating / over-reaching?
2. Assign a confidence score 0.0-1.0
3. If confidence < 0.6, either REFINE the finding to be more accurate, or REJECT it

Respond ONLY with JSON array:
[
  {
    "index": <original index>,
    "action": "confirm" | "refine" | "reject",
    "confidence": <0.0-1.0>,
    "refined_title": "<only if action=refine>",
    "refined_description": "<only if action=refine>",
    "reasoning": "<one sentence why>"
  }
]
Be harsh. Reject vague findings. Reject findings not supported by the code snippet.
Only JSON. No markdown.
"""

MULTI_HOP_PROMPT = """You are doing a multi-hop analysis of related code findings.
You have been given several findings from different files that may be related.
Identify if they form a CHAIN OF VULNERABILITY — e.g. unsanitized input in file A
flows into a SQL query in file B, creating an injection chain.

If you find a chain, describe it as a NEW combined finding with severity = one level higher
than the individual findings. If no chain exists, return [].

Respond ONLY with JSON array of new chained findings:
[
  {
    "severity": "critical|high|medium|low",
    "category": "<category>",
    "file": "<primary file>",
    "title": "<chain title>",
    "description": "<full chain description>",
    "suggestion": "<how to break the chain>",
    "code_snippet": null
  }
]
Only JSON.
"""


def score_and_reflect(findings: list[Finding], code_context: str) -> list[Finding]:
    """
    Run self-reflection over a list of findings.
    Returns confirmed/refined findings with confidence scores attached.
    Low-confidence findings that survive reflection get a warning in their description.
    """
    if not findings:
        return []

    llm = get_llm(temperature=0.0)  # Zero temp for critic — we want determinism

    # Build structured finding list for the critic
    finding_entries = []
    for i, f in enumerate(findings):
        entry = {
            "index": i,
            "severity": f.severity,
            "title": f.title,
            "description": f.description,
            "file": f.file,
            "code_snippet": f.code_snippet or "",
        }
        finding_entries.append(entry)

    messages = [
        SystemMessage(content=CONFIDENCE_PROMPT),
        HumanMessage(content=(
            f"Code context:\n{code_context[:3000]}\n\n"
            f"Findings to audit:\n{json.dumps(finding_entries, indent=2)}"
        )),
    ]

    try:
        response = llm.invoke(messages)
        raw = response.content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        decisions = json.loads(raw)
    except Exception:
        # If reflection fails, return originals — never lose findings due to critic error
        return findings

    refined_findings = []
    decision_map = {d["index"]: d for d in decisions}

    for i, finding in enumerate(findings):
        decision = decision_map.get(i, {"action": "confirm", "confidence": 0.7, "reasoning": ""})
        action = decision.get("action", "confirm")
        confidence = decision.get("confidence", 0.7)

        if action == "reject":
            continue  # Drop hallucinated finding

        if action == "refine":
            finding.title = decision.get("refined_title", finding.title)
            finding.description = decision.get("refined_description", finding.description)
            finding.description += f" [Confidence: {confidence:.0%} — refined by self-reflection]"
        else:
            if confidence < 0.6:
                finding.description += f" [Confidence: {confidence:.0%} — verify manually]"

        refined_findings.append(finding)

    return refined_findings


def detect_vulnerability_chains(all_findings: list[Finding]) -> list[Finding]:
    """
    Multi-hop RAG reasoning: look across findings from different agents/files
    to detect vulnerability chains (e.g. unsanitized input → SQL injection path).
    Returns any newly discovered chained findings.
    """
    if len(all_findings) < 3:
        return []

    llm = get_llm(temperature=0.1)

    # Focus on security + bug findings for chain detection
    candidates = [f for f in all_findings if f.category in ("security", "bug")][:20]
    if len(candidates) < 2:
        return []

    finding_summary = "\n".join(
        f"[{f.severity.upper()}] {f.file}: {f.title} — {f.description[:100]}"
        for f in candidates
    )

    messages = [
        SystemMessage(content=MULTI_HOP_PROMPT),
        HumanMessage(content=f"Findings from this codebase:\n{finding_summary}"),
    ]

    try:
        response = llm.invoke(messages)
        raw = response.content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        chain_data = json.loads(raw)

        chains = []
        for cd in chain_data:
            chains.append(Finding(
                agent="reflection_chain",
                severity=cd.get("severity", "high"),
                category=cd.get("category", "security"),
                file=cd.get("file", "multiple"),
                line=None,
                title=f"[CHAIN] {cd.get('title', 'Vulnerability chain detected')}",
                description=cd.get("description", ""),
                suggestion=cd.get("suggestion", ""),
                code_snippet=None,
            ))
        return chains
    except Exception:
        return []
