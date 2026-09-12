# PsiHub Reader Master v11

A quality-first academic PDF understanding + translation pipeline.

## Pipeline

PDF bytes
-> source-page preservation
-> publisher-page detection
-> native geometric layout analysis
-> block-level Document Model
-> structural invariants / anomaly scoring
-> conservative physical cleanup
-> cleaned-PDF snapshot
-> pymupdf4llm extraction with page provenance
-> two-column / footnote normalization
-> boundary Vision arbitration
-> risk-routed full-page Vision arbitration
-> deterministic Markdown cleanup
-> language detection
-> table isolation + structural validation
-> concurrent DeepSeek translation
-> restoration + placeholder validation
-> reference indexing + PDF backlinks
-> mobile-safe Markdown
-> persistent cache

## Design principles

1. The Document Model is the source of structural truth; Markdown is an output format.
2. Native PDF geometry is authoritative when confidence is high.
3. Vision is an arbiter, not a free-form editor.
4. Expensive visual calls are routed to structurally risky pages.
5. Original source PDF and page provenance are preserved.
6. Translation is parallelized while result order remains deterministic.
7. Tables are isolated and structurally validated before replacement.
8. Caches are versioned so structural changes never silently reuse stale results.

## VLM arbitration

Two complementary visual passes exist:
- boundary arbitration for cross-page paragraph/table/list/citation continuity;
- full-page arbitration for high-risk pages, comparing native block geometry with the rendered page.

The VLM can report conflicts and recommended reading order, but it never directly rewrites the extracted academic text.

## Runtime

`pymupdf4llm` and Pillow are required. Vision and translation require their respective API keys.
The full runtime should be tested against a representative corpus of one-column, two-column, tables, equations, footnotes, publisher front matter, scans and mixed-layout papers before production deployment.
