"""
core/state.py
LangGraph state using TypedDict + Annotated reducers.

WHY TypedDict instead of Pydantic BaseModel:
  LangGraph requires TypedDict for its StateGraph. When multiple agents
  run in parallel (fan-out), they all write to the same state keys
  simultaneously. Without Annotated reducers, LangGraph raises:
    InvalidUpdateError: Can receive only one value per step.

  The fix: use operator.add as the reducer for list fields so LangGraph
  knows to CONCATENATE lists from parallel agents instead of overwriting.

  String/scalar fields that are only written by ONE node use `last_value`
  (the default) — no Annotated needed on those.
"""
from __future__ import annotations
from typing import Annotated, Optional, TypedDict
from pydantic import BaseModel
import operator


class Finding(BaseModel):
    """A single code review finding from any agent."""
    agent: str
    severity: str       # critical | high | medium | low | info
    category: str
    file: str
    line: Optional[int] = None
    title: str
    description: str
    suggestion: str
    code_snippet: Optional[str] = None


def _last(a, b):
    """Reducer: keep the last written value (for scalar fields written by one node)."""
    return b


class ReviewState(TypedDict):
    # Set once by ingest node — scalar, no reducer needed
    repo_url: str
    repo_path: str
    collection_name: str
    all_chunks: list

    # Written by 4 parallel agents — MUST use operator.add to concatenate
    bug_findings:      Annotated[list[Finding], operator.add]
    security_findings: Annotated[list[Finding], operator.add]
    quality_findings:  Annotated[list[Finding], operator.add]
    perf_findings:     Annotated[list[Finding], operator.add]

    # Written by synthesizer only
    summary:         str
    score:           int
    report_markdown: str
    status:          str
    error:           Optional[str]
