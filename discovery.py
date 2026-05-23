"""Discovery helpers for sitemap- and brand-page-driven traversal.

This module is consumed by ``scraper.collect_model_refs`` and exposes pure
async functions that take a ``FetchHtml`` callable so retries, polite delays,
and shutdown handling stay in ``TelSpbScraper.fetch_html``.

See `design.md` sections 1 and 5 for the full specification.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from config import BASE_URL, MAX_BRAND_DEPTH, MAX_BRAND_SUBPAGES, SITEMAP_URL
from models import ModelRef
from utils import canonical_path, match_model_url

# A fetch callable that returns the raw HTML/XML text for a URL or raises.
FetchHtml = Callable[[str], Awaitable[str]]

logger = logging.getLogger("telspb_scraper")

# XML namespace used by sitemaps.org sitemaps.
_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_SITEMAPINDEX_TAG = f"{{{_SITEMAP_NS}}}sitemapindex"
_URLSET_TAG = f"{{{_SITEMAP_NS}}}urlset"
_LOC_TAG = f"{{{_SITEMAP_NS}}}loc"

# Same-host predicate per design section 5.
_SAME_HOSTS = frozenset({"", "tel-spb.ru", "www.tel-spb.ru"})

# Hrefs starting with these schemes are never enqueued.
_SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:")


def _visited_key(url: str) -> str:
  """Visited-set key: ``canonical_path(url) + '?' + raw query``.

  Plain ``canonical_path`` would drop ``?page=N``; keeping the raw query lets
  ``?page=2`` and ``?page=3`` be visited as distinct sub-pages (design § 5).
  """
  parsed = urlparse(url)
  return f"{canonical_path(url)}?{parsed.query}"


def _is_brand_subpage(
  path: str,
  *,
  brand_slug: str,
  only_led: bool,
) -> bool:
  """Return ``True`` iff ``path`` is a brand-scoped Sub_Page.

  Applies the brand-scoped, ``--only-led``, and "not a Model_Page" rules from
  the design table in section 5. The same-host and "not visited" rules are
  enforced by the caller.
  """
  prefix = f"/remont-tv-lcd/{brand_slug}/"
  led_prefix = f"/remont-tv-lcd/{brand_slug}/led"
  p = path.lower()
  if not p.startswith(prefix):
    return False
  if only_led and not p.startswith(led_prefix):
    return False
  # A Sub_Page is, by definition, not itself a Model_Page.
  if match_model_url(path, known_brand_slugs={brand_slug}) is not None:
    return False
  return True


async def collect_brand_subpages(
  fetch: FetchHtml,
  brand_slug: str,
  root_url: str,
  *,
  only_led: bool = False,
  max_subpages: int = MAX_BRAND_SUBPAGES,
  max_depth: int = MAX_BRAND_DEPTH,
) -> set[str]:
  """BFS over in-domain links rooted at ``root_url``.

  Returns the set of sub-page URLs (absolute, including the root) that the
  caller should fetch and feed into ``parse_brand_listing``. Parsing is
  intentionally NOT performed here — the caller drives the actual fetch +
  parse + retry path so error handling stays in one place.

  A child URL is enqueued iff every condition holds (design § 5):

  - same host as ``BASE_URL`` (``tel-spb.ru`` / ``www.tel-spb.ru``)
  - path starts with ``/remont-tv-lcd/<brand_slug>/``
    (or ``/remont-tv-lcd/<brand_slug>/led`` when ``only_led=True``)
  - path is NOT itself a Model_Page (``match_model_url`` returns ``None``)
  - canonical visited key has not been seen before
  - child depth ``< max_depth`` and ``len(visited) < max_subpages``

  Depth and count caps each emit a single INFO log line when first hit.
  """
  brand_slug = brand_slug.lower()
  root_abs = urljoin(BASE_URL + "/", root_url)

  visited: set[str] = {_visited_key(root_abs)}
  result: set[str] = {root_abs}
  queue: deque[tuple[str, int]] = deque([(root_abs, 0)])

  depth_cap_logged = False
  count_cap_logged = False

  while queue:
    url, depth = queue.popleft()

    try:
      html = await fetch(url)
    except Exception as exc:  # noqa: BLE001 — caller decides retry policy
      logger.warning(
        "collect_brand_subpages(%s): fetch failed for %s: %s",
        brand_slug,
        url,
        exc,
      )
      continue

    if not html:
      continue

    soup = BeautifulSoup(html, "html.parser")
    cap_break = False

    for anchor in soup.find_all("a", href=True):
      href = (anchor.get("href") or "").strip()
      if not href or href.startswith("#"):
        continue
      if any(href.lower().startswith(scheme) for scheme in _SKIP_SCHEMES):
        continue

      # Same-host rule operates on the raw href netloc per the design table.
      raw_netloc = urlparse(href).netloc.lower()
      if raw_netloc not in _SAME_HOSTS:
        continue

      child_url = urljoin(url, href)
      parsed = urlparse(child_url)
      child_path = parsed.path or "/"

      key = _visited_key(child_url)
      if key in visited:
        continue

      if not _is_brand_subpage(
        child_path,
        brand_slug=brand_slug,
        only_led=only_led,
      ):
        continue

      child_depth = depth + 1

      if child_depth >= max_depth:
        if not depth_cap_logged:
          logger.info(
            "collect_brand_subpages(%s): depth cap (%d) reached; skipping deeper links",
            brand_slug,
            max_depth,
          )
          depth_cap_logged = True
        continue

      if len(visited) >= max_subpages:
        if not count_cap_logged:
          logger.info(
            "collect_brand_subpages(%s): subpage count cap (%d) reached",
            brand_slug,
            max_subpages,
          )
          count_cap_logged = True
        cap_break = True
        break

      visited.add(key)
      result.add(child_url)
      queue.append((child_url, child_depth))

    if cap_break:
      break

  return result


async def discover_models_from_sitemap(
  fetch: FetchHtml,
  known_brand_slugs: set[str],
) -> list[ModelRef]:
  """Walk ``sitemap.xml`` + nested ``<sitemapindex>`` entries and return Model_Refs.

  The walker is breadth-first; each fetched document is parsed once and
  classified as either a ``<sitemapindex>`` (queue children) or a ``<urlset>``
  (extract Model_Page URLs via :func:`match_model_url`). A shared visited set
  guarantees termination even if an index self-references.

  Failures on a single document log a WARNING and continue — the rest of the
  walk is unaffected (design § 1, requirements 5.1, 5.2).

  Returns a list of :class:`ModelRef` deduplicated on the canonical URL,
  preserving the order in which each URL was first encountered.
  """
  visited: set[str] = set()
  queue: deque[str] = deque([SITEMAP_URL])
  visited.add(SITEMAP_URL)

  seen_urls: set[str] = set()
  refs: list[ModelRef] = []

  while queue:
    sitemap_url = queue.popleft()

    try:
      text = await fetch(sitemap_url)
    except Exception as exc:  # noqa: BLE001 — caller decides retry policy
      logger.warning(
        "discover_models_from_sitemap: fetch failed for %s: %s",
        sitemap_url,
        exc,
      )
      continue

    if not text:
      logger.warning(
        "discover_models_from_sitemap: empty document at %s",
        sitemap_url,
      )
      continue

    try:
      root = ET.fromstring(text)
    except ET.ParseError as exc:
      logger.warning(
        "discover_models_from_sitemap: XML parse failed for %s: %s",
        sitemap_url,
        exc,
      )
      continue

    tag = root.tag

    if tag == _SITEMAPINDEX_TAG:
      # Enqueue every child <sitemap><loc>…</loc></sitemap>.
      for loc in root.iterfind(f".//{_LOC_TAG}"):
        child = (loc.text or "").strip()
        if not child:
          continue
        child_abs = urljoin(sitemap_url, child)
        if child_abs in visited:
          continue
        visited.add(child_abs)
        queue.append(child_abs)
      continue

    if tag == _URLSET_TAG:
      for loc in root.iterfind(f".//{_LOC_TAG}"):
        url = (loc.text or "").strip()
        if not url:
          continue
        path = urlparse(url).path
        # match_model_url now supports both /remont-tv-lcd/ and /tv/ paths.
        parsed = match_model_url(path, known_brand_slugs=known_brand_slugs)
        if parsed is None:
          continue
        brand, model = parsed
        # Dedup on the canonical URL at this layer; the call site applies the
        # full composite key (brand, normalized_model_name, canonical_path).
        key = canonical_path(url)
        if key in seen_urls:
          continue
        seen_urls.add(key)
        refs.append(
          ModelRef(
            brand=brand,
            model_name=model,
            url=url,
            source_page=sitemap_url,
          )
        )
      continue

    logger.warning(
      "discover_models_from_sitemap: unknown root tag %r at %s",
      tag,
      sitemap_url,
    )

  return refs
