"""Property-based test for ``discovery.collect_brand_subpages``.

**Validates: Requirements 1.4, 1.5, 7.5**

Hypothesis generates small synthetic site graphs in which each node is tagged
as one of:

- ``in_scope``    — a Brand_Page Sub_Page (``/remont-tv-lcd/lg/sub-N/``)
- ``out_of_scope`` — either another brand's path or an unrelated section
- ``model_page``  — a Model_Page (``/remont-tv-lcd/lg-modelN``); leaves only

A fake :data:`discovery.FetchHtml` returns each node's HTML, listing every
child via ``<a href>`` tags. We then assert that ``collect_brand_subpages``
visits **exactly** the in-scope nodes reachable from the root, never a
Model_Page or out-of-scope page, stays within the depth and count caps, and
never visits the same canonical key twice (design.md sections 1 and 5).
"""

from __future__ import annotations

import asyncio
from collections import deque
from urllib.parse import urlparse

import hypothesis.strategies as st
from hypothesis import given, settings

from config import MAX_BRAND_DEPTH, MAX_BRAND_SUBPAGES
from discovery import collect_brand_subpages
from utils import canonical_path

BRAND_SLUG = "lg"
BASE_HOST = "https://tel-spb.ru"


def _visited_key(url: str) -> str:
    """Mirror :func:`discovery._visited_key` — must stay in sync with production."""
    parsed = urlparse(url)
    return f"{canonical_path(url)}?{parsed.query}"


def _make_url(kind: str, idx: int, variant: int) -> str:
    """Build a unique URL per (kind, idx).

    - ``in_scope``    → ``/remont-tv-lcd/lg/sub-{idx}/``
    - ``out_of_scope`` → either ``/remont-tv-lcd/other/sub-{idx}/`` or
                          ``/some-section/{idx}/`` (chosen by ``variant``)
    - ``model_page``  → ``/remont-tv-lcd/lg-model{idx}``
    """
    if kind == "in_scope":
        return f"{BASE_HOST}/remont-tv-lcd/{BRAND_SLUG}/sub-{idx}/"
    if kind == "out_of_scope":
        if variant == 0:
            return f"{BASE_HOST}/remont-tv-lcd/other/sub-{idx}/"
        return f"{BASE_HOST}/some-section/{idx}/"
    # model_page
    return f"{BASE_HOST}/remont-tv-lcd/{BRAND_SLUG}-model{idx}"


@st.composite
def site_graphs(draw):
    """Draw a small synthetic site graph plus an in-scope root index.

    Returns ``(nodes, root_url)`` where ``nodes`` is a list of
    ``{"url", "kind", "index", "children"}`` dicts and ``root_url`` is one
    of the in-scope node URLs.
    """
    n = draw(st.integers(min_value=1, max_value=8))
    kinds = draw(
        st.lists(
            st.sampled_from(["in_scope", "out_of_scope", "model_page"]),
            min_size=n,
            max_size=n,
        )
    )
    variants = draw(
        st.lists(st.integers(min_value=0, max_value=1), min_size=n, max_size=n)
    )

    # Force at least one in-scope node so we always have a valid root.
    if "in_scope" not in kinds:
        kinds[0] = "in_scope"

    nodes = []
    for i in range(n):
        nodes.append(
            {
                "url": _make_url(kinds[i], i, variants[i]),
                "kind": kinds[i],
                "index": i,
                "children": [],  # filled below
            }
        )

    all_urls = [node["url"] for node in nodes]
    for node in nodes:
        child_indices = draw(
            st.lists(
                st.integers(min_value=0, max_value=n - 1),
                max_size=4,
                unique=True,
            )
        )
        node["children"] = [all_urls[c] for c in child_indices]

    in_scope_indices = [i for i, k in enumerate(kinds) if k == "in_scope"]
    root_idx = draw(st.sampled_from(in_scope_indices))
    return nodes, nodes[root_idx]["url"]


def _build_html(children_urls):
    anchors = "".join(f'<a href="{url}">link</a>' for url in children_urls)
    return f"<html><body>{anchors}</body></html>"


def _expected_reachable_in_scope(nodes_by_url, root_url, max_depth):
    """Compute the BFS-reachable in-scope set from ``root_url``.

    Mirrors the gating in :func:`collect_brand_subpages`: only in-scope
    children at depth strictly less than ``max_depth`` are enqueued, since
    out-of-scope and Model_Page nodes are never fetched and therefore can
    never extend the frontier.
    """
    result = {root_url}
    visited_keys = {_visited_key(root_url)}
    queue: deque = deque([(root_url, 0)])
    while queue:
        url, depth = queue.popleft()
        node = nodes_by_url.get(url)
        if node is None:
            continue
        for child_url in node["children"]:
            child = nodes_by_url.get(child_url)
            if child is None:
                continue
            child_depth = depth + 1
            if child_depth >= max_depth:
                continue
            if child["kind"] != "in_scope":
                continue
            child_key = _visited_key(child_url)
            if child_key in visited_keys:
                continue
            visited_keys.add(child_key)
            result.add(child_url)
            queue.append((child_url, child_depth))
    return result


def _shortest_in_scope_depth(url, root_url, nodes_by_url):
    """Shortest-path depth from ``root_url`` to ``url`` over in-scope nodes only."""
    if url == root_url:
        return 0
    visited = {root_url}
    queue: deque = deque([(root_url, 0)])
    while queue:
        u, d = queue.popleft()
        node = nodes_by_url.get(u)
        if node is None:
            continue
        for c in node["children"]:
            child = nodes_by_url.get(c)
            if child is None:
                continue
            if child["kind"] != "in_scope":
                continue
            if c in visited:
                continue
            if c == url:
                return d + 1
            visited.add(c)
            queue.append((c, d + 1))
    return None


@given(graph=site_graphs())
@settings(max_examples=100, deadline=None)
def test_collect_brand_subpages_returns_exactly_reachable_in_scope(graph):
    """Property 2: BFS visits exactly the reachable, in-scope sub-pages.

    For any small synthetic site graph rooted at an in-scope Brand_Page,
    ``collect_brand_subpages`` returns the set of in-scope sub-pages reachable
    from the root, never a Model_Page or out-of-scope page, terminates within
    ``MAX_BRAND_SUBPAGES`` visits and ``MAX_BRAND_DEPTH`` levels, and never
    visits the same canonical key twice.

    **Validates: Requirements 1.4, 1.5, 7.5**
    """
    nodes, root_url = graph
    nodes_by_url = {node["url"]: node for node in nodes}

    url_to_html = {
        node["url"]: _build_html(node["children"]) for node in nodes
    }

    fetch_log: list[str] = []

    async def fake_fetch(url: str) -> str:
        fetch_log.append(url)
        return url_to_html.get(url, "")

    result = asyncio.run(
        collect_brand_subpages(fake_fetch, BRAND_SLUG, root_url)
    )

    # 1. The returned set equals the in-scope reachable set from the root.
    expected = _expected_reachable_in_scope(nodes_by_url, root_url, MAX_BRAND_DEPTH)
    assert result == expected, (
        f"Result mismatch:\n  got     ={sorted(result)}\n  expected={sorted(expected)}"
    )

    # 2. No Model_Page or out-of-scope URL appears in the result.
    for url in result:
        node = nodes_by_url.get(url)
        assert node is not None, f"Result contains an unknown URL: {url!r}"
        assert node["kind"] == "in_scope", (
            f"Result contains a non-in-scope URL: {url!r} (kind={node['kind']!r})"
        )

    # 3. Total visits stay within the count cap.
    assert len(result) <= MAX_BRAND_SUBPAGES, (
        f"Result size {len(result)} exceeds MAX_BRAND_SUBPAGES={MAX_BRAND_SUBPAGES}"
    )

    # 4. Depth of every returned URL ≤ MAX_BRAND_DEPTH.
    for url in result:
        depth = _shortest_in_scope_depth(url, root_url, nodes_by_url)
        assert depth is not None, (
            f"Returned URL is not reachable from the root over in-scope edges: {url!r}"
        )
        assert depth <= MAX_BRAND_DEPTH, (
            f"Depth {depth} exceeds MAX_BRAND_DEPTH={MAX_BRAND_DEPTH} for {url!r}"
        )

    # 5. The fake fetch was called at most once per canonical key.
    seen_keys: set[str] = set()
    for url in fetch_log:
        key = _visited_key(url)
        assert key not in seen_keys, (
            f"Canonical key fetched more than once: {key!r} (url={url!r})"
        )
        seen_keys.add(key)
