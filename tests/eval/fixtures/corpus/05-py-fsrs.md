---
url: https://github.com/open-spaced-repetition/py-fsrs
title: py-fsrs — Reference Python Implementation
tags: [fsrs, python, reference]
---

# py-fsrs

`py-fsrs` is the reference implementation of the FSRS scheduler in
Python. It is small (a few hundred lines) and intentionally kept simple
so that the API surface clearly maps to the FSRS data model.

Core API:

```python
from fsrs import FSRS, Card, Rating

scheduler = FSRS()           # default weights
card = Card()                # new card; D, S, R all initialised
new_card, review_log = scheduler.review_card(card, Rating.Good)
```

Important boundaries:

- `scheduler.review_card` is a pure function of `(card, rating, now)` and
  returns a *new* card object. Persisting it is the caller's job.
- The scheduler holds the trained weights, not card state. A single
  scheduler instance can serve many cards concurrently without locking.
- The optimizer lives in a sibling package (`fsrs-optimizer`) — `py-fsrs`
  itself does not depend on PyTorch.

These boundaries make py-fsrs a useful proof of the FSRS architectural
claim: scheduler is stateless across cards, parameter update is offline.
A web service can hold one `FSRS()` per active model version and route
review calls through it without per-card synchronisation.
