"""Retrieval-quality benchmark for Aquifer itself.

Builds a lake from a corpus of repository directories with the *production* pipeline (connector
→ extraction → chunking → embedding → index) and runs scenario cases through the *production*
MCP tool implementations, scoring what an agent would actually receive. Nothing in the
retrieval path knows about the benchmark; expected answers live only in the case files.

Offline mode (default, used in CI) is deterministic: in-memory store, hashing (lexical)
embedder, deterministic extractors only. Cases that need LLM extraction are reported as
skipped, not as passed. ``--bedrock`` uses Bedrock embeddings and LLM extraction instead.
"""

from aquifer.benchmark.runner import load_cases, run_benchmark

__all__ = ["load_cases", "run_benchmark"]
