---
url: https://github.com/open-spaced-repetition/fsrs-optimizer
title: FSRS Optimizer — Offline Parameter Fitting
tags: [fsrs, optimizer, training]
---

# FSRS Optimizer

The FSRS optimizer is a separate pipeline from the online scheduler. It
takes a user's accumulated review log and fits the 17 weights of the
recall-prediction model.

Concretely, the optimizer:

1. Loads review logs (one row per (card, timestamp, rating)).
2. Splits into training/validation windows.
3. Runs gradient descent (Adam) over the 17 weights minimising log-loss on
   the user's recall outcomes.
4. Emits a parameter blob the scheduler loads at next launch.

FSRS-6 (released late 2025) shipped its default weights fit on a public
training set of about 700 million reviews from roughly 20 000 anonymised
Anki users. End users can also re-optimise on their own history.

Two important consequences for system design:

- The optimizer is the only "expensive" FSRS component. Per-review inference
  is cheap; optimisation is occasional (weekly is typical) and offline.
- The two-phase split — online scheduling (cheap, per card) vs offline
  optimisation (batch, all reviews) — is what makes FSRS deployable at
  multi-user scale without a queue around the scheduler.
