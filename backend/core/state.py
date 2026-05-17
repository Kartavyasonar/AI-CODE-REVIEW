"""
core/state.py  v3
Plain TypedDict — no Annotated reducers needed because agents now run
sequentially inside a single node (no fan-out/fan-in).

The Annotated[list, operator.add] reducers were needed only for parallel
fan-out where multiple nodes write to the same key simultaneously.
With sequential execution, last-write-wins is exactly what we want.
"""
from __future__ import annotations
from typing import Optional, TypedDict
from pydantic import BaseModel


class Finding(BaseModel):
    """A single code review finding produced by any agent."""
    agent:        str
    severity:     str           # critical | high | medium | low | info
    category:     str
    file:         str
    line:         Optional[int] = None
    title:        str
    description:  str
    suggestion:   str
    code_snippet: Optional[str] = None


class ReviewState(TypedDict):
    repo_url:          str
    repo_path:         str
    collection_name:   str
    all_chunks:        list

    bug_findings:      list
    security_findings: list
    quality_findings:  list
    perf_findings:     list

    summary:           str
    score:             int
    report_markdown:   str
    status:            str
    error:             Optional[str]