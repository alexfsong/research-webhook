---
url: https://github.com/ankitects/anki/tree/main/rslib
title: anki-rs — Rust Core for Anki
tags: [anki, rust, wasm]
---

# anki-rs

`rslib` (often referred to in third-party ports as `anki-rs`) is the Rust
core that backs modern Anki desktop, AnkiWeb, and AnkiDroid via FFI. It
ships:

- A scheduler library with both SM-2 and FSRS implementations.
- A storage layer over SQLite for cards, reviews, and decks.
- Bindings to Python (used by Anki desktop) and a WASM build (used by
  AnkiWeb).

Concurrency model:

- Per-card scheduling is a pure function of the card's persisted state and
  the new review rating. No global lock is needed on the scheduler itself.
- The bottleneck is the SQLite write that records the review. SQLite is
  single-writer; multi-user deployments (AnkiWeb) shard by user so each
  user's database accepts writes without contention with other users.

For high-throughput scenarios (a single user grinding through hundreds of
reviews per minute), the limiting factor is *not* FSRS inference but the
disk write per review. Batched reviews — accumulating N reviews then
committing — are the standard mitigation.
