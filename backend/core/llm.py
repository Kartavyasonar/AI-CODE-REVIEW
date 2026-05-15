"""
core/llm.py
Thin wrapper around Groq via LangChain. Single place to swap models.

FIXED: lru_cache(maxsize=1) was used on a function with a `temperature` argument.
  lru_cache caches only the FIRST call — so get_llm(temperature=0.0) returned
  the cached temperature=0.1 instance, silently ignoring the argument.
  Replaced with a plain dict cache keyed on (model, temperature).
"""
import os
from langchain_groq import ChatGroq

_llm_cache: dict = {}


def get_llm(
    model: str = "llama-3.3-70b-versatile",
    temperature: float = 0.1,
) -> ChatGroq:
    """
    Return a cached ChatGroq instance for the given (model, temperature) pair.
    Multiple callers with different temperatures each get their own instance.
    """
    key = (model, temperature)
    if key not in _llm_cache:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "GROQ_API_KEY is not set. "
                "Copy .env.example to .env and add your key."
            )
        _llm_cache[key] = ChatGroq(
            api_key=api_key,
            model=model,
            temperature=temperature,
            max_tokens=4096,
        )
    return _llm_cache[key]
