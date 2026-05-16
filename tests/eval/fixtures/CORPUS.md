# Eval corpus: FSRS / spaced repetition

Topic: FSRS (Free Spaced Repetition Scheduler) and adjacent algorithms / engines.

## Why FSRS

- Narrow, well-bounded technical topic with enough surface for a 3–5 lesson course (algorithm internals, optimizer pipeline, language/runtime tradeoffs, concurrency model).
- The webhook's own ask-pipeline has been used with FSRS questions in real sessions, so we have informal ground truth for what a good course on this topic should cover.
- All sources are publicly documented; the fixture docs are short hand-written summaries — they paraphrase public material rather than copying it verbatim, so the corpus stays under tight size limits and free of licensing concerns.

## Provenance

The fixture docs below are short summaries written for testing purposes. They cite the public sources whose contents they paraphrase. They are NOT verbatim copies. Each doc has frontmatter (`url`, `title`, `tags`) so citation tests can resolve `[n]` references to a stable identity.

## Files

- `01-fsrs-algorithm.md` — core 3-variable FSRS model (difficulty, stability, retrievability).
- `02-fsrs-optimizer.md` — offline parameter optimizer + training-data scale (FSRS-6, ~700M reviews).
- `03-sm2-baseline.md` — SM-2 reference algorithm and what FSRS replaces.
- `04-anki-rs.md` — Rust + WASM port; per-card stateless review path.
- `05-py-fsrs.md` — Python reference implementation and its API surface.
- `06-throughput-tradeoffs.md` — algorithmic cost vs review-volume reduction at scale.
- `07-cloud-api-rate-limits.md` — SuperMemo-style cloud APIs and their concurrency ceiling.
- `08-card-state-machine.md` — per-card state transitions as the concurrency boundary.

Total: 8 docs. Each ≤ 5 KB. Together they cover enough surface for a course objective like *"compare scheduler designs and their concurrency tradeoffs."*
