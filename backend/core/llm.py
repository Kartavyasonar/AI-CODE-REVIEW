"""
core/llm.py
Thin wrapper around Groq via LangChain. Single place to swap models.
"""
import os
from functools import lru_cache

from langchain_groq import ChatGroq


@lru_cache(maxsize=1)
def get_llm(model: str = "llama-3.3-70b-versatile", temperature: float = 0.1) -> ChatGroq:
    """Return a cached ChatGroq instance."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY is not set. Copy .env.example to .env and add your key.")
    return ChatGroq(
        api_key=api_key,
        model=model,
        temperature=temperature,
        max_tokens=4096,
    )
