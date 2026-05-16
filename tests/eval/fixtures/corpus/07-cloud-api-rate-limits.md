---
url: https://www.supermemo.com/en/blog/programming-supermemo-api
title: Cloud Scheduling APIs — Concurrency Ceilings
tags: [supermemo, cloud, rate-limits]
---

# Cloud Scheduling APIs

Some spaced-repetition products expose their scheduler as a cloud API
rather than running it locally. The SuperMemo cloud API is the canonical
example.

Architecturally this puts the scheduler behind a hard concurrency ceiling
that has nothing to do with the algorithm's intrinsic complexity:

- Per-tenant request quotas (e.g. N requests per minute) cap throughput
  regardless of how many cards the client has queued.
- Network latency adds tens of milliseconds per review, dwarfing the
  microsecond-scale cost of the actual scheduling computation.
- The provider needs to scale a fleet of workers per user to absorb burst
  traffic; this is a far more expensive ops model than shipping the
  scheduler to the client.

Comparison to local schedulers (FSRS-in-rslib, py-fsrs, SM-2-in-Anki):

- Local: throughput limited by client CPU and disk; effectively unbounded
  for normal study volumes.
- Cloud: throughput limited by per-tenant rate limits; unbounded study
  volume requires raising the limit or batching reviews.

This is one reason FSRS adoption ran ahead of any cloud-scheduler product:
the algorithm ships as a library and runs on every client, so concurrency
scales with users without the provider holding any quota.
