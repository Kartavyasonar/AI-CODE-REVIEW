"""
core/state.py  v4
Added chroma_temp_dir to ensure proper cleanup and prevent disk space leaks.
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
    chroma_temp_dir:   str  # Added for ChromaDB temp directory cleanup
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
