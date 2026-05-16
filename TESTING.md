# Testing

The repo ships two test layers:

- **Unit (`tests/unit/`)** — hermetic. No network, no Anthropic spend, no
  LlamaIndex spin-up. Mocks the Anthropic client via `tests/conftest.py`
  and points `courses_db` at a per-test temp SQLite. Runs in <1 s.
- **Eval (`tests/eval/`)** — opt-in. Runs the real course-generation
  pipeline end-to-end against a checked-in fixture corpus, using the real
  Anthropic API. Costs API tokens; never runs by default.

## Install

Test deps are separate from runtime deps. From a venv:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # runtime
pip install -r requirements-dev.txt      # pytest + plugins
```

For unit-only work, you can skip the heavy runtime deps:

```bash
pip install fastapi pydantic httpx anthropic
pip install -r requirements-dev.txt
```

LlamaIndex, ChromaDB, sentence-transformers, and torch are only needed
for production runs and a small set of integration tests that touch
`llamaindex_store`. The unit suite mocks retrieval, so they are not
required to run it.

## Running the unit suite

```bash
pytest                                  # everything (eval auto-skips if no API key)
pytest tests/unit                       # just hermetic unit tests
pytest -m "not eval"                    # CI-equivalent invocation
pytest -m "not eval" \
    --disable-socket --allow-hosts=127.0.0.1   # hermetic check
```

The hermetic check is what CI runs in `.github/workflows/unit-tests.yml`.
It proves the unit suite never makes a real network call — if a future
test accidentally hits the real Anthropic API or downloads a model, the
socket guard will fail it red.

## Running the eval suite

```bash
ANTHROPIC_API_KEY=sk-ant-... pytest -m eval -s
```

`-s` keeps `print()` enabled so the token-usage line at the end of the
run is visible. Without `ANTHROPIC_API_KEY` the eval auto-skips, which
is what the default `pytest` invocation does on a developer laptop.

### Cost expectation

The eval runs one full course generation (plan + 3–7 lessons × body +
claims) plus one held-out judge call. Measured on the FSRS fixture
corpus, May 2026 (Sonnet 4.6 plan/lesson, Haiku 4.5 claims, Opus 4.7
judge):

- Course-gen tokens: ~25 k input + ~11 k output = ~37 k total across
  13 calls (1 plan + 6 lessons × {body, claims}). Well inside the
  default `EVAL_MAX_TOKENS=200_000` cap — only `_UsageTracker` totals
  count, judge tokens are separate.
- Wall time: ~220 s (Sonnet streams are the long pole).
- Cost: under $1 per run at current per-token pricing.

Run it before any prompt change to courses.py, model bumps in
`COURSE_PLAN_MODEL` / `COURSE_LESSON_MODEL` / `COURSE_CLAIMS_MODEL`, or
significant retrieval changes upstream. Do not put it in CI on every PR.

### Tuning knobs

| Env var | Default | Purpose |
|---------|---------|---------|
| `EVAL_JUDGE_MODEL` | `claude-opus-4-7` | Held-out scorer model; should not be the same as the generation models. |
| `EVAL_MAX_TOKENS` | `200000` | Hard cap across the full eval run; the test fails if exceeded. |
| `ANTHROPIC_API_KEY` | — | Required; eval skips without it. |

### Manual GitHub Actions trigger

`.github/workflows/eval.yml` is `workflow_dispatch`-only — kick it from
the Actions tab when you want a CI-equivalent run. It uses
`ANTHROPIC_API_KEY` from the repo's Actions secrets.

## Test layout

```
tests/
  conftest.py          # shared fixtures: mem_courses_db, mock_anthropic, …
  unit/
    test_courses_db.py # DAO round-trips, FK cascade, idempotent migration
    test_parsers.py    # _strip_code_fence, _parse_json, plan/claims payloads
    test_pipeline.py   # generate_course / regenerate_lesson / ask_lesson
  eval/
    fixtures/
      CORPUS.md        # provenance + topic doc for the fixture corpus
      corpus/*.md      # 8 short docs on FSRS / SM-2 / anki-rs
    test_course_quality.py  # end-to-end run with deterministic + LLM-as-judge checks
```

## Regenerating canned mock responses

When `courses.py` prompts change, the unit suite still passes because it
hands the pipeline canned JSON regardless of prompt text. Re-running the
eval suite is what catches prompt regressions. If a unit test starts
failing after a prompt change, it usually means the *output schema* the
prompt asks for has shifted; update the canned responses in
`tests/unit/test_parsers.py` / `tests/unit/test_pipeline.py` to match
the new shape and add a parser test for the new field.

## Adding a new test

- DAO change → extend `tests/unit/test_courses_db.py`. New tables need
  a round-trip + a cascade test. Keep tests synchronous unless they
  exercise async DAO.
- Prompt change → update parser tests if the response *shape* changed;
  always run the eval suite once before merging.
- New pipeline branch → add a `test_pipeline.py` case that drives the
  branch through `mock_anthropic.queue(...)` and asserts on the
  persisted state, not on internal call counts where possible.
