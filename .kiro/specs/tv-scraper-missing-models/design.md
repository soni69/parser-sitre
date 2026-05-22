# Design Document — tv-scraper-missing-models

## Overview

Rework discovery so the tel-spb.ru scraper reaches the full model catalog while keeping the existing SQLite schema, CLI surface, and pure-`aiohttp`+`BeautifulSoup` architecture. Three discovery sources are combined per brand: the site's `sitemap.xml` tree, the brand listing pages (`/<slug>/`, `/<slug>/led`), and a bounded BFS over in-domain sub-pages of the brand (pagination, year sections, etc.). Discovery, dedup, and per-brand bookkeeping are decoupled into focused modules so tests can target each one in isolation.

The change is additive: existing tables, indexes, and CLI flags stay intact. Two new modules (`discovery.py`, `coverage.py`), three small additions to `utils.py`/`config.py`/`storage.py`, plus a refactor of `scraper.collect_model_refs` and `main.run_scraper` are all that is required.

> **Investigation complete (2026-05-22).** Captures for Samsung, LG, Sony are committed under `data/raw/investigation/` (root + `/led` per brand) alongside a synthetic `tcl-rowa` fixture; full findings in `data/raw/investigation/README.md`. The site currently emits no `?page=N`, no year segments, and no off-host links — each listing variant renders the full catalog on a single page. The BFS rules below stay conservative on purpose so they continue to handle pagination or year segments if introduced later. See **Sub-page patterns observed** below for the concrete enumeration.

## Architecture

### Discovery pipeline

```
                      ┌────────────────────────────┐
                      │ TelSpbScraper.discover_     │
                      │ brands() (existing)        │
                      └────────────┬───────────────┘
                                   │ list[(slug, brand_url)]
                                   ▼
              ┌────────────────────┴────────────────────┐
              │              CoverageTracker            │
              └────────────────────┬────────────────────┘
                                   │
   ┌──────────────────────┬────────┴────────┬───────────────────────┐
   │                      │                 │                       │
   ▼                      ▼                 ▼                       ▼
discovery.discover_   scraper.collect_  discovery.collect_     storage.count_
models_from_sitemap() model_refs(slug)  brand_subpages(slug,   saved_for_brand
(once, run-start)        │              brand_url)                  ▲
   │                      │                 │                       │
   │             ┌────────┴────────┐        │                       │
   │             ▼                 ▼        │                       │
   │       parse_brand_listing  Sub-pages BFS                       │
   │         (uses match_      (in-domain, depth/count capped)      │
   │          model_url)                                            │
   │                                                                │
   └──────────────► Per-brand list[ModelRef] ─────► dedup ──┐       │
                                                            │       │
                                                            ▼       │
                                                  storage.save  ────┘
                                                            │
                                                            ▼
                                              coverage.finalize → JSON
```

### Sequence diagram (per brand)

```
main          scraper                discovery               storage     tracker
 │              │                        │                      │           │
 │ discover_brands()                     │                      │           │
 │─────────────►│                        │                      │           │
 │◄────list[(slug,url)]──────────────────                       │           │
 │                                                              │           │
 │ discover_models_from_sitemap(session)                        │           │
 │──────────────────────────────────────►│                      │           │
 │◄──── list[ModelRef] (all brands) ─────                       │           │
 │                                                              │           │
 │ for slug in brands:                                          │           │
 │    collect_model_refs(slug)                                  │           │
 │─────────────►│                                               │           │
 │              │ collect_brand_subpages(slug, url)             │           │
 │              │──────────────────────►│                       │           │
 │              │◄──── set[str] subpage URLs ───                │           │
 │              │ fetch each + parse_brand_listing              │           │
 │              │ tracker.record_discovered(slug,'brand_page',n)│           │
 │              │ tracker.record_discovered(slug,'sub_page',n)  │──────────►│
 │◄──── list[ModelRef] for brand ─────                          │           │
 │                                                              │           │
 │ merge with sitemap refs of slug                              │           │
 │ dedup by (brand, normalized_name, canonical_path)            │           │
 │ tracker.record_after_dedup(slug, n)                          │──────────►│
 │                                                              │           │
 │ scrape_model + storage.save (existing)                       │           │
 │                                                              │           │
 │ end-of-run:                                                  │           │
 │ tracker.finalize(storage)                                    │──────────►│
 │ tracker.to_json(data/coverage_<run_id>.json)                 │           │
```

## Components and Interfaces

### 1. `discovery.py` (new)

Holds the two new traversal entry points. Pure async functions, no class state — they take the existing `aiohttp.ClientSession` plus a small fetch callable so retries/politeness stay in `TelSpbScraper.fetch_html`.

```python
# discovery.py
from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable, Iterable
from urllib.parse import urlparse, urlunparse
from xml.etree import ElementTree as ET

import aiohttp
from bs4 import BeautifulSoup

from config import BASE_URL, MAX_BRAND_DEPTH, MAX_BRAND_SUBPAGES, SITEMAP_URL
from models import ModelRef
from utils import canonical_path, match_model_url, normalize_url

FetchHtml = Callable[[str], Awaitable[str]]  # returns raw HTML/XML text or raises

logger = logging.getLogger("telspb_scraper")

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


async def discover_models_from_sitemap(
    fetch: FetchHtml,
    known_brand_slugs: set[str],
) -> list[ModelRef]:
    """Walk sitemap.xml + nested <sitemapindex> entries and return Model_Refs.

    The walker is breadth-first; each fetched document is parsed once and
    classified as either a sitemapindex (queue children) or a urlset (extract
    Model_Page URLs via `match_model_url`). Failures on a single document log a
    warning and continue.
    """

async def collect_brand_subpages(
    fetch: FetchHtml,
    brand_slug: str,
    root_url: str,
    *,
    only_led: bool = False,
    max_subpages: int = MAX_BRAND_SUBPAGES,
    max_depth: int = MAX_BRAND_DEPTH,
) -> set[str]:
    """BFS over in-domain links rooted at `root_url`.

    A URL is enqueued iff every condition holds:
      - same host as BASE_URL (`tel-spb.ru`)
      - path starts with `/remont-tv-lcd/<brand_slug>/`
        (or `/remont-tv-lcd/<brand_slug>/led` when `only_led=True`)
      - path is NOT itself a Model_Page (does not match `match_model_url`)
      - canonical key (path + raw query) not already visited
      - current depth < max_depth and visited count < max_subpages

    Pagination query strings (`?page=N`) are kept in the canonical key so
    `/<slug>/?page=2` and `/<slug>/?page=3` are visited as distinct sub-pages.
    """
```

Key implementation details:

- `discover_models_from_sitemap` uses a shared `set[str]` of visited sitemap URLs to guarantee termination even if the index self-references. The matcher runs against `urlparse(loc).path`, with `known_brand_slugs` provided up-front so hyphenated brands are recovered correctly.
- `collect_brand_subpages` returns the **set of sub-page URLs to fetch** (not the parsed refs). The caller (`scraper.collect_model_refs`) drives the actual fetch + `parse_brand_listing` so retries, raw-HTML capture, and shutdown handling stay in one place.
- The visited key is `canonical_path(url) + "?" + (parsed.query or "")`. Plain `canonical_path` would drop `?page=N`; raw query keeps pagination distinct.
- BFS uses `collections.deque` and tracks per-URL depth. Hard caps surface as INFO logs (`Brand X: subpage cap hit at depth=…`).

### 2. `scraper.py` refactor

`collect_model_refs` is rewritten:

```python
# scraper.py (refactored excerpt)
async def collect_model_refs(
    self,
    brand_slug: str,
    brand_root_url: str,
    sitemap_refs: list[ModelRef],
    tracker: "CoverageTracker",
) -> list[ModelRef]:
    if self.should_stop():
        return []

    # 1. Determine root list: brand base + brand led, depending on flags.
    roots = self._brand_root_urls(brand_slug, brand_root_url)

    # 2. BFS over each root to gather sub-page URLs.
    subpage_urls: set[str] = set()
    for root in roots:
        subpage_urls |= await collect_brand_subpages(
            self._tracked_fetch(brand_slug, tracker),
            brand_slug,
            root,
            only_led=self.only_led,
        )

    # 3. Fetch each sub-page (root pages included) and parse listings.
    page_refs: list[ModelRef] = []
    for url in [*roots, *sorted(subpage_urls - set(roots))]:
        html = await self._fetch_listing_for_brand(url, brand_slug, tracker)
        if html is None:
            continue
        page_refs.extend(self.parse_brand_listing(html, brand_slug, url))

    tracker.record_discovered(brand_slug, "brand_page", sum(
        1 for r in page_refs if canonical_path(r.source_page or "") == canonical_path(roots[0])
    ))
    tracker.record_discovered(brand_slug, "sub_page", len(page_refs) - _count_root(page_refs, roots))

    # 4. Merge with sitemap-sourced refs of this brand.
    sitemap_for_brand = [r for r in sitemap_refs if r.brand.lower() == brand_slug.lower()]
    tracker.record_discovered(brand_slug, "sitemap", len(sitemap_for_brand))

    return [*sitemap_for_brand, *page_refs]
```

- `_brand_root_urls` returns `[/<slug>/, /<slug>/led]` when `--only-led` is off, else `[/<slug>/led]` (Requirement 7.5).
- `_tracked_fetch` is a small adapter that calls `self.fetch_html` and forwards 404/5xx outcomes to the tracker before re-raising or returning `None`.
- `parse_brand_listing` swaps raw-regex calls (`re.search(MODEL_PAGE_RE, …)`) for `match_model_url(path, known_brand_slugs={brand_slug, *self._known_slugs})`. The set of known slugs is captured by `discover_brands` and passed in via `TelSpbScraper.set_known_slugs(slugs)`.

### 3. Brand-slug-aware URL matcher (`utils.py`)

The current regex `r"/remont-tv-lcd/([a-z0-9]+)-([a-z0-9][a-z0-9_-]*)"` rejects hyphenated brand slugs. A naive fix to lazy `[a-z0-9-]*?` introduces ambiguity: `tcl-rowa-32led` could legitimately split as `(tcl, rowa-32led)` or `(tcl-rowa, 32led)`. Resolution: prefer a known-slug match whenever possible, fall back to lazy regex only for unknown brands.

```python
# utils.py
MODEL_PATH_RE = re.compile(
    r"^/remont-tv-lcd/(?P<brand>[a-z0-9][a-z0-9-]*?)-(?P<model>[a-z0-9][a-z0-9_-]*)$",
    re.IGNORECASE,
)


def match_model_url(
    path: str,
    known_brand_slugs: set[str] | None = None,
) -> tuple[str, str] | None:
    """Parse a Model_Page URL path into (brand_slug, model_slug) or None.

    Strategy:
      1. If `known_brand_slugs` is provided, try the LONGEST slug that prefixes
         the segment after `/remont-tv-lcd/` and is followed by `-` and a
         non-empty model slug. This unambiguously resolves cases like
         `tcl-rowa-32led` when both `tcl` and `tcl-rowa` are known brands.
      2. Otherwise fall back to the lazy regex above. Lazy is used so the brand
         portion is the SHORTEST viable slug; this is correct for any single
         hyphen scheme but is documented as best-effort for unknown brands.
    """
    p = path.lower().rstrip("/")
    if not p.startswith("/remont-tv-lcd/"):
        return None
    tail = p[len("/remont-tv-lcd/"):]
    if "/" in tail:  # this is a sub-page, not a model page
        return None

    if known_brand_slugs:
        candidates = sorted(
            (s for s in known_brand_slugs if tail.startswith(s + "-")),
            key=len,
            reverse=True,
        )
        for slug in candidates:
            model = tail[len(slug) + 1:]
            if model and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", model):
                return slug, model

    m = MODEL_PATH_RE.match("/" + p.lstrip("/"))
    if not m:
        return None
    return m.group("brand"), m.group("model")


def normalize_model_name(name: str) -> str:
    """Lower-cased, whitespace-collapsed model name for dedup keys."""
    return re.sub(r"\s+", " ", (name or "").strip()).lower()
```

`scraper.parse_brand_listing` now does:

```python
parsed = match_model_url(urlparse(href).path, known_brand_slugs=self._known_slugs)
if not parsed or parsed[0] != brand_slug.lower():
    continue
```

Replacing the brittle `m.group(1).lower() != brand_slug.lower()` check.

### 4. Dedup in `main.py`

```python
# main.py (excerpt)
from utils import canonical_path, normalize_model_name

DedupKey = tuple[str, str, str]


def _dedup_key(ref: ModelRef) -> DedupKey:
    return (
        ref.brand.lower(),
        normalize_model_name(ref.model_name),
        canonical_path(ref.url),
    )


unique: dict[DedupKey, ModelRef] = {}
for ref in all_refs:
    unique[_dedup_key(ref)] = ref
refs_list = list(unique.values())
```

Two `ModelRef`s differing in any one of {brand, normalized model name, canonical path} are kept; only refs identical on all three collapse (Requirements 3.1–3.4).

### 5. Sub-page detection rules

A link `<a href=…>` on a Brand_Page is enqueued for BFS iff **all** of the following hold:

| Rule                                | Test                                                                    |
| ----------------------------------- | ----------------------------------------------------------------------- |
| Same host                           | `urlparse(href).netloc in {"", "tel-spb.ru", "www.tel-spb.ru"}`         |
| Brand-scoped path                   | `path.lower().startswith(f"/remont-tv-lcd/{brand_slug}/")`              |
| Not a Model_Page                    | `match_model_url(path, known_slugs) is None`                            |
| `--only-led` honored                | If `only_led=True`, also `path.startswith(f"/…/{slug}/led")`            |
| Not visited                         | `f"{canonical_path(url)}?{query}"` not in visited set                  |
| Within depth/count caps             | `depth < MAX_BRAND_DEPTH` and `len(visited) < MAX_BRAND_SUBPAGES`        |

Caps live in `config.py`:

```python
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
MAX_BRAND_SUBPAGES = 200
MAX_BRAND_DEPTH = 3
```

### Sub-page patterns observed

Captures: `data/raw/investigation/{samsung,lg,sony}__{root,led}.html` plus the
synthetic `tcl-rowa__synthetic_root.html` fixture. Source-of-truth notes live
in `data/raw/investigation/README.md`; this subsection summarises the
implications for the BFS rules above.

Patterns enumerated across the six real captures:

| Pattern                          | Observed? | Example URL                                              |
| -------------------------------- | --------- | -------------------------------------------------------- |
| Brand root                       | yes       | `https://tel-spb.ru/remont-tv-lcd/samsung/`              |
| `/<slug>/led` variant            | yes       | `https://tel-spb.ru/remont-tv-lcd/samsung/led`           |
| Cross-link between root and led  | yes       | `<a href="/remont-tv-lcd/samsung/led">` on `/samsung/`   |
| `?page=N` pagination             | **no**    | (none emitted; full catalog rendered on a single page)   |
| `/<slug>/<year>/` segment        | **no**    | (none emitted on any captured listing)                   |
| `/<slug>/by-year/` style index   | **no**    | (none emitted)                                           |
| Off-host or absolute URLs        | **no**    | every same-brand `<a href>` is a relative `/…` path      |
| Hyphenated brand listing         | **no**    | `tcl-rowa`, `daewoo-electronics`, etc. all 404 today     |

Concrete consequences:

- Same-brand sub-page link counts per file are at most 2 (root ↔ led
  cross-links only); depth-2 BFS from `/<slug>/` and `/<slug>/led` therefore
  discovers no further sub-pages on the present-day site. The full catalog
  for each variant is delivered in one document (the Samsung `/led` listing
  is ~320 KB on its own).
- Both `/<slug>/` and `/<slug>/led` carry distinct, overlapping rows for
  Samsung / LG / Sony, which is why the default mode merges both roots
  (Requirement 1.7) and dedup absorbs the overlap.
- Hyphenated brand support (Requirement 2) is forward-looking: no
  hyphenated slug currently exists on `tel-spb.ru` (39 brand slugs in the
  index, none containing `-`). The synthetic `tcl-rowa` fixture is
  committed only to drive Task 10.1's smoke test.
- The conservative BFS rules table is left **unchanged**. The captures
  don't justify tightening it (no patterns to filter out today) and the
  rules already handle `?page=N`, year segments, and off-host links
  correctly the moment they appear.

### 6. Coverage tracker (`coverage.py`)

```python
# coverage.py
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger("telspb_scraper")

Source = Literal["sitemap", "brand_page", "sub_page"]
FailureKind = Literal["404", "5xx", "network", "parse"]


@dataclass
class _BrandStats:
    discovered: dict[str, int] = field(default_factory=dict)  # source -> count
    after_dedup: int = 0
    saved: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)


class CoverageTracker:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._brands: dict[str, _BrandStats] = {}

    def _get(self, brand: str) -> _BrandStats:
        return self._brands.setdefault(brand.lower(), _BrandStats())

    def record_discovered(self, brand: str, source: Source, count: int) -> None:
        stats = self._get(brand)
        stats.discovered[source] = stats.discovered.get(source, 0) + count

    def record_after_dedup(self, brand: str, count: int) -> None:
        self._get(brand).after_dedup = count

    def record_failure(self, brand: str, url: str, kind: FailureKind, message: str) -> None:
        self._get(brand).failures.append(
            {"url": url, "kind": kind, "message": message[:240]}
        )

    def finalize(self, storage) -> dict[str, dict]:
        report: dict[str, dict] = {}
        for brand, stats in sorted(self._brands.items()):
            saved = storage.count_saved_for_brand(brand)
            stats.saved = saved
            discovered_total = sum(stats.discovered.values())
            report[brand] = {
                "discovered": dict(stats.discovered),
                "discovered_total": discovered_total,
                "after_dedup": stats.after_dedup,
                "saved": saved,
                "diff": discovered_total - saved,
                "failures": list(stats.failures),
            }
            logger.info(
                "Brand %s: discovered=%s saved=%s diff=%s",
                brand,
                discovered_total,
                saved,
                discovered_total - saved,
            )
        return report

    def to_json(self, path: Path, report: dict[str, dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"run_id": self.run_id, "brands": report}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
```

`storage.StorageManager.count_saved_for_brand(slug: str) -> int` is a one-liner that runs `SELECT COUNT(*) FROM tv_repairs WHERE LOWER(brand)=?`.

`main.py` writes the JSON to `DATA_DIR / f"coverage_{run_id}.json"` after the worker pool finishes (or after a graceful shutdown), and emits one INFO line per brand inside `finalize`.

### 7. Resilience

`TelSpbScraper._fetch_listing_html` already returns `(html, exc)`; it is extended to record outcomes into the tracker and to surface the HTTP status for failure classification:

```python
async def _fetch_listing_html(
    self, url: str, *, brand_slug: str | None, tracker: "CoverageTracker | None"
) -> tuple[str | None, Exception | None]:
    try:
        return await self.fetch_html(url), None
    except aiohttp.ClientResponseError as exc:
        kind = "404" if exc.status == 404 else "5xx" if 500 <= exc.status < 600 else "network"
        if tracker and brand_slug:
            tracker.record_failure(brand_slug, url, kind, f"{exc.status} {exc.message}")
        if exc.status == 404:
            return None, None
        return None, exc
    except Exception as exc:  # noqa: BLE001
        if tracker and brand_slug:
            tracker.record_failure(brand_slug, url, "network", str(exc))
        return None, exc
```

Model-detail failures are recorded by `main.run_scraper`'s worker:

```python
except Exception as exc:
    logger.error("Failed %s: %s", ref.url, exc)
    tracker.record_failure(ref.brand, ref.url, "network", str(exc))
```

### 8. CLI / backwards compatibility

All existing flags remain. New optional flag:

| Flag             | Effect                                                                   |
| ---------------- | ------------------------------------------------------------------------ |
| `--no-sitemap`   | Skip `discover_models_from_sitemap`. Used for debugging brand-page BFS.  |

`--only-led` semantics (Requirement 7.5):
- Sub-page BFS roots become `[/<slug>/led]` only.
- `discover_models_from_sitemap` is **still** consulted; refs of that brand surface even if the BFS root path is missing.
- `parse_brand_listing` still keeps every Model_Ref whose URL matches the brand, regardless of `/led` segment, because sitemap and `/led` listings can yield non-`/led`-pathed Model_Pages.

## Data Models

No changes to `TVRepairData` or `ModelRef`. The composite dedup key is computed at use-site, never persisted.

The `tv_repairs` SQLite schema is unchanged (Requirement 7.1):
- `UNIQUE(brand, model_name)` table-level constraint
- `idx_tv_brand_model` unique index
- `ON CONFLICT(brand, model_name) DO UPDATE` upsert path is reused as-is

## File diff overview

| File           | Change   | Notes                                                                                  |
| -------------- | -------- | -------------------------------------------------------------------------------------- |
| `discovery.py` | NEW      | `discover_models_from_sitemap`, `collect_brand_subpages`                               |
| `coverage.py`  | NEW      | `CoverageTracker` + `_BrandStats`                                                      |
| `utils.py`     | MODIFIED | Add `match_model_url`, `normalize_model_name`; keep `canonical_path`, `slugify_model`  |
| `config.py`    | MODIFIED | Add `SITEMAP_URL`, `MAX_BRAND_SUBPAGES`, `MAX_BRAND_DEPTH`; keep `MODEL_PAGE_RE` (used only as fallback by `MODEL_PATH_RE` documentation)            |
| `scraper.py`   | MODIFIED | Replace `MODEL_PAGE_RE` raw use in `parse_brand_listing` and `_parse_listing_rows` with `match_model_url`; rewrite `collect_model_refs` to consume sub-page BFS + sitemap refs; thread `tracker` through `_fetch_listing_html`; add `set_known_slugs` |
| `storage.py`   | MODIFIED | Add `count_saved_for_brand(slug) -> int`                                               |
| `main.py`      | MODIFIED | Use composite dedup key; instantiate `CoverageTracker`; pass sitemap refs into `collect_model_refs`; write coverage JSON; honour `--no-sitemap` |
| `models.py`    | UNCHANGED | —                                                                                     |

## Error Handling

| Class of failure                          | Behavior                                                                                  |
| ----------------------------------------- | ----------------------------------------------------------------------------------------- |
| 404 on sitemap / brand / sub / model page | WARNING + tracker `record_failure(kind="404")`; processing continues.                     |
| 5xx after retries exhausted               | ERROR + tracker `record_failure(kind="5xx")`; processing continues.                       |
| `aiohttp.ClientError`, timeouts           | Retried by `retry_async`; if exhausted, ERROR + `record_failure(kind="network")`.         |
| Malformed sitemap XML                     | WARNING + skip; the walker keeps the parent index running.                                |
| Sub-page BFS hits cap                     | INFO log, BFS terminates cleanly, refs collected so far are kept.                         |
| `scrape_model` failure                    | Existing per-worker `except Exception` block, plus tracker entry.                         |

The sitemap stage failing entirely (e.g. site-wide 5xx on `sitemap.xml`) does **not** abort the run; it is recorded as a single failure for the synthetic `__sitemap__` brand and the rest of discovery proceeds.

## Testing Strategy

### Property-based tests (Hypothesis)

PBT applies cleanly to four pure logic surfaces and stays out of the HTTP/parser layer (covered separately by fixture tests with sample XML/HTML).

- **P3 / P4 / P5**: pure helpers in `utils.py` — `match_model_url`, `normalize_model_name`, dedup key composition.
- **P1**: sitemap walker is wrapped behind a `FetchHtml` callable, so Hypothesis generates synthetic XML trees and a dict-backed fake fetcher.
- **P2**: sub-page BFS uses the same `FetchHtml` seam; Hypothesis generates a small graph of HTML pages.
- **P6**: `CoverageTracker` is a state machine over plain dicts; Hypothesis drives sequences of `record_*` calls and asserts the finalize output.

PBT is **not** used for HTTP retries, real `aiohttp` calls, the SQLite schema check, or CLI argparse — those are example-based or smoke tests.

### Example tests (pytest fixtures)

- `discover_brands` over a captured copy of `/remont-tv-lcd/` HTML.
- `parse_brand_listing` on captured listing HTML for one large brand and one hyphenated brand (e.g. `tcl-rowa`).
- 404 / 5xx behavior using `aiohttp` mock responses (via `aioresponses`).
- `--brand` and `--only-led` argparse paths run end-to-end against mocked HTTP.
- Schema check: open `tv_repairs.db` after `_init_sqlite`, assert `UNIQUE(brand, model_name)` and `idx_tv_brand_model` are present.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of the system — a formal statement about what the system should do. Properties bridge the gap between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Sitemap traversal recovers every Model_Page

*For any* sitemap tree (a `<sitemapindex>` recursively pointing at `<urlset>` leaves, with arbitrary fan-out and depth) where each leaf contains an arbitrary mix of Model_Page and non-Model_Page URLs, `discover_models_from_sitemap` returns a `ModelRef` for **every** Model_Page URL in the tree and for **none** of the others, with no duplicates.

**Validates: Requirements 1.1, 1.2**

### Property 2: Brand sub-page BFS visits exactly the reachable, in-scope sub-pages

*For any* synthetic site graph rooted at a Brand_Page where each node is either an in-scope Sub_Page (`/remont-tv-lcd/<slug>/…` and not a Model_Page), an out-of-scope page, or a Model_Page, `collect_brand_subpages` returns the set of in-scope Sub_Pages reachable from the root, never a Model_Page or an out-of-scope page, terminates within `MAX_BRAND_SUBPAGES` visits and `MAX_BRAND_DEPTH` levels, and never visits the same canonical key twice.

**Validates: Requirements 1.4, 1.5, 7.5**

### Property 3: `match_model_url` is a round-trip when the brand is known

*For any* brand slug `b` matching `[a-z0-9][a-z0-9-]*` and any model slug `m` matching `[a-z0-9][a-z0-9_-]*`, if `b ∈ known_brand_slugs`, then `match_model_url(f"/remont-tv-lcd/{b}-{m}", known_brand_slugs)` returns exactly `(b, m)`, even when other prefixes of `b` are also in `known_brand_slugs`.

**Validates: Requirements 2.1, 2.2, 2.3**

### Property 4: `normalize_model_name` is idempotent and whitespace-stable

*For any* string `s`, `normalize_model_name(normalize_model_name(s)) == normalize_model_name(s)`, and inserting any sequence of additional whitespace characters into `s` produces the same normalized output.

**Validates: Requirements 3.2**

### Property 5: Dedup keeps exactly one ref per composite key

*For any* list of `ModelRef`s, the dedup output has length equal to the number of distinct `(brand.lower(), normalize_model_name(model_name), canonical_path(url))` triples in the input, every triple in the input is represented exactly once in the output, and refs differing in any one of the three coordinates are both retained.

**Validates: Requirements 3.1, 3.3, 3.4**

### Property 6: Coverage tracker faithfully sums recorded events and produces a well-formed report

*For any* sequence of `record_discovered(brand, source, count)`, `record_after_dedup(brand, count)`, and `record_failure(brand, url, kind, message)` calls on a fresh `CoverageTracker`, the output of `finalize(storage_stub)` contains every brand mentioned in any call, each brand's `discovered[source]` equals the sum of counts recorded with that source, `after_dedup` equals the last value recorded, `saved` equals `storage_stub.count_saved_for_brand(brand)`, `diff == discovered_total - saved`, and `failures` is the in-order list of recorded failure dicts.

**Validates: Requirements 4.1, 4.2, 5.4**

## Risks and Open Questions

- **Sitemap may not list all model pages.** Mitigation: combine sitemap + brand-page BFS; the dedup key absorbs overlap. Coverage report exposes the diff per brand so missed pages surface immediately.
- **Sub-page URL shape — captured, no patterns to tighten yet.** Task 1.1 captured Samsung, LG, Sony root + `/led` listings under `data/raw/investigation/`; no `?page=N`, no year segments, no off-host or hyphenated-brand listings exist on the live site today (full findings in `data/raw/investigation/README.md` and the **Sub-page patterns observed** subsection of section 5). The BFS rules stay intentionally conservative so they handle pagination and year segments correctly the moment they appear; if either pattern lands later, no design change is needed.
- **Performance.** ~9k pages with `concurrency=6` and `0.8–2.0s` polite delay yields a 30–60 minute run — acceptable per the clarify phase. If this grows, the polite-delay range or concurrency ceiling can be tuned in `config.py` without further design changes.
- **Ambiguous lazy regex on unknown brands.** `match_model_url` falls back to lazy regex when the brand isn't yet known, which can mis-split URLs like `tcl-rowa-32led` if `tcl-rowa` is missing from `known_brand_slugs`. Mitigation: `discover_brands` runs first and seeds the known-slug set, so the fallback is exercised only for brands that slipped through the brand index — a pathological case that surfaces in the coverage report's `diff` column.
- **`MAX_BRAND_SUBPAGES = 200` cap.** Picked conservatively for a catalog with the largest brands estimated under ~150 pages. If a brand truly has more sub-pages, the cap is logged at INFO and surfaces in coverage. Operator can raise it via `config.py` without code changes.
