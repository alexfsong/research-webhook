---
url: https://github.com/open-spaced-repetition/fsrs4anki/wiki/ABC-of-FSRS
title: FSRS — Three-Variable Model
tags: [fsrs, algorithm, dsr]
---

# FSRS — Three-Variable Model

FSRS (Free Spaced Repetition Scheduler) replaces SM-2's single ease factor
with three latent per-card variables:

- **Difficulty** (1–10): intrinsic hardness of the item.
- **Stability**: number of days until the recall probability drops to 90 %.
- **Retrievability**: current probability the learner can recall the item.

Each review updates all three by passing through a small neural model with
17 trainable weights. The model is offline-trained from the user's review
history; per-review inference is a forward pass that runs in microseconds.

Compared to SM-2, FSRS:

- Produces calibrated retention curves, not just a heuristic interval.
- Allows the scheduler to target a user-chosen *desired retention* (e.g. 90 %).
- Reduces total review load 20–30 % at equivalent retention.

The three variables are stored per card and updated independently across
cards. A review session that touches many cards can be parallelised over
cards because no shared mutable state lives outside each card row.
