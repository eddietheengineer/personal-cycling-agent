"""
LLM Wiki — a Karpathy-style second brain for the cycling agent.

Incrementally builds and maintains a persistent wiki of markdown files
that sits between raw sources and the user. The LLM writes and maintains
all wiki content; the user curates sources and asks questions.

Modules:
- engine: core wiki operations (create, read, update pages)
- index: index.md and log.md management
- ingest: source ingestion pipeline with LLM processing
"""