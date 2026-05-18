"""Eixo 2 v2c — RAG over the narrative archive.

Retrieval-Augmented Generation for ad-hoc analyst queries over the
historical corpus of narrative summaries stored in BigQuery.

Components
----------
retriever  — embed query + BQ VECTOR_SEARCH → list[NarrativeHit]
generator  — NarrativeHit[] + LLM → RAGAnswer
run_rag    — CLI glue (standalone; not a scheduled job)
"""
