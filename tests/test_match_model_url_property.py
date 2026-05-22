"""Property-based test for ``utils.match_model_url``.

**Validates: Requirements 2.1, 2.2, 2.3**

When the brand slug ``b`` is present in ``known_brand_slugs``, parsing
``/remont-tv-lcd/{b}-{m}`` must round-trip back to ``(b, m)``, even when
shorter prefixes of ``b`` are also present in ``known_brand_slugs``.
``match_model_url`` resolves this ambiguity by preferring the longest
matching known slug, so ``b`` itself always wins over any of its prefixes
(see design.md section 3 — "Brand-slug-aware URL matcher").
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from utils import match_model_url


# Brand slug regex from Requirement 2.1: ``[a-z0-9][a-z0-9-]*``.
# We additionally constrain the slug NOT to end with a hyphen — task 2.2
# notes this is needed to avoid edge cases where ``b + '-' + m`` parses
# ambiguously (a trailing-hyphen brand glued to ``-m`` produces ``--``,
# which is technically valid for the regex but not a real URL shape).
brand_slug_st = (
    st.from_regex(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", fullmatch=True)
    .filter(lambda s: 1 <= len(s) <= 20)
)

# Model slug regex from ``utils._MODEL_SLUG_RE``: ``[a-z0-9][a-z0-9_-]*``.
# Same trailing-hyphen constraint as above (allow ``_`` or alphanumerics
# at the end).
model_slug_st = (
    st.from_regex(r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9_])?", fullmatch=True)
    .filter(lambda s: 1 <= len(s) <= 20)
)


def _proper_prefixes(s: str) -> list[str]:
    """All non-empty proper prefixes of ``s`` (excluding ``s`` itself)."""
    return [s[:i] for i in range(1, len(s))]


@st.composite
def brand_model_with_prefix_slugs(draw):
    """Draw ``(b, m, extra_prefixes)`` where ``extra_prefixes ⊆ proper_prefixes(b)``.

    The extra slugs simulate the realistic case where a hyphenated brand
    (``tcl-rowa``) coexists with a shorter known brand (``tcl``) — the
    matcher must still attribute ``/remont-tv-lcd/tcl-rowa-32led`` to
    ``tcl-rowa``, not ``tcl``.
    """
    b = draw(brand_slug_st)
    m = draw(model_slug_st)
    prefixes = _proper_prefixes(b)
    if prefixes:
        extra = draw(st.lists(st.sampled_from(prefixes), max_size=5, unique=True))
    else:
        extra = []
    return b, m, extra


@given(data=brand_model_with_prefix_slugs())
@settings(max_examples=200, deadline=None)
def test_match_model_url_roundtrip_when_brand_is_known(data):
    """Property 3: ``match_model_url`` round-trips known-brand URLs.

    For any brand slug ``b`` matching ``[a-z0-9][a-z0-9-]*`` (not ending
    with a hyphen) and any model slug ``m`` matching ``[a-z0-9][a-z0-9_-]*``
    (not ending with a hyphen), if ``b ∈ known_brand_slugs`` then
    ``match_model_url(f"/remont-tv-lcd/{b}-{m}", known_brand_slugs)``
    returns exactly ``(b, m)`` — even when arbitrary proper prefixes of
    ``b`` are also present in ``known_brand_slugs``.

    **Validates: Requirements 2.1, 2.2, 2.3**
    """
    b, m, extra_prefixes = data
    known = {b, *extra_prefixes}
    path = f"/remont-tv-lcd/{b}-{m}"

    assert match_model_url(path, known) == (b, m)
