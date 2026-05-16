---
url: https://docs.ankiweb.net/getting-started.html#card-states
title: Card State Machine — Concurrency Boundary
tags: [anki, state-machine, concurrency]
---

# Card State Machine

A card in any SRS implementation moves through a small state machine:

- **New** — never reviewed.
- **Learning** — in the initial fast-cycle (sub-day) phase.
- **Review** — long-interval phase, scheduler-managed.
- **Relearning** — re-entered after a lapse.

The state transition on a given review is a pure function of:

- The card's current state, latent variables (FSRS D/S/R or SM-2 ease
  factor), and last-review timestamp.
- The user-provided rating (Again / Hard / Good / Easy).
- The current scheduler weights.

Because the transition reads/writes one card row and consults globally
read-only data (the trained scheduler weights), per-card review updates
are **embarrassingly parallel**. The concurrency boundary in any
production SRS is not the algorithm but the storage layer:

- Multi-user systems shard by user — each user's deck is its own
  single-writer SQLite (Anki) or row partition (cloud variants).
- Single-user clients batch writes to avoid touching disk per review.

This separation — scheduler is stateless across cards, storage is the
single-writer bottleneck — applies equally to SM-2, FSRS, and any future
scheduler that fits the same shape.
