"""Property-based test for the composite-key dedup in ``main._dedup_key``.

**Validates: Requirements 3.1, 3.3, 3.4**

Discovery merges Model_Refs from three sources (sitemap, brand pages,
brand sub-page BFS) into a single per-run list. Before the persistence
layer sees them, ``main.run_scraper`` collapses duplicates with the
composite key

    (brand.lower(), normalize_model_name(model_name), canonical_path(url))

via the helper :func:`main._dedup_key`. The properties below assert the
universal invariants this dedup must satisfy:

  * **Cardinality (3.4)** — the deduped list contains exactly one ref per
    distinct composite-key triple, so its length equals the number of
    distinct triples in the input.
  * **Coverage (3.4)** — every triple present in the input appears in the
    deduped output exactly once; no triple is dropped or duplicated.
  * **Discriminating coordinates (3.1, 3.3)** — refs that differ in *any*
    single coordinate (brand, normalised model name, canonical path) must
    both survive dedup. URL collisions alone do not collapse refs.

The dedup logic in :func:`main.run_scraper` keeps the **last** ref
encountered per key (``unique[_dedup_key(ref)] = ref``); this test
mirrors that exactly so it stays in lock-step with production.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import assume, given, settings

from main import DedupKey, _dedup_key
from models import ModelRef
from utils import canonical_path, normalize_model_name


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Brand pool with mixed casing — the dedup key lower-cases brands, so
# refs that only differ by case must collapse to the same triple.
BRAND_POOL: tuple[str, ...] = (
    "LG",
    "lg",
    "Lg",
    "samsung",
    "Samsung",
    "SAMSUNG",
    "tcl-rowa",
    "TCL-Rowa",
    "Sony",
    "sony",
)

# Base model names with no internal whitespace; whitespace variants are
# layered on top via the ``model_name_st`` strategy below.
MODEL_BASE_POOL: tuple[str, ...] = (
    "32LED",
    "32led",
    "ABC-123",
    "abc-123",
    "Model X",
    "model x",
    "OLED-55C1",
    "QN90A",
    "Bravia 4K",
)

# Base URL paths used for refs. ``canonical_path`` lower-cases and strips
# trailing slashes, so these stay equivalent under normalisation while
# the surrounding URL components (scheme, query) vary.
URL_PATH_POOL: tuple[str, ...] = (
    "/remont-tv-lcd/lg-32led",
    "/remont-tv-lcd/LG-32LED",
    "/remont-tv-lcd/samsung-qn90a",
    "/remont-tv-lcd/sony-bravia-4k",
    "/remont-tv-lcd/tcl-rowa-32led",
)

QUERY_POOL: tuple[str, ...] = (
    "",
    "?utm=foo",
    "?ref=bar",
    "?page=2",
    "?utm=foo&ref=bar",
)

WHITESPACE_PADDINGS: tuple[str, ...] = ("", " ", "  ", "\t", " \t ", "\n")


brand_st = st.sampled_from(BRAND_POOL)


@st.composite
def model_name_st(draw) -> str:
    """Pick a base model name and decorate it with arbitrary whitespace.

    ``normalize_model_name`` collapses any ``\\s+`` run to a single space
    and strips, so whitespace decorations should not change the dedup
    key — but they CAN produce a distinct key if they alter the
    underlying tokens (e.g. ``"32LED"`` vs ``"32 LED"``).
    """
    base = draw(st.sampled_from(MODEL_BASE_POOL))
    leading = draw(st.sampled_from(WHITESPACE_PADDINGS))
    trailing = draw(st.sampled_from(WHITESPACE_PADDINGS))
    # Optionally inject extra whitespace inside an existing space.
    if " " in base and draw(st.booleans()):
        injected = draw(st.sampled_from(WHITESPACE_PADDINGS[1:]))
        base = base.replace(" ", " " + injected, 1)
    return f"{leading}{base}{trailing}"


@st.composite
def url_st(draw) -> str:
    """Build a URL by combining a known path with an optional query."""
    path = draw(st.sampled_from(URL_PATH_POOL))
    query = draw(st.sampled_from(QUERY_POOL))
    trailing_slash = draw(st.booleans())
    if trailing_slash and not path.endswith("/"):
        path = path + "/"
    return f"https://tel-spb.ru{path}{query}"


@st.composite
def model_ref_st(draw) -> ModelRef:
    return ModelRef(
        brand=draw(brand_st),
        model_name=draw(model_name_st()),
        url=draw(url_st()),
    )


def _dedup(refs: list[ModelRef]) -> list[ModelRef]:
    """Mirror the dedup loop in :func:`main.run_scraper`.

    Keeps the LAST ref per composite key, matching the production
    semantics exactly so we are testing the same shape of dedup.
    """
    unique: dict[DedupKey, ModelRef] = {}
    for ref in refs:
        unique[_dedup_key(ref)] = ref
    return list(unique.values())


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@given(refs=st.lists(model_ref_st(), max_size=30))
@settings(max_examples=200, deadline=None)
def test_dedup_keeps_one_ref_per_composite_key(refs: list[ModelRef]) -> None:
    """Property 5a: deduped length equals the number of distinct triples.

    For any list of Model_Refs, the deduped output's length must equal
    the number of distinct ``(brand.lower(), normalize_model_name(name),
    canonical_path(url))`` triples in the input. This is the cardinality
    side of Requirement 3.4.

    **Validates: Requirements 3.1, 3.4**
    """
    deduped = _dedup(refs)
    distinct_keys = {_dedup_key(r) for r in refs}
    assert len(deduped) == len(distinct_keys)


@given(refs=st.lists(model_ref_st(), max_size=30))
@settings(max_examples=200, deadline=None)
def test_dedup_every_triple_appears_exactly_once(refs: list[ModelRef]) -> None:
    """Property 5b: every input triple appears exactly once in the output.

    The set of dedup keys produced by the output must equal the set of
    dedup keys produced by the input (no triple dropped, no triple
    invented), and within the output every key occurs exactly once
    (no duplicates).

    **Validates: Requirement 3.4**
    """
    deduped = _dedup(refs)
    input_keys = {_dedup_key(r) for r in refs}
    output_keys_list = [_dedup_key(r) for r in deduped]
    output_keys = set(output_keys_list)

    assert output_keys == input_keys
    assert len(output_keys_list) == len(output_keys)


@given(a=model_ref_st(), b=model_ref_st())
@settings(max_examples=200, deadline=None)
def test_dedup_retains_refs_differing_in_any_single_coordinate(
    a: ModelRef, b: ModelRef
) -> None:
    """Property 5c: refs differing in ANY one coordinate are both retained.

    Two Model_Refs are equivalent under dedup iff their full composite
    triples coincide. If they differ in *any* of brand-lc, normalised
    model name, or canonical path, both must survive — Requirement 3.3
    explicitly forbids URL-only collisions from collapsing distinct
    brand/model pairs.

    **Validates: Requirements 3.1, 3.3**
    """
    key_a = _dedup_key(a)
    key_b = _dedup_key(b)
    assume(key_a != key_b)

    deduped = _dedup([a, b])
    output_keys = {_dedup_key(r) for r in deduped}

    assert key_a in output_keys
    assert key_b in output_keys
    assert len(deduped) == 2


@given(
    brand=brand_st,
    model_name=model_name_st(),
    url=url_st(),
    other_brand=brand_st,
    other_model=model_name_st(),
    other_url=url_st(),
)
@settings(max_examples=200, deadline=None)
def test_dedup_url_only_collision_keeps_distinct_brand_or_model(
    brand: str,
    model_name: str,
    url: str,
    other_brand: str,
    other_model: str,
    other_url: str,
) -> None:
    """Property 5d (Requirement 3.3 focus): same canonical URL, distinct
    brand or normalised model_name → both refs survive.

    We force the canonical paths of the two refs to coincide and require
    that at least one of the other two coordinates differs. Under the
    composite key, the two refs must therefore land in distinct buckets
    and both be retained — the legacy URL-only dedup would have dropped
    one of them.

    **Validates: Requirement 3.3**
    """
    # Force matching canonical paths by reusing ``url`` for both refs
    # (any trailing slash / case differences vanish under canonical_path).
    ref_a = ModelRef(brand=brand, model_name=model_name, url=url)
    ref_b = ModelRef(brand=other_brand, model_name=other_model, url=url)

    assume(canonical_path(ref_a.url) == canonical_path(ref_b.url))
    assume(
        ref_a.brand.lower() != ref_b.brand.lower()
        or normalize_model_name(ref_a.model_name)
        != normalize_model_name(ref_b.model_name)
    )

    deduped = _dedup([ref_a, ref_b])
    output_keys = {_dedup_key(r) for r in deduped}
    assert _dedup_key(ref_a) in output_keys
    assert _dedup_key(ref_b) in output_keys
    assert len(deduped) == 2
