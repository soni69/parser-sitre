# Requirements Document

## Introduction

The tel-spb.ru TV repair scraper currently saves only a fraction of the models actually published on the site (estimated ~9k available, far fewer persisted to SQLite). Code inspection identified four root causes:

1. `collect_model_refs` fetches a single brand listing page (`/brand/led` or `/brand/`) and never follows pagination or year-based sub-pages, so large brands (Samsung, LG, Sony, ...) lose most of their catalog.
2. `MODEL_PAGE_RE` requires `[a-z0-9]+` for the brand slug while `discover_brands` accepts `[a-z0-9-]+`; brands with hyphens are rejected during listing parsing because of a slug equality check.
3. CLI-level deduplication keys on `ref.url.lower()` only, so distinct models that happen to share a canonical URL collapse into one.
4. Only `/led` and `/` sub-sections are visited, and the workflow has been narrowed to LED-only flows even when the operator wants the full catalog.

This feature reworks discovery to reach full catalog coverage while preserving the existing SQLite schema, CLI surface, and pure HTTP + BeautifulSoup architecture.

## Glossary

- **Scraper**: The tel-spb.ru TV repair scraping module composed of `scraper.py`, `main.py`, and supporting helpers.
- **Sitemap**: The XML document at `https://tel-spb.ru/sitemap.xml`, including any nested sitemaps it references through `<sitemapindex>` entries.
- **Brand_Index**: The page at `https://tel-spb.ru/remont-tv-lcd/` that lists every brand.
- **Brand_Slug**: A lower-case identifier matching `[a-z0-9][a-z0-9-]*` that names one brand on the site.
- **Brand_Page**: A page under `/remont-tv-lcd/<Brand_Slug>/` or `/remont-tv-lcd/<Brand_Slug>/led` that lists models for one brand.
- **Sub_Page**: A child of a Brand_Page reachable via a pagination link (e.g. `?page=N`) or a year segment (e.g. `/2020/`) that lists additional models for the same brand.
- **Model_Page**: A model detail page whose URL matches `/remont-tv-lcd/<Brand_Slug>-<model_slug>`.
- **Model_Ref**: A tuple `(brand, model_name, canonical_url)` representing one discovered model before persistence.
- **Coverage_Report**: A per-run artifact listing, for every Brand_Slug, how many Model_Refs were discovered and how many were saved to SQLite.

## Requirements

### Requirement 1: Full catalog coverage

**User Story:** As an operator, I want the Scraper to traverse the entire tel-spb.ru catalog, so that the SQLite database contains every published TV model and not only the first page of each brand.

#### Acceptance Criteria

1. THE Scraper SHALL fetch the Sitemap and every nested sitemap referenced from it.
2. WHEN parsing a fetched sitemap, THE Scraper SHALL extract every URL whose path matches the Model_Page pattern as a Model_Ref source.
3. THE Scraper SHALL fetch the Brand_Index and derive a Brand_Page URL for every Brand_Slug found there.
4. WHEN a Brand_Page is fetched, THE Scraper SHALL follow every link on that page that targets a Sub_Page of the same Brand_Slug.
5. WHILE Sub_Pages of a Brand_Slug remain undiscovered, THE Scraper SHALL continue fetching newly discovered Sub_Pages until no further Sub_Page link of that Brand_Slug appears.
6. THE Scraper SHALL merge Model_Refs collected from the Sitemap, Brand_Pages, and Sub_Pages into a single per-brand result set before deduplication.
7. WHERE the operator does not pass `--only-led`, THE Scraper SHALL include Model_Refs from both the `/remont-tv-lcd/<Brand_Slug>/` and `/remont-tv-lcd/<Brand_Slug>/led` listings whenever both exist.

### Requirement 2: Hyphenated brand slug support

**User Story:** As an operator, I want brands whose slug contains a hyphen to be scraped, so that vendors like `tcl-rowa` or `daewoo-electronics` are not silently dropped.

#### Acceptance Criteria

1. THE Model_Page URL pattern SHALL accept Brand_Slugs that contain hyphens, matching the regex `[a-z0-9][a-z0-9-]*` for the brand portion.
2. WHEN a Model_Page link is found on a Brand_Page whose Brand_Slug contains a hyphen, THE Scraper SHALL retain the link as a Model_Ref for that Brand_Slug.
3. WHEN comparing a parsed Brand_Slug against the Brand_Slug of the page that contains the link, THE Scraper SHALL treat the comparison as equal whenever both strings are identical after lower-casing.

### Requirement 3: Relaxed deduplication

**User Story:** As an operator, I want models with distinct names to be retained even when they share a canonical URL, so that I do not lose entries to URL collisions.

#### Acceptance Criteria

1. THE Scraper SHALL deduplicate discovered Model_Refs using the composite key `(brand_slug, normalized_model_name, canonical_url)`.
2. THE Scraper SHALL produce `normalized_model_name` by lower-casing the parsed model name and collapsing internal whitespace to a single space.
3. WHERE two Model_Refs share the same canonical_url but differ in either brand_slug or normalized_model_name, THE Scraper SHALL keep both Model_Refs in the queue passed to the persistence layer.
4. WHEN multiple Model_Refs share the full composite key, THE Scraper SHALL keep exactly one of them.

### Requirement 4: Per-brand metrics and coverage report

**User Story:** As an operator, I want a per-brand coverage breakdown at the end of every run, so that I can see at a glance which brands were scraped completely and which lost models.

#### Acceptance Criteria

1. WHILE collecting Model_Refs, THE Scraper SHALL maintain, per Brand_Slug, counters for refs discovered from the Sitemap, refs discovered from Brand_Pages and Sub_Pages, and refs remaining after deduplication.
2. WHEN a run finishes, THE Scraper SHALL emit a Coverage_Report containing, for every Brand_Slug encountered, the discovered total, the saved total read back from SQLite, and the difference `discovered_total - saved_total`.
3. THE Scraper SHALL log the Coverage_Report to the run log at INFO level.
4. THE Scraper SHALL write the Coverage_Report as JSON to `data/coverage_<run_id>.json`.

### Requirement 5: Resilience to HTTP errors

**User Story:** As an operator, I want transient or per-page HTTP failures to be logged with concrete URLs, so that one bad request never silently drops an entire brand.

#### Acceptance Criteria

1. IF a request to the Sitemap, a Brand_Page, a Sub_Page, or a Model_Page returns HTTP 404, THEN THE Scraper SHALL log the failing URL at WARNING level and continue processing remaining URLs.
2. IF a request returns an HTTP 5xx response after the configured retry count is exhausted, THEN THE Scraper SHALL log the failing URL and the final status code at ERROR level and continue processing remaining URLs.
3. WHEN a Sub_Page fetch fails, THE Scraper SHALL skip only that Sub_Page and SHALL continue collecting Model_Refs from the remaining Sub_Pages of the same Brand_Slug.
4. THE Scraper SHALL include every logged failure in the Coverage_Report under a `failures` list keyed by Brand_Slug.

### Requirement 6: Pure HTTP scraping

**User Story:** As a maintainer, I want the Scraper to retrieve pages exclusively over HTTP, so that the runtime stays lightweight and free of browser dependencies.

#### Acceptance Criteria

1. THE Scraper SHALL retrieve every page using `aiohttp` HTTP requests parsed with BeautifulSoup.
2. THE Scraper SHALL operate without invoking Selenium, Playwright, or any other headless browser engine.

### Requirement 7: Backwards compatibility

**User Story:** As an operator, I want my existing database files and CLI invocations to keep working unchanged, so that the upgrade does not force a data migration or a script rewrite.

#### Acceptance Criteria

1. THE Scraper SHALL preserve the existing `tv_repairs` SQLite schema, including the `UNIQUE(brand, model_name)` constraint and the `idx_tv_brand_model` unique index.
2. WHEN persisting Model_Refs that share `(brand, model_name)` but differ in canonical_url, THE Scraper SHALL upsert exactly one row per `(brand, model_name)` via the existing `ON CONFLICT(brand, model_name) DO UPDATE` clause.
3. THE Scraper SHALL accept the CLI flags `--brand`, `--max-models`, `--only-led`, `--resume`, `--concurrency`, `--run-id`, `--with-csv`, `--with-jsonl`, `--export-csv`, and `--export-csv-path` with the semantics defined in the current `main.py`.
4. WHEN the operator passes `--brand <slug>`, THE Scraper SHALL apply Requirements 1 through 5 scoped to that single Brand_Slug.
5. WHEN the operator passes `--only-led`, THE Scraper SHALL restrict Brand_Page and Sub_Page traversal to URLs under `/remont-tv-lcd/<Brand_Slug>/led` while still consulting the Sitemap for Model_Pages of the same Brand_Slug.
