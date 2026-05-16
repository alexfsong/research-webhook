---
url: https://supermemo.guru/wiki/SuperMemo_2_algorithm
title: SM-2 — The SuperMemo 2 Baseline
tags: [sm2, supermemo, baseline]
---

# SM-2 (SuperMemo 2)

SM-2 is the rule-based spaced-repetition algorithm SuperMemo released in
1987 and the algorithm Anki shipped by default for two decades. Each card
keeps three integers:

- **EaseFactor** (default 2.5, floor 1.3).
- **Interval** in days.
- **Repetitions** since last lapse.

On each review the user rates 0–5. The update rule is pure arithmetic:

```
if rating < 3:
    repetitions = 0
    interval = 1
else:
    if repetitions == 0: interval = 1
    elif repetitions == 1: interval = 6
    else: interval = round(prev_interval * ease_factor)
    ease_factor = max(1.3, ease_factor + 0.1 - (5 - rating)*(0.08 + (5 - rating)*0.02))
    repetitions += 1
```

Properties relevant to scheduler design:

- O(1) per-review update, branch-free, no model inference.
- No global state — every card's update is independent.
- No notion of "current recall probability"; intervals grow geometrically
  regardless of how the actual recall curve behaves for that user.

SM-2 is the floor that FSRS and other modern schedulers are benchmarked
against. It is trivially concurrent but produces less efficient schedules
than model-based approaches.
