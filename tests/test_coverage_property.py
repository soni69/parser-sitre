"""Property-based tests for ``coverage.CoverageTracker``.

**Validates: Requirements 4.1, 4.2, 5.4**

The tracker is a state machine over plain dicts (see design.md section 6),
so Hypothesis generates arbitrary sequences of ``record_discovered`` /
``record_after_dedup`` / ``record_failure`` calls and we check that
``finalize`` faithfully aggregates the events into the per-brand report.
"""

from __future__ import annotations

from collections import defaultdict

import hypothesis.strategies as st
from hypothesis import given, settings

from coverage import CoverageTracker

# A small fixed pool of brand strings (mixed case included) ensures the
# generated ops collide on the same slug often enough to exercise the
# accumulation paths and the lower-casing in ``CoverageTracker._get``.
BRANDS = ["lg", "samsung", "sony", "tcl", "LG", "Samsung", "TCL", "Daewoo-Electronics"]
SOURCES = ["sitemap", "brand_page", "sub_page"]
KINDS = ["404", "5xx", "network", "parse"]

brand_st = st.sampled_from(BRANDS)
source_st = st.sampled_from(SOURCES)
kind_st = st.sampled_from(KINDS)
count_st = st.integers(min_value=0, max_value=1000)
url_st = st.text(
    min_size=1,
    max_size=40,
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
)
# Allow messages well past the 240-char truncation boundary so that the
# truncation branch is exercised on at least some examples.
message_st = st.text(min_size=0, max_size=400)

discovered_op_st = st.tuples(st.just("disc"), brand_st, source_st, count_st)
dedup_op_st = st.tuples(st.just("dedup"), brand_st, count_st)
failure_op_st = st.tuples(st.just("fail"), brand_st, url_st, kind_st, message_st)
op_st = st.one_of(discovered_op_st, dedup_op_st, failure_op_st)


class _StubStorage:
    """Deterministic ``count_saved_for_brand`` for property-test stubbing."""

    def count_saved_for_brand(self, slug: str) -> int:
        # ``hash`` is stable within a single Python process, which is all
        # the test needs: ``finalize`` calls this once per brand and we
        # re-call it from the assertions in the same process.
        return abs(hash(("saved", slug))) % 100


@given(ops=st.lists(op_st, max_size=80))
@settings(max_examples=200, deadline=None)
def test_coverage_tracker_finalize_matches_recorded_events(ops):
    """Property 6: finalize sums recorded events into a well-formed report.

    **Validates: Requirements 4.1, 4.2, 5.4**
    """
    tracker = CoverageTracker(run_id="prop-test")

    # Build the expected state by replaying the ops in Python.
    expected_discovered: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    expected_after_dedup: dict[str, int] = {}
    expected_failures: dict[str, list[dict[str, str]]] = defaultdict(list)
    mentioned: set[str] = set()

    for op in ops:
        tag = op[0]
        if tag == "disc":
            _, brand, source, count = op
            tracker.record_discovered(brand, source, count)
            slug = brand.lower()
            mentioned.add(slug)
            # The implementation creates the source key even when count==0,
            # so accumulate with ``+=`` against a defaultdict to mirror that.
            expected_discovered[slug][source] += count
        elif tag == "dedup":
            _, brand, count = op
            tracker.record_after_dedup(brand, count)
            slug = brand.lower()
            mentioned.add(slug)
            # ``record_after_dedup`` overwrites — keep only the last value.
            expected_after_dedup[slug] = count
        else:  # "fail"
            _, brand, url, kind, message = op
            tracker.record_failure(brand, url, kind, message)
            slug = brand.lower()
            mentioned.add(slug)
            # Messages longer than 240 chars are truncated by record_failure.
            expected_failures[slug].append(
                {"url": url, "kind": kind, "message": message[:240]}
            )

    storage = _StubStorage()
    report = tracker.finalize(storage)

    # Report contains every brand mentioned in any call, keyed by lowercase slug.
    assert set(report.keys()) == mentioned

    for slug in mentioned:
        entry = report[slug]

        # discovered[source] equals the sum of recorded counts for that source.
        assert entry["discovered"] == dict(expected_discovered[slug])

        # discovered_total is the total across sources.
        expected_total = sum(expected_discovered[slug].values())
        assert entry["discovered_total"] == expected_total

        # after_dedup equals the last value recorded (or 0 if never recorded).
        assert entry["after_dedup"] == expected_after_dedup.get(slug, 0)

        # saved equals the stub storage's return value for this slug.
        assert entry["saved"] == storage.count_saved_for_brand(slug)

        # diff == discovered_total - saved.
        assert entry["diff"] == entry["discovered_total"] - entry["saved"]

        # failures is the in-order list of recorded failure dicts.
        assert entry["failures"] == expected_failures.get(slug, [])
