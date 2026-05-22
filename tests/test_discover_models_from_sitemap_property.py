"""Property-based test for ``discovery.discover_models_from_sitemap``.

**Validates: Requirements 1.1, 1.2**

Hypothesis generates synthetic sitemap trees rooted at :data:`config.SITEMAP_URL`.
Each non-leaf node is a ``<sitemapindex>`` that points at child sitemap URLs;
each leaf node is a ``<urlset>`` containing an arbitrary mix of Model_Page and
non-Model_Page ``<loc>`` entries. A dict-keyed fake :data:`discovery.FetchHtml`
returns each sitemap document by URL.

We then assert that ``discover_models_from_sitemap`` returns a ``ModelRef`` for
**every** Model_Page URL present in the tree and for **none** of the others,
with no duplicates by canonical URL, and that every returned ref's
``brand`` / ``model_name`` matches what :func:`utils.match_model_url` would
parse from the URL (design.md section 1, requirements 1.1 and 1.2).

Tree generation is capped tightly (``≤ 1`` index level above the leaves, fan-out
``≤ 3``, ``≤ 4`` URLs per ``<urlset>``) so Hypothesis generation does not
explode at ``max_examples=100``.
"""

from __future__ import annotations

import asyncio
import itertools
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import hypothesis.strategies as st
from hypothesis import given, settings

import config
from discovery import discover_models_from_sitemap
from utils import canonical_path, match_model_url

# Sitemap protocol namespace (https://www.sitemaps.org/protocol.html).
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
# Make ElementTree emit ``xmlns="…"`` instead of ``ns0:`` prefixes.
ET.register_namespace("", SITEMAP_NS)


# Brand slugs chosen so none is a prefix of another. This removes the
# ambiguity ``match_model_url`` resolves via "longest known slug wins":
# the test does not exercise that resolution path here (it is covered by
# `test_match_model_url_property.py`), so we keep the search space simple.
BRAND_POOL: tuple[str, ...] = (
    "lg",
    "samsung",
    "sony",
    "philips",
    "tcl-rowa",
    "akai-2",
)

# Non-Model leaf paths. Every entry is guaranteed not to match the
# Model_Page pattern of ``utils.match_model_url`` (verified by inspection):
# they either do not start with ``/remont-tv-lcd/``, have an empty tail
# after that prefix, or contain a ``/`` after the brand slug.
NON_MODEL_PATHS: tuple[str, ...] = (
    "/",
    "/about",
    "/contacts",
    "/blog/article-1",
    "/some/other/path",
    "/remont-tv-lcd/",
    "/remont-tv-lcd/lg/",
    "/remont-tv-lcd/samsung/led",
    "/remont-tv-lcd/sony/year/2020/",
    "/news/2024/some-news",
)

# Model slug regex from ``utils._MODEL_SLUG_RE``: ``[a-z0-9][a-z0-9_-]*``.
# Constrain not to end with ``-`` so concatenation with the brand slug
# never produces a ``--`` separator that ``match_model_url`` would refuse.
model_slug_st = (
    st.from_regex(r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9_])?", fullmatch=True)
    .filter(lambda s: 1 <= len(s) <= 10)
)


@st.composite
def leaf_url(draw):
    """Draw a leaf ``<loc>`` entry tagged as Model_Page or non-Model_Page."""
    is_model = draw(st.booleans())
    if is_model:
        brand = draw(st.sampled_from(BRAND_POOL))
        model = draw(model_slug_st)
        url = f"https://tel-spb.ru/remont-tv-lcd/{brand}-{model}"
        return {"url": url, "kind": "model", "brand": brand, "model": model}
    path = draw(st.sampled_from(NON_MODEL_PATHS))
    return {"url": f"https://tel-spb.ru{path}", "kind": "non_model"}


def _sitemap_node_strategy(remaining_depth: int):
    """Build a sitemap-tree strategy with bounded fan-out and depth.

    - ``urlset`` nodes carry up to 4 leaf URLs.
    - ``sitemapindex`` nodes carry up to 3 child sitemap nodes and reduce
      ``remaining_depth`` by one for the recursive call.
    - At ``remaining_depth == 0`` only ``urlset`` is emitted.
    """
    urlset = st.builds(
        lambda leaves: {"type": "urlset", "leaves": leaves},
        st.lists(leaf_url(), max_size=4),
    )
    if remaining_depth <= 0:
        return urlset
    sitemapindex = st.builds(
        lambda kids: {"type": "sitemapindex", "children": kids},
        st.lists(_sitemap_node_strategy(remaining_depth - 1), max_size=3),
    )
    return st.one_of(urlset, sitemapindex)


# Two index levels above the urlsets is enough to exercise BFS recursion
# while keeping the worst-case tree small (1 + 3 + 9 = 13 sitemap nodes).
sitemap_tree_st = _sitemap_node_strategy(remaining_depth=2)


def _build_urlset_xml(leaves: list[dict]) -> str:
    root = ET.Element(f"{{{SITEMAP_NS}}}urlset")
    for leaf in leaves:
        url_el = ET.SubElement(root, f"{{{SITEMAP_NS}}}url")
        loc_el = ET.SubElement(url_el, f"{{{SITEMAP_NS}}}loc")
        loc_el.text = leaf["url"]
    return ET.tostring(root, encoding="unicode")


def _build_sitemapindex_xml(child_urls: list[str]) -> str:
    root = ET.Element(f"{{{SITEMAP_NS}}}sitemapindex")
    for child_url in child_urls:
        entry = ET.SubElement(root, f"{{{SITEMAP_NS}}}sitemap")
        loc_el = ET.SubElement(entry, f"{{{SITEMAP_NS}}}loc")
        loc_el.text = child_url
    return ET.tostring(root, encoding="unicode")


def _materialise_tree(tree: dict, root_sitemap_url: str):
    """Walk ``tree``, build the fetch dict and collect every leaf entry.

    Returns ``(fetch_dict, leaves)`` where:

    - ``fetch_dict`` maps every sitemap URL (including ``root_sitemap_url``)
      to its serialized XML payload, ready for the fake ``FetchHtml``.
    - ``leaves`` is the ordered list of every ``<loc>`` entry encountered
      across every ``<urlset>`` in the tree.
    """
    fetch_dict: dict[str, str] = {}
    leaves: list[dict] = []
    counter = itertools.count(1)

    def visit(node: dict, sitemap_url: str) -> None:
        if node["type"] == "urlset":
            fetch_dict[sitemap_url] = _build_urlset_xml(node["leaves"])
            leaves.extend(node["leaves"])
            return
        # sitemapindex
        child_urls: list[str] = []
        for child in node["children"]:
            child_url = f"https://tel-spb.ru/sitemap-{next(counter)}.xml"
            child_urls.append(child_url)
            visit(child, child_url)
        fetch_dict[sitemap_url] = _build_sitemapindex_xml(child_urls)

    visit(tree, root_sitemap_url)
    return fetch_dict, leaves


@given(tree=sitemap_tree_st)
@settings(max_examples=100, deadline=None)
def test_discover_models_from_sitemap_recovers_every_model_page(tree):
    """Property 1: Sitemap traversal recovers every Model_Page URL.

    For any sitemap tree (a ``<sitemapindex>`` recursively pointing at
    ``<urlset>`` leaves, with arbitrary fan-out and depth) where each leaf
    contains an arbitrary mix of Model_Page and non-Model_Page URLs,
    ``discover_models_from_sitemap`` returns a ``ModelRef`` for **every**
    Model_Page URL in the tree and for **none** of the others, with no
    duplicates.

    **Validates: Requirements 1.1, 1.2**
    """
    fetch_dict, leaves = _materialise_tree(tree, config.SITEMAP_URL)

    # Pre-compute ``known_brand_slugs`` from the brand slugs that actually
    # appear in generated Model_Page URLs (per task 4.2 instructions).
    known_brand_slugs: set[str] = {
        leaf["brand"] for leaf in leaves if leaf["kind"] == "model"
    }

    # Expected set: canonical URLs of every Model_Page leaf in the tree,
    # deduplicated (the implementation dedupes on canonical URL).
    expected_canonical: set[str] = {
        canonical_path(leaf["url"]) for leaf in leaves if leaf["kind"] == "model"
    }

    async def fake_fetch(url: str) -> str:
        # Any URL not in the dict means the BFS strayed off the generated
        # tree; returning empty makes ``discover_models_from_sitemap`` log
        # a warning and continue, which the assertions then catch via the
        # set-equality check.
        return fetch_dict.get(url, "")

    refs = asyncio.run(discover_models_from_sitemap(fake_fetch, known_brand_slugs))

    # 1. No duplicates by canonical URL in the returned list.
    canonical_urls = [canonical_path(ref.url) for ref in refs]
    assert len(canonical_urls) == len(set(canonical_urls)), (
        f"Duplicate refs by canonical URL: {canonical_urls}"
    )

    # 2. The set of returned canonical URLs equals the expected set.
    result_canonical = set(canonical_urls)
    assert result_canonical == expected_canonical, (
        "Mismatch between returned canonical URLs and expected Model_Page set:\n"
        f"  got     ={sorted(result_canonical)}\n"
        f"  expected={sorted(expected_canonical)}"
    )

    # 3. Every returned ref is parseable as a Model_Page (no non-Model URL
    #    leaks through), and 4. its brand / model_name match what
    #    ``match_model_url`` parses from the URL.
    for ref in refs:
        path = urlparse(ref.url).path
        parsed = match_model_url(path, known_brand_slugs)
        assert parsed is not None, (
            f"discover_models_from_sitemap returned a non-Model URL: {ref.url!r}"
        )
        brand, model = parsed
        assert ref.brand == brand, (
            f"ModelRef.brand mismatch for {ref.url!r}: "
            f"got {ref.brand!r}, expected {brand!r}"
        )
        assert ref.model_name == model, (
            f"ModelRef.model_name mismatch for {ref.url!r}: "
            f"got {ref.model_name!r}, expected {model!r}"
        )
