---
url: https://expertium.github.io/Benchmark.html
title: Throughput — Algorithmic Cost vs Review Volume Reduction
tags: [fsrs, sm2, throughput, benchmark]
---

# Throughput Tradeoffs

The naïve view is that FSRS is "slower" than SM-2 because it does a model
forward pass while SM-2 does fixed arithmetic. In practice the per-review
cost gap is too small to matter at any realistic review rate; the more
important throughput lever is *how many reviews the algorithm requires*.

Empirically:

- SM-2 inference cost: dozens of nanoseconds.
- FSRS inference cost: a few microseconds (still much faster than the
  SQLite write that records the review).
- FSRS schedules **20–30 % fewer reviews** than SM-2 at equivalent
  retention targets. This compounds over months of study.

So for a high-throughput review session:

- The scheduler call is not on the critical path. Disk I/O and UI latency
  dominate.
- The total *work to reach the same retention* is lower under FSRS even
  though each individual scheduler call costs slightly more.

The two-phase split (online scheduling, offline optimisation) keeps the
expensive component out of the request path entirely. Concurrency at the
scheduler boundary stays trivially parallelisable across cards.
