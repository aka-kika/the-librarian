# Embedder test, 2026-10-12

Question: should the librarian swap nomic-embed-text for embeddinggemma-2:740m-mxfp8?

Test: the 31 intents in ../decision-filter-2026-10-09/testset.json that have a skill marked good,
ranked against all 780 skills. Script: bench.py (run with `uv run --with numpy --with httpx python bench.py`).

| Model | Good skill in top 5 | In top 10 | MRR | Good ranked above bad |
| --- | --- | --- | --- | --- |
| nomic-embed-text (live) | 25/31 | 28/31 | 0.614 | 60/64 |
| embeddinggemma-2, plain | 21/31 | 26/31 | 0.589 | 58/64 |
| embeddinggemma-2, with its query/title prompts | 22/31 | 26/31 | 0.602 | 57/64 |

Bias check: most top-5 picks were never judged for either model (197 vs 206 of 240), so the
labels do not strongly favour nomic.

Decision: keep nomic-embed-text. The new model is not better here, is 5x bigger in memory
(1.4 GB vs 274 MB) and slower per query.
