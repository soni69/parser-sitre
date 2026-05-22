"""One-off investigation fetcher for tv-scraper-missing-models / Task 1.1.

Captures brand-root + (optional) /led + depth-2 sub-pages (year segments,
pagination) for a small set of representative brands so that downstream
tasks (1.2, 5.1, 10.1) can reason against real HTML samples.

Run:
    python -m scripts.fetch_investigation

Output:
    data/raw/investigation/<slug>__<label>.html   (one file per fetched URL)
    data/raw/investigation/_index.json            (URL -> filename map)
    data/raw/investigation/README.md              (summary or failure note)

Notes:
- Uses plain aiohttp with the project's polite_delay() and random_user_agent()
  helpers, so the same throttling rules apply as the main scraper.
- No cookies, no auth — purely public listing pages.
- Depth-2 BFS over same-brand sub-pages: pagination (?page=N), year segments
  (/2020/), and any other in-scope sub-page reachable from the brand root.
- Model_Page URLs are intentionally NOT followed; only listing/sub-listing
  pages are saved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from collections import deque
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

# Allow running as `python scripts/fetch_investigation.py` from project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import BASE_URL, DATA_DIR, DEFAULT_HEADERS, REQUEST_TIMEOUT_SEC  # noqa: E402
from utils import polite_delay, random_user_agent, setup_logging  # noqa: E402

OUT_DIR = DATA_DIR / "raw" / "investigation"
INDEX_FILE = OUT_DIR / "_index.json"
README_FILE = OUT_DIR / "README.md"

# Brands to capture. tcl-rowa is the primary hyphenated brand; if it returns
# nothing useful, the script also probes daewoo-electronics as a fallback.
PRIMARY_BRANDS = ["samsung", "lg", "sony", "tcl-rowa"]
FALLBACK_HYPHENATED = "daewoo-electronics"

MAX_DEPTH = 2
MAX_PAGES_PER_BRAND = 25  # safety cap — investigation only

# Same Model_Page pattern used by config.MODEL_PAGE_RE; we reproduce it here so
# the script can recognise (and skip) model-detail links during BFS without
# coupling to a possibly-evolving regex.
MODEL_PATH_RE = re.compile(
    r"^/remont-tv-lcd/[a-z0-9][a-z0-9-]*?-[a-z0-9][a-z0-9_-]*$",
    re.IGNORECASE,
)


logger = setup_logging(logging.INFO)


def _slug_label_from_url(brand: str, url: str) -> str:
    """Derive a stable, descriptive filename label from a fetched URL.

    Examples:
        /remont-tv-lcd/samsung/                        -> root
        /remont-tv-lcd/samsung/led                     -> led
        /remont-tv-lcd/samsung/?page=2                 -> page2
        /remont-tv-lcd/samsung/2020/                   -> 2020
        /remont-tv-lcd/samsung/led/?page=3             -> led_page3
        /remont-tv-lcd/tcl-rowa/2019/?page=2           -> 2019_page2
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    prefix = f"/remont-tv-lcd/{brand}"
    tail = path[len(prefix):].lstrip("/") if path.startswith(prefix) else path
    parts: list[str] = []
    if tail:
        parts.extend(p for p in tail.split("/") if p)
    if parsed.query:
        # collapse k=v pairs into k<v> tokens, strip non-alnum
        for chunk in parsed.query.split("&"):
            if not chunk:
                continue
            k, _, v = chunk.partition("=")
            token = re.sub(r"[^a-z0-9]+", "", f"{k}{v}".lower())
            if token:
                parts.append(token)
    if not parts:
        return "root"
    label = "_".join(parts)
    return re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_") or "root"


def _filename_for(brand: str, url: str, used: set[str]) -> str:
    """Return `<brand>__<label>.html`, deduping label collisions."""
    label = _slug_label_from_url(brand, url)
    base = f"{brand}__{label}"
    candidate = f"{base}.html"
    counter = 2
    while candidate in used:
        candidate = f"{base}_{counter}.html"
        counter += 1
    used.add(candidate)
    return candidate


def _is_in_scope(url: str, brand: str) -> bool:
    """True if the URL is a same-host listing/sub-listing page for `brand`.

    Strictly requires the path to be `/remont-tv-lcd/<brand>` (root, possibly
    with a trailing slash) or `/remont-tv-lcd/<brand>/...` (sub-page). This
    matters for hyphenated brand slugs (e.g. `tcl-rowa`) because the lazy
    Model_Page regex would otherwise mis-split the root URL into a brand and
    model and reject the root listing itself.
    """
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc not in {"tel-spb.ru", "www.tel-spb.ru"}:
        return False
    path = parsed.path.lower().rstrip("/")
    prefix = f"/remont-tv-lcd/{brand.lower()}"
    if path != prefix and not path.startswith(prefix + "/"):
        return False
    return True


def _extract_links(html: str, base_url: str) -> Iterable[str]:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = f"{BASE_URL}{href}"
        elif not href.startswith("http"):
            # relative — resolve against the page URL
            from urllib.parse import urljoin

            href = urljoin(base_url, href)
        yield href


def _canonical_key(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/").lower() or "/"
    query = parsed.query
    return f"{path}?{query}" if query else path


async def _fetch(
    session: aiohttp.ClientSession, url: str
) -> tuple[Optional[str], Optional[int], Optional[str]]:
    """GET `url` once with retries handled implicitly by the politeness loop.

    Returns (text_or_None, status_or_None, error_message_or_None).
    """
    headers = {**DEFAULT_HEADERS, "User-Agent": random_user_agent()}
    await polite_delay()
    try:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            text = await resp.text(errors="replace")
            return text, resp.status, None
    except aiohttp.ClientResponseError as exc:
        return None, exc.status, f"{exc.status} {exc.message}"
    except asyncio.TimeoutError:
        return None, None, "timeout"
    except aiohttp.ClientError as exc:
        return None, None, f"client-error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, None, f"unexpected: {exc}"


async def _capture_brand(
    session: aiohttp.ClientSession,
    brand: str,
    used_filenames: set[str],
) -> tuple[list[dict], list[dict]]:
    """BFS up to MAX_DEPTH from `/<brand>/` and `/<brand>/led` roots.

    Returns (saved_records, failure_records). Each record is a dict suitable
    for the index.json sidecar.
    """
    roots = [
        f"{BASE_URL}/remont-tv-lcd/{brand}/",
        f"{BASE_URL}/remont-tv-lcd/{brand}/led",
    ]

    queue: deque[tuple[str, int]] = deque((r, 0) for r in roots)
    visited: set[str] = set()
    saved: list[dict] = []
    failures: list[dict] = []

    while queue and len(saved) < MAX_PAGES_PER_BRAND:
        url, depth = queue.popleft()
        key = _canonical_key(url)
        if key in visited:
            continue
        visited.add(key)

        if not _is_in_scope(url, brand):
            continue

        text, status, error = await _fetch(session, url)
        if text is None:
            logger.warning("Brand %s: fetch failed [%s] %s", brand, status, url)
            failures.append({"brand": brand, "url": url, "status": status, "error": error})
            continue

        if status and status >= 400:
            logger.warning("Brand %s: HTTP %s for %s", brand, status, url)
            failures.append({"brand": brand, "url": url, "status": status, "error": f"http-{status}"})
            continue

        filename = _filename_for(brand, url, used_filenames)
        path = OUT_DIR / filename
        path.write_text(text, encoding="utf-8")
        saved.append(
            {
                "brand": brand,
                "url": url,
                "status": status,
                "depth": depth,
                "filename": filename,
                "bytes": len(text),
            }
        )
        logger.info(
            "Brand %s: saved depth=%s status=%s bytes=%s -> %s",
            brand,
            depth,
            status,
            len(text),
            filename,
        )

        if depth >= MAX_DEPTH:
            continue

        # Enqueue in-scope children.
        for href in _extract_links(text, url):
            if not _is_in_scope(href, brand):
                continue
            child_key = _canonical_key(href)
            if child_key in visited:
                continue
            queue.append((href, depth + 1))

    return saved, failures


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC, connect=20)
    connector = aiohttp.TCPConnector(limit=4, ssl=False)
    used_filenames: set[str] = set()
    all_saved: list[dict] = []
    all_failures: list[dict] = []

    brands = list(PRIMARY_BRANDS)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        for brand in brands:
            try:
                saved, failures = await _capture_brand(session, brand, used_filenames)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Brand %s: investigation aborted: %s", brand, exc)
                all_failures.append({"brand": brand, "url": None, "status": None, "error": f"abort: {exc}"})
                continue
            all_saved.extend(saved)
            all_failures.extend(failures)

            # If the primary hyphenated brand yielded nothing, try the fallback.
            if brand == "tcl-rowa" and not saved:
                logger.warning(
                    "Brand tcl-rowa returned no pages — falling back to %s",
                    FALLBACK_HYPHENATED,
                )
                fb_saved, fb_failures = await _capture_brand(
                    session, FALLBACK_HYPHENATED, used_filenames
                )
                all_saved.extend(fb_saved)
                all_failures.extend(fb_failures)

    INDEX_FILE.write_text(
        json.dumps(
            {
                "saved": all_saved,
                "failures": all_failures,
                "max_depth": MAX_DEPTH,
                "max_pages_per_brand": MAX_PAGES_PER_BRAND,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if all_saved:
        readme = [
            "# Investigation captures (Task 1.1)",
            "",
            "Raw brand-listing HTML used by Task 1.2 (sub-page pattern review)",
            "and Task 10.1 (smoke test against a hyphenated brand).",
            "",
            f"Total pages saved: **{len(all_saved)}**.",
            f"Total failures: **{len(all_failures)}**.",
            "",
            "## Captures by brand",
            "",
        ]
        by_brand: dict[str, list[dict]] = {}
        for rec in all_saved:
            by_brand.setdefault(rec["brand"], []).append(rec)
        for brand, recs in sorted(by_brand.items()):
            readme.append(f"### {brand} ({len(recs)})")
            readme.append("")
            for rec in recs:
                readme.append(f"- `{rec['filename']}` <- {rec['url']}")
            readme.append("")
        if all_failures:
            readme.append("## Failures")
            readme.append("")
            for rec in all_failures:
                readme.append(
                    f"- {rec.get('brand')}: {rec.get('url')} (status={rec.get('status')}, error={rec.get('error')})"
                )
            readme.append("")
        README_FILE.write_text("\n".join(readme), encoding="utf-8")
        logger.info("Investigation complete: %s pages, %s failures", len(all_saved), len(all_failures))
        return 0

    # No captures at all — likely network unavailable. Drop empty placeholders
    # so downstream tasks (smoke test in 10.1) can still run against a known
    # set of fixture filenames, with a README explaining the situation.
    logger.error("No pages captured. Network unavailable? Writing empty placeholders.")
    placeholders = [
        ("samsung__root.html", "samsung"),
        ("samsung__led.html", "samsung"),
        ("lg__root.html", "lg"),
        ("lg__led.html", "lg"),
        ("sony__root.html", "sony"),
        ("sony__led.html", "sony"),
        ("tcl-rowa__root.html", "tcl-rowa"),
        ("tcl-rowa__led.html", "tcl-rowa"),
    ]
    for filename, _brand in placeholders:
        path = OUT_DIR / filename
        if not path.exists():
            path.write_text("", encoding="utf-8")
    README_FILE.write_text(
        "# Investigation captures (Task 1.1)\n\n"
        "**Network access to tel-spb.ru was unavailable during the investigation run.**\n\n"
        "Empty placeholder HTML files have been created so downstream task scaffolding\n"
        "(notably the smoke test in Task 10.1) can resolve the expected filenames.\n"
        "Re-run `python scripts/fetch_investigation.py` from a host with network\n"
        "access to populate them with real listings.\n\n"
        f"Recorded failures: {len(all_failures)}.\n"
        + "\n".join(
            f"- {rec.get('brand')}: {rec.get('url')} (status={rec.get('status')}, error={rec.get('error')})"
            for rec in all_failures
        ),
        encoding="utf-8",
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
