"""Analysis layer (Phase 3 entry point).

The ingestion pipeline never imports this package. The analysis layer READS
the database through ``bundle.py`` and talks to models only through the
``AnalysisProvider`` protocol, so providers (DeepSeek, Ollama, others) can be
swapped without touching collection or storage.
"""
