"""One-shot regression demo: confirm the Bloom-diversity guard fires red.

Not part of the normal eval suite — marked `eval_regression` so it only
runs when explicitly invoked. Used to satisfy §10.4 of the course-testing
change (intentionally regress to a single Bloom level, confirm the eval
catches it).

Run with:
    pytest tests/eval/test_regression_demo.py -m eval_regression -s

Costs roughly the same as the standard eval run (one full
generate_course end-to-end against the fixture corpus). Designed to
*expect failure*: a green result here means the regression test itself
is broken (eval too lenient).
"""
from __future__ import annotations

import pytest

import courses
import courses_db

from tests.eval.test_course_quality import (  # reuse shared helpers
    SEED,
    real_anthropic_tracked,  # noqa: F401  (re-export so pytest discovers the fixture)
    fixture_retrieval,        # noqa: F401
)

pytestmark = pytest.mark.eval_regression


async def test_single_bloom_regression_fires_red(
    monkeypatch,
    mem_courses_db,
    real_anthropic_tracked,
    fixture_retrieval,
):
    """Force the plan to land on a single Bloom level; assert the eval catches it.

    Mechanism: shrink `courses.BLOOM_LEVELS` to {"understand"}. The post-plan
    sanity loop in `_plan_course` then coerces every non-`understand` tag to
    None, so the persisted lessons end up with bloom_level either
    `"understand"` or NULL. The Bloom-diversity assertion expects ≥ 2
    distinct non-None levels.
    """
    monkeypatch.setattr(courses, "BLOOM_LEVELS", {"understand"})

    cid = courses_db.insert_course(
        title="(pending)", objective=SEED, scope={"query_seed": SEED}, status="pending",
    )
    await courses.generate_course(cid, SEED, scope={"query_seed": SEED})
    course = courses_db.get_course(cid)
    assert course["status"] == "draft", f"course failed: {course.get('error')}"

    bloom_levels = {l.get("bloom_level") for l in course["lessons"] if l.get("bloom_level")}
    print(
        f"\n[regression] lessons={len(course['lessons'])} "
        f"non_null_blooms={bloom_levels}"
    )

    # The eval suite asserts len(bloom_levels) >= 2 — we expect that to fail.
    assert len(bloom_levels) < 2, (
        f"regression FAILED to regress: got Bloom diversity {bloom_levels}, "
        f"len={len(bloom_levels)}. Either the plan ignored our BLOOM_LEVELS "
        f"clamp or the test setup is wrong."
    )
