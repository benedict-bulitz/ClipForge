"""Research Pipeline V2: discovery, retrieval, extraction, evidence and the research package.

See ``docs/research-pipeline-v2.md``.  The public entry point is
``run_research``; ``clipforge.research.research_topic`` calls it and keeps the
V1 path as the isolated fallback.
"""
from .service import ResearchRun, run_research

__all__ = ["ResearchRun", "run_research"]
