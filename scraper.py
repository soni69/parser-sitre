"""Async scraper for tel-spb.ru TV repair database."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable, Optional
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup, Tag

from config import (
  BASE_URL,
  FIELD_LABEL_MAP,
  INDEX_URL,
  MODEL_PAGE_RE,
  OTHER_PART_KEYWORDS,
  PANEL_SPEC_LABEL_MAP,
  TV_INDEX_URL,
)
from discovery import collect_brand_subpages
from images import fetch_and_save_preview, preview_path, resolve_image_url
from models import ModelRef, TVRepairData
from utils import (
  canonical_path,
  clean_value,
  label_matches,
  match_model_url,
  normalize_url,
  polite_delay,
  random_user_agent,
  retry_async,
  setup_logging,
  split_model_names,
)

if TYPE_CHECKING:
  # Imported lazily for type-hint purposes only — keeps `coverage` (which
  # shares a name with a popular third-party package) out of the runtime
  # import cycle for `scraper`.
  from coverage import CoverageTracker

logger = logging.getLogger("telspb_scraper")


class TelSpbScraper:
  """Scrapes brands, listing pages, and model detail pages."""

  def __init__(
    self,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    *,
    only_led: bool = False,
    save_raw_html: Callable[[str, str], None] | None = None,
    shutdown_event: asyncio.Event | None = None,
  ) -> None:
    self.session = session
    self.semaphore = semaphore
    self.only_led = only_led
    self.save_raw_html = save_raw_html
    self.shutdown_event = shutdown_event or asyncio.Event()
    self._raw_saved = 0
    self._led_brands: set[str] = set()
    self._known_slugs: set[str] = set()

  def set_known_slugs(self, slugs: set[str]) -> None:
    """Provide the set of known brand slugs for ``match_model_url``.

    Called by ``discover_brands`` (or any other discovery entry point) before
    listing pages are parsed so hyphenated brand slugs are resolved
    unambiguously. Stored slugs are lower-cased.
    """
    self._known_slugs = {s.lower() for s in slugs if s}

  def should_stop(self) -> bool:
    return self.shutdown_event.is_set()

  @retry_async(max_retries=3)
  async def fetch_html(self, url: str) -> str:
    if self.should_stop():
      raise asyncio.CancelledError("Shutdown requested")

    async with self.semaphore:
      headers = {"User-Agent": random_user_agent()}
      await polite_delay()
      async with self.session.get(url, headers=headers) as resp:
        resp.raise_for_status()
        return await resp.text(errors="replace")

  async def discover_brands(self) -> list[tuple[str, str]]:
    """Return list of (brand_slug, brand_page_url)."""
    html = await self.fetch_html(INDEX_URL)
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    led_brands: set[str] = set()

    for a in soup.find_all("a", href=True):
      href = normalize_url(a["href"])
      path = urlparse(href).path
      m = re.match(r"^/remont-tv-lcd/([a-z0-9-]+)/?(?:led)?/?$", path, re.I)
      if not m:
        continue
      slug = m.group(1).lower()
      if slug in ("remont-tv-lcd",):
        continue
      is_led = path.rstrip("/").endswith("/led")
      if is_led:
        led_brands.add(slug)
        found[slug] = href if href.endswith("/") else href + "/"
      elif not self.only_led:
        found.setdefault(slug, href if href.endswith("/") else href + "/")

    for opt in soup.find_all("option", value=True):
      slug = (opt.get("value") or "").strip().lower()
      if slug and re.fullmatch(r"[a-z0-9-]+", slug):
        found.setdefault(slug, f"{BASE_URL}/remont-tv-lcd/{slug}/")

    # Also discover brands from the /tv/ section.
    try:
      tv_html = await self.fetch_html(TV_INDEX_URL)
      tv_soup = BeautifulSoup(tv_html, "html.parser")
      for a in tv_soup.find_all("a", href=True):
        href = normalize_url(a["href"])
        path = urlparse(href).path.lower()
        # Match /tv/<brand>/ directory links (e.g. /tv/samsung/, /tv/lg/2021).
        m_tv = re.match(r"^/tv/([a-z0-9][a-z0-9-]*)(?:/|$)", path)
        if m_tv:
          slug = m_tv.group(1).lower()
          # Skip generic sub-paths that aren't brand slugs.
          if slug in ("old", "price", "oled"):
            continue
          found.setdefault(slug, f"{BASE_URL}/remont-tv-lcd/{slug}/")
    except Exception as exc:  # noqa: BLE001
      logger.warning("Failed to fetch /tv/ index for brand discovery: %s", exc)

    self._led_brands = led_brands
    brands = sorted(found.items(), key=lambda x: x[0])
    self.set_known_slugs({slug for slug, _ in brands})
    logger.info(
      "Discovered %s brands (%s with /led section)",
      len(brands),
      len(led_brands),
    )
    return brands

  def _brand_led_url(self, brand_slug: str) -> str:
    return f"{BASE_URL}/remont-tv-lcd/{brand_slug}/led"

  def _brand_base_url(self, brand_slug: str) -> str:
    return f"{BASE_URL}/remont-tv-lcd/{brand_slug}/"

  def _brand_root_urls(self, brand_slug: str, brand_root_url: str) -> list[str]:
    """Return the listing root URLs to BFS for ``brand_slug``.

    With ``--only-led`` set the result is ``[<brand>/led]`` only; otherwise
    both ``<brand>/`` and ``<brand>/led`` are included so the default mode
    merges both listings (Requirement 7.5). The first entry is always the
    canonical brand root so callers can attribute "brand_page" vs "sub_page"
    counts correctly.
    """
    base = self._brand_base_url(brand_slug)
    led = self._brand_led_url(brand_slug)
    if self.only_led:
      return [led]
    return [base, led]

  def _tracked_fetch(
    self,
    brand_slug: str,
    tracker: "CoverageTracker | None",
  ) -> Callable[[str], Awaitable[str]]:
    """Return a ``FetchHtml`` adapter that records 404/5xx/network failures.

    The returned coroutine forwards to :meth:`fetch_html` and, on failure,
    classifies the exception by HTTP status (404, 5xx) or as ``network`` and
    appends a tracker entry before re-raising. ``collect_brand_subpages``
    consumes it directly; failures are surfaced as ``(None, None)`` only
    inside :meth:`_fetch_listing_html`, since the BFS tolerates raised
    exceptions on individual nodes.
    """

    async def _fetch(url: str) -> str:
      try:
        return await self.fetch_html(url)
      except aiohttp.ClientResponseError as exc:
        if tracker is not None:
          if exc.status == 404:
            kind = "404"
          elif 500 <= exc.status < 600:
            kind = "5xx"
          else:
            kind = "network"
          tracker.record_failure(
            brand_slug,
            url,
            kind,
            f"{exc.status} {exc.message}",
          )
        raise
      except asyncio.CancelledError:
        raise
      except Exception as exc:  # noqa: BLE001
        if tracker is not None:
          tracker.record_failure(brand_slug, url, "network", str(exc))
        raise

    return _fetch

  async def _fetch_listing_html(
    self,
    url: str,
    *,
    brand_slug: str | None = None,
    tracker: "CoverageTracker | None" = None,
  ) -> tuple[Optional[str], Optional[Exception]]:
    """GET a brand listing page; record per-URL failures into ``tracker``.

    Returns ``(html, None)`` on success, ``(None, None)`` on a 404 (so the
    caller can quietly skip), and ``(None, exc)`` on any other failure. When
    ``brand_slug`` and ``tracker`` are supplied, every 404 / 5xx / network
    outcome is also forwarded to ``tracker.record_failure`` with the correct
    kind (Requirements 5.1, 5.2, 5.3).
    """
    try:
      return await self.fetch_html(url), None
    except aiohttp.ClientResponseError as exc:
      if tracker is not None and brand_slug:
        if exc.status == 404:
          kind = "404"
        elif 500 <= exc.status < 600:
          kind = "5xx"
        else:
          kind = "network"
        tracker.record_failure(
          brand_slug,
          url,
          kind,
          f"{exc.status} {exc.message}",
        )
      if exc.status == 404:
        return None, None
      return None, exc
    except asyncio.CancelledError:
      raise
    except Exception as exc:  # noqa: BLE001
      if tracker is not None and brand_slug:
        tracker.record_failure(brand_slug, url, "network", str(exc))
      return None, exc

  async def collect_model_refs(
    self,
    brand_slug: str,
    brand_root_url: str,
    sitemap_refs: list[ModelRef],
    tracker: "CoverageTracker | None" = None,
  ) -> list[ModelRef]:
    """Collect Model_Refs for ``brand_slug`` from sitemap + brand pages + sub-pages.

    The flow per design § 2:

    1. Build the listing root list via :meth:`_brand_root_urls` honoring
       ``--only-led`` (Requirement 7.5).
    2. BFS each root with :func:`discovery.collect_brand_subpages` to gather
       every in-scope sub-page URL (pagination, year sections, etc.).
    3. Fetch each unique URL (roots first, then sub-pages) via the
       tracker-aware listing fetch; parse each via :meth:`parse_brand_listing`.
    4. Filter ``sitemap_refs`` to this brand and merge with the page refs.
       Deduplication on the composite key happens at the call site.
    """
    brand_slug_lc = brand_slug.lower()
    if self.should_stop():
      return []

    # 1. Determine root list: brand base + brand led, depending on flags.
    roots = self._brand_root_urls(brand_slug_lc, brand_root_url)
    root_paths = {canonical_path(r) for r in roots}

    # 2. BFS over each root to gather sub-page URLs (the BFS result includes
    #    the root itself, so we union all of them). The BFS handles its own
    #    fetch errors internally — failures are recorded by ``_tracked_fetch``
    #    and the BFS still returns at minimum the root URL so it gets fetched
    #    for parsing in step 3.
    fetch = self._tracked_fetch(brand_slug_lc, tracker)
    discovered_urls: set[str] = set()
    for root in roots:
      if self.should_stop():
        break
      discovered_urls |= await collect_brand_subpages(
        fetch,
        brand_slug_lc,
        root,
        only_led=self.only_led,
      )

    # 3. Fetch each unique URL (roots first, then sub-pages) and parse.
    ordered_urls: list[str] = []
    seen: set[str] = set()
    for url in roots:
      if url in seen:
        continue
      seen.add(url)
      ordered_urls.append(url)
    for url in sorted(discovered_urls):
      if url in seen:
        continue
      seen.add(url)
      ordered_urls.append(url)

    page_refs: list[ModelRef] = []
    brand_page_count = 0
    sub_page_count = 0

    for url in ordered_urls:
      if self.should_stop():
        break
      html, err = await self._fetch_listing_html(
        url, brand_slug=brand_slug_lc, tracker=tracker
      )
      if err is not None:
        logger.warning(
          "Brand %s: ошибка каталога %s — %s", brand_slug_lc, url, err
        )
        continue
      if html is None:
        continue

      parsed_refs = self.parse_brand_listing(html, brand_slug_lc, url)
      page_refs.extend(parsed_refs)

      if canonical_path(url) in root_paths:
        brand_page_count += len(parsed_refs)
      else:
        sub_page_count += len(parsed_refs)

    # 4. Merge with sitemap-sourced refs of this brand.
    sitemap_for_brand = [
      r for r in sitemap_refs if r.brand.lower() == brand_slug_lc
    ]

    # 5. Collect refs from the /tv/ section for this brand.
    #    Strategy: fetch /tv/<brand>/ page, discover year/type sub-pages from
    #    it (e.g. /tv/samsung/2021, /tv/samsung/lcd), then fetch each sub-page
    #    and parse model links. The /tv/ index is only used for brand discovery
    #    (in discover_brands), not for model collection.
    tv_refs: list[ModelRef] = []
    tv_brand_url = f"{BASE_URL}/tv/{brand_slug_lc}/"
    try:
      tv_brand_html, tv_brand_err = await self._fetch_listing_html(
        tv_brand_url, brand_slug=brand_slug_lc, tracker=tracker
      )
      if tv_brand_html and tv_brand_err is None:
        # Discover sub-pages (years, types) from the brand's /tv/ page.
        tv_subpages = self._discover_tv_subpages(tv_brand_html, brand_slug_lc)
        # Also parse the brand root page itself for models.
        tv_refs.extend(
          self._parse_tv_listing(tv_brand_html, brand_slug_lc, tv_brand_url)
        )
        for tv_sub_url in tv_subpages:
          if self.should_stop():
            break
          # Skip the brand root if it's in the subpage list (already parsed).
          if canonical_path(tv_sub_url) == canonical_path(tv_brand_url):
            continue
          tv_sub_html, tv_sub_err = await self._fetch_listing_html(
            tv_sub_url, brand_slug=brand_slug_lc, tracker=tracker
          )
          if tv_sub_err is not None:
            logger.warning(
              "Brand %s: /tv/ sub-page error %s — %s",
              brand_slug_lc, tv_sub_url, tv_sub_err,
            )
            continue
          if tv_sub_html is None:
            continue
          tv_refs.extend(
            self._parse_tv_listing(tv_sub_html, brand_slug_lc, tv_sub_url)
          )
        logger.debug(
          "Brand %s: /tv/ section yielded %s refs from %s sub-pages",
          brand_slug_lc, len(tv_refs), len(tv_subpages),
        )
    except Exception as exc:  # noqa: BLE001
      logger.warning("Brand %s: failed to process /tv/ section: %s", brand_slug_lc, exc)

    if tracker is not None:
      tracker.record_discovered(brand_slug_lc, "sitemap", len(sitemap_for_brand))
      tracker.record_discovered(brand_slug_lc, "brand_page", brand_page_count)
      tracker.record_discovered(brand_slug_lc, "sub_page", sub_page_count + len(tv_refs))

    logger.info(
      "Brand %s: refs sitemap=%s brand_page=%s sub_page=%s tv_section=%s",
      brand_slug_lc,
      len(sitemap_for_brand),
      brand_page_count,
      sub_page_count,
      len(tv_refs),
    )

    return [*sitemap_for_brand, *page_refs, *tv_refs]

  def parse_brand_listing(
    self,
    html: str,
    brand_slug: str,
    source_page: str,
  ) -> list[ModelRef]:
    soup = BeautifulSoup(html, "html.parser")
    refs: list[ModelRef] = []

    # Primary: anchor links brand-modelslug
    for a in soup.find_all("a", href=True):
      href = a["href"]
      parsed = match_model_url(
        urlparse(href).path,
        known_brand_slugs=self._known_slugs,
      )
      if not parsed or parsed[0] != brand_slug.lower():
        continue
      _, model_slug = parsed
      model_raw = a.get_text(" ", strip=True) or model_slug
      url = normalize_url(href)
      for model_name in split_model_names(model_raw):
        refs.append(
          ModelRef(
            brand=brand_slug,
            model_name=model_name,
            url=self._model_url(brand_slug, model_name, url),
            source_page=source_page,
          )
        )

    # Secondary: structured rows (table / div rows)
    refs.extend(self._parse_listing_rows(soup, brand_slug, source_page))
    return refs

  def _discover_tv_subpages(
    self,
    tv_brand_html: str,
    brand_slug: str,
  ) -> list[str]:
    """Extract year/type sub-page URLs from a /tv/<brand>/ page.

    Looks for links like ``/tv/<brand>/2021``, ``/tv/<brand>/oled``,
    ``/tv/<brand>/lcd`` — anything under ``/tv/<brand_slug>/``.
    Excludes ``/price`` pages (those are shop listings, not model catalogs).
    The brand root page itself (``/tv/<brand>/``) is included in the result
    so it also gets parsed for models.
    """
    soup = BeautifulSoup(tv_brand_html, "html.parser")
    prefix = f"/tv/{brand_slug.lower()}"
    urls: set[str] = set()

    for a in soup.find_all("a", href=True):
      href = a["href"]
      full_url = normalize_url(href)
      path = urlparse(full_url).path.lower().rstrip("/")
      if not path.startswith(prefix):
        continue
      # Skip /price pages — those are shop listings, not model catalogs.
      if path.endswith("/price") or "/price" in path:
        continue
      urls.add(full_url)

    return sorted(urls)

  def _parse_tv_listing(
    self,
    html: str,
    brand_slug: str,
    source_page: str,
  ) -> list[ModelRef]:
    """Parse a /tv/ brand sub-page for model links belonging to ``brand_slug``.

    Model links on /tv/ pages follow the pattern ``/tv/<brand>-<model>``
    (matched by ``match_model_url`` which supports both ``/remont-tv-lcd/``
    and ``/tv/`` prefixes). Also picks up ``/remont-tv-lcd/<brand>-<model>``
    links if the page cross-references the main catalog.
    """
    soup = BeautifulSoup(html, "html.parser")
    refs: list[ModelRef] = []

    for a in soup.find_all("a", href=True):
      href = a["href"]
      path = urlparse(href).path
      parsed = match_model_url(path, known_brand_slugs=self._known_slugs)
      if not parsed or parsed[0] != brand_slug.lower():
        continue
      _, model_slug = parsed
      model_raw = a.get_text(" ", strip=True) or model_slug
      url = normalize_url(href)
      for model_name in split_model_names(model_raw):
        refs.append(
          ModelRef(
            brand=brand_slug,
            model_name=model_name,
            url=url,
            source_page=source_page,
          )
        )
    return refs

  def _parse_listing_rows(
    self,
    soup: BeautifulSoup,
    brand_slug: str,
    source_page: str,
  ) -> list[ModelRef]:
    refs: list[ModelRef] = []
    rows = soup.select("div.row1, tr")
    if not rows:
      rows = soup.select("div.row1")

    for row in rows:
      if not isinstance(row, Tag):
        continue
      link = None
      parsed = None
      href = ""
      for a in row.find_all("a", href=True):
        cand_href = a.get("href", "")
        candidate = match_model_url(
          urlparse(cand_href).path,
          known_brand_slugs=self._known_slugs,
        )
        if candidate and candidate[0] == brand_slug.lower():
          link = a
          parsed = candidate
          href = cand_href
          break
      if link is None or parsed is None:
        continue
      model_raw = link.get_text(" ", strip=True) or parsed[1]
      url = normalize_url(href)
      text = row.get_text("\n", strip=True)
      inline = self._parse_inline_fields(text)
      for model_name in split_model_names(model_raw):
        refs.append(
          ModelRef(
            brand=brand_slug,
            model_name=model_name,
            url=self._model_url(brand_slug, model_name, url),
            panel=inline.get("panel"),
            backlight=inline.get("backlight"),
            mainboard=inline.get("mainboard"),
            psu=inline.get("psu"),
            source_page=source_page,
          )
        )
    return refs

  def _model_url(self, brand: str, model_name: str, fallback: str) -> str:
    path = urlparse(fallback).path
    # Accept both /remont-tv-lcd/brand-model and /tv/brand-model as valid model URLs.
    if re.search(MODEL_PAGE_RE, path, re.I) or re.search(r"/tv/[a-z0-9]", path, re.I):
      return normalize_url(fallback)
    slug = re.sub(r"[^a-z0-9]+", "", model_name.lower())
    return f"{BASE_URL}/remont-tv-lcd/{brand.lower()}-{slug}"

  def _parse_inline_fields(self, text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    patterns = {
      "panel": r"(?:Панель|Panel)\s*[:\-]?\s*([^\n]+)",
      "backlight": r"(?:Подсветка(?:\s+CCFL/EEFL)?|Lamp\s+backlight|Backlight)\s*[:\-]?\s*([^\n]+)",
      "mainboard": r"MainBoard\s*[:\-]?\s*([^\n]+)",
      "psu": r"PSU\s*[:\-]?\s*([^\n]+)",
    }
    for key, pat in patterns.items():
      m = re.search(pat, text, re.I)
      if m:
        result[key] = clean_value(m.group(1))
    return result

  async def scrape_model(self, ref: ModelRef) -> TVRepairData:
    html = await self.fetch_html(ref.url)
    if self.save_raw_html and self._raw_saved < 50:
      self.save_raw_html(ref.url, html)
      self._raw_saved += 1

    data, preview_src = self.parse_detail_page(html, ref)
    if preview_src:
      dest = preview_path(ref.brand, ref.model_name)
      rel = await fetch_and_save_preview(
        self.session, resolve_image_url(preview_src, ref.url), dest
      )
      if rel:
        data.preview_image = rel
    return data

  def parse_detail_page(self, html: str, ref: ModelRef) -> tuple[TVRepairData, Optional[str]]:
    soup = BeautifulSoup(html, "html.parser")
    full_title = f"{ref.brand.upper()} {ref.model_name}"
    preview_src = self._find_preview_src(soup)

    fields: dict[str, str] = {}
    panel_specs: dict[str, str] = {}
    other_parts: dict[str, str] = {}

    info = soup.select_one("div.tv_repair_info_table")

    paragraphs: Iterable[Tag] = []
    if info:
      paragraphs = info.find_all("p")
    if not paragraphs:
      paragraphs = soup.find_all("p")

    for p in paragraphs:
      label, value = self._extract_label_value(p)
      if not label or not value:
        continue
      mapped = self._map_label(label)
      if mapped:
        if mapped == "inverter" and "pwm" in label.lower() and fields.get("inverter"):
          other_parts[label] = value
        elif mapped in fields and fields[mapped]:
          if fields[mapped] != value:
            fields[mapped] = f"{fields[mapped]} // {value}"
        else:
          fields[mapped] = value
      else:
        low = label.lower()
        if any(k in low for k in OTHER_PART_KEYWORDS) and "tuner" not in low:
          other_parts[label] = value

    panel_specs.update(self._parse_panel_datasheet(soup))
    panel_specs.update(self._parse_char_table(soup))
    self._merge_panel_specs(panel_specs, fields)

    text = soup.get_text("\n", strip=True)
    self._regex_fill(fields, text)

    year = fields.pop("year", None) or self._extract_year(text)

    def _v(key: str, fallback: str | None = None) -> str | None:
      val = fields.get(key) or (panel_specs.get(key) if key in panel_specs else None)
      if val:
        return val
      return fallback or None

    data = TVRepairData(
      brand=ref.brand,
      model_name=ref.model_name,
      full_title=full_title,
      chassis=_v("chassis"),
      panel=_v("panel", ref.panel),
      backlight=_v("backlight", ref.backlight),
      inverter=_v("inverter"),
      tcon=_v("tcon"),
      tuner=_v("tuner"),
      mainboard=_v("mainboard", ref.mainboard),
      mainboard_ic=_v("mainboard_ic"),
      psu=_v("psu", ref.psu),
      pwm_power=_v("pwm_power"),
      panel_diagonal=_v("panel_diagonal"),
      panel_resolution=_v("panel_resolution"),
      panel_active_area=_v("panel_active_area"),
      panel_brightness=_v("panel_brightness"),
      panel_contrast=_v("panel_contrast"),
      panel_display_colors=_v("panel_display_colors"),
      panel_frequency=_v("panel_frequency"),
      panel_lamp_type=_v("panel_lamp_type"),
      panel_voltage=_v("panel_voltage"),
      other_parts=other_parts,
      year=year,
    )
    return data, preview_src

  def _find_preview_src(self, soup: BeautifulSoup) -> Optional[str]:
    img = soup.select_one("div.tv_panel_img img.size, div.tv_panel_img img, img.size")
    if img and img.get("src"):
      return img["src"]
    return None

  def _map_panel_spec_label(self, label: str) -> Optional[str]:
    for field, aliases in PANEL_SPEC_LABEL_MAP.items():
      if label_matches(label, aliases):
        return field
    return None

  def _parse_panel_datasheet(self, soup: BeautifulSoup) -> dict[str, str]:
    specs: dict[str, str] = {}
    for p in soup.find_all("p"):
      raw = p.get_text("\n", strip=True)
      if not any(
        x in raw
        for x in (
          "Diagonal size",
          "Resolution",
          "Lamp Type",
          "Brightness",
          "Contrast",
          "Active area",
          "Display colors",
          "Voltage",
        )
      ):
        continue
      for line in raw.split("\n"):
        if ":" not in line:
          continue
        label, _, value = line.partition(":")
        label, value = clean_value(label), clean_value(value)
        key = self._map_panel_spec_label(label)
        if key and value:
          specs.setdefault(key, value)
    return specs

  def _parse_char_table(self, soup: BeautifulSoup) -> dict[str, str]:
    specs: dict[str, str] = {}
    char_map = {
      "диагональ экрана": "panel_diagonal",
      "разрешение": "panel_resolution",
      "потребление от сети": "panel_voltage",
    }
    for tr in soup.select("table.char tr"):
      cells = tr.find_all("td")
      if len(cells) < 2:
        continue
      label = clean_value(cells[0].get_text(" ", strip=True)).rstrip(":").lower()
      value = clean_value(cells[1].get_text(" ", strip=True))
      if not value:
        continue
      for prefix, key in char_map.items():
        if label.startswith(prefix):
          specs.setdefault(key, value)
          break
    return specs

  def _merge_panel_specs(self, panel_specs: dict[str, str], fields: dict[str, str]) -> None:
    if panel_specs.get("panel_lamp_type") and not fields.get("backlight"):
      fields.setdefault("backlight", panel_specs["panel_lamp_type"])
    if panel_specs.get("tuner"):
      fields.setdefault("tuner", panel_specs.pop("tuner"))

  def _extract_label_value(self, tag: Tag) -> tuple[str, str]:
    bold = tag.find("b")
    if bold:
      full = tag.get_text(" ", strip=True)
      label_part = full.split(bold.get_text(strip=True))[0]
      label = clean_value(re.sub(r"[:\s]+$", "", label_part))
      value = clean_value(bold.get_text(" ", strip=True))
      if label.endswith(":"):
        label = label[:-1]
      return label, value

    text = tag.get_text(" ", strip=True)
    if ":" in text:
      label, _, value = text.partition(":")
      return clean_value(label), clean_value(value)
    return "", ""

  def _map_label(self, label: str) -> Optional[str]:
    low = label.lower().replace("т", "t")  # кириллическая «т» в «Тuner»
    if re.match(r"^tuner", low):
      return "tuner"
    low = label.lower()
    if "pwm" in low and ("power" in low or "inverter" in low):
      return "pwm_power"
    for field, aliases in FIELD_LABEL_MAP.items():
      if field == "year":
        continue
      if label_matches(label, aliases):
        return field
    return None

  def _regex_fill(self, fields: dict[str, str], text: str) -> None:
    patterns = {
      "chassis": r"(?:Chassis(?:/Version)?|Шасси)\s*:?\s*([^\n]+)",
      "panel": r"(?:Panel|Панель|Матрица)\s*:?\s*([^\n]+)",
      "backlight": r"(?:Lamp\s+backlight|Подсветка)\s*:?\s*([^\n]+)",
      "inverter": r"Inverter(?:\s*\(backlight\))?\s*:?\s*([^\n]+)",
      "tcon": r"T-CON\s*:?\s*([^\n]+)",
      "mainboard": r"MainBoard\s*:?\s*([^\n]+)",
      "mainboard_ic": r"IC\s+MainBoard\s*:?\s*([^\n]+)",
      "psu": r"(?:Power\s+Supply(?:\s*\(PSU\))?|PSU)\s*:?\s*([^\n]+)",
      "pwm_power": r"PWM\s+(?:Power|Inverter)\s*:?\s*([^\n]+)",
      "tuner": r"(?:Tuner|Тюнер)\s*:?\s*([^\n]+)",
    }
    for key, pat in patterns.items():
      if fields.get(key):
        continue
      m = re.search(pat, text, re.I)
      if m:
        fields[key] = clean_value(m.group(1))

  def _extract_year(self, text: str) -> Optional[str]:
    m = re.search(
      r"(?:Год\s+выпуска|Year)\s*:?\s*(\d{4}|\d{2})",
      text,
      re.I,
    )
    return m.group(1) if m else None

def build_scraper(
  concurrency: int,
  only_led: bool = False,
  save_raw_html: Callable[[str, str], None] | None = None,
  shutdown_event: asyncio.Event | None = None,
) -> tuple[aiohttp.ClientSession, asyncio.Semaphore, TelSpbScraper]:
  setup_logging()
  timeout = aiohttp.ClientTimeout(total=60, connect=20)
  connector = aiohttp.TCPConnector(limit=concurrency * 2, ssl=False)
  session = aiohttp.ClientSession(timeout=timeout, connector=connector)
  semaphore = asyncio.Semaphore(concurrency)
  scraper = TelSpbScraper(
    session,
    semaphore,
    only_led=only_led,
    save_raw_html=save_raw_html,
    shutdown_event=shutdown_event,
  )
  return session, semaphore, scraper
