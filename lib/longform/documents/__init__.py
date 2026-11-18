"""Staged document writing pipeline (papers first, resumes second).

Responsibility: one fixed, checkpointed graph — intake → dossier → evidence
audit → novelty positioning → outline → draft → critique → revision →
render QA → blind A/B → delivery — over the production stage executor. Each
stage persists an inspectable artifact under ``<workdir>/artifacts/`` and
passes a machine-checked gate. Document types (``types.py``) vary required
sections, rules, rubric and venue kit; they never fork the graph.

Entry points: ``run_document_pipeline`` (``pipeline.py``), CLI
``scripts/document_pipeline.py``. Authority doc: ``docs/DOCUMENT_PIPELINE.md``.
"""
