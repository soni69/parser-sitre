"""End-to-end smoke test on a hyphenated brand.

**Validates: Requirements 2.2, 4.2, 4.4, 7.4**

Drives ``main.run_scraper`` with ``--brand tcl-rowa --no-sitemap`` against the
synthetic listing fixture committed in Task 1.1 (``tcl-rowa`` is a hyphenated
brand slug that the legacy ``MODEL_PAGE_RE`` would have dropped). Every HTTP
call goes through ``aioresponses`` so the test is hermetic; every path
constant is monkey-patched onto ``tmp_path`` so real ``data/`` is never
touched.

The test asserts that:

1. Every ``ModelRef`` produced by :meth:`scraper.TelSpbScraper.parse_brand_listing`
   on the fixture survives end-to-end and lands in the SQLite database — i.e.
   the hyphenated brand is no longer dropped (Requirement 2.2 / 7.4).
2. The coverage JSON written to ``DATA_DIR / coverage_<run_id>.json`` is
   well-formed: it contains the brand key with non-zero ``discovered_total``,
   integer ``after_dedup`` / ``saved`` values, ``diff == discovered_total -
   saved``, and a (possibly non-empty) ``failures`` list (Requirements 4.2,
   4.4).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from argparse import Namespace
from pathlib import Path

import pytest

# Make the project root importable BEFORE importing project modules. The
# tests/ folder ships its own conftest.py which already inserts ROOT, but
# importing this file directly (e.g. via ``pytest path/to/file``) should
# also work, so we mirror the conftest insertion defensively.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aioresponses import aioresponses  # noqa: E402  (after sys.path setup)

import config  # noqa: E402
import images  # noqa: E402
import main as main_mod  # noqa: E402
import scraper as scraper_mod  # noqa: E402
import storage as storage_mod  # noqa: E402
import utils  # noqa: E402
from scraper import TelSpbScraper  # noqa: E402


# ---------------------------------------------------------------------------
# Test fixtures (HTML payloads).
# ---------------------------------------------------------------------------

FIXTURE_PATH = (
    ROOT / "data" / "raw" / "investigation" / "tcl-rowa__synthetic_root.html"
)

# Brand index page. Only needs to be parseable by ``discover_brands``; one
# anchor matching ``/remont-tv-lcd/<slug>/`` is enough for the slug to land in
# ``_known_slugs``.
INDEX_HTML = (
    "<!DOCTYPE html><html><body>"
    "<a href='/remont-tv-lcd/tcl-rowa/'>TCL-Rowa</a>"
    "</body></html>"
)

# Minimal model detail page. ``parse_detail_page`` tolerates entirely empty
# field tables (every value falls back to ``None``); no ``<img>`` means no
# preview download path is exercised.
MODEL_DETAIL_HTML = (
    "<!DOCTYPE html><html><head><title>m</title></head><body>"
    "<div class='tv_repair_info_table'></div></body></html>"
)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _redirect_data_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every project path constant at ``tmp_path``.

    ``storage.py`` and ``main.py`` import path constants by name from
    ``config`` (``from config import DATA_DIR, SQLITE_FILE, ...``), so each
    importer holds its own binding. Patching only ``config.<NAME>`` is not
    enough — every binding has to be updated for the test run.
    """
    data_dir = tmp_path / "data"
    json_dir = data_dir / "json"
    csv_dir = data_dir / "csv"
    raw_dir = data_dir / "raw"
    preview_dir = data_dir / "previews"
    logs_dir = tmp_path / "logs"
    for d in (data_dir, json_dir, csv_dir, raw_dir, preview_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    targets: dict[str, Path] = {
        "DATA_DIR": data_dir,
        "JSON_DIR": json_dir,
        "CSV_DIR": csv_dir,
        "RAW_DIR": raw_dir,
        "PREVIEW_DIR": preview_dir,
        "LOGS_DIR": logs_dir,
        "SQLITE_FILE": data_dir / "tv_repairs.db",
        "RESUME_FILE": data_dir / "resume_state.json",
        "JSONL_FILE": json_dir / "tv_repairs.jsonl",
        "CSV_FILE": csv_dir / "tv_repairs.csv",
    }

    for module in (config, storage_mod, main_mod, images):
        for name, value in targets.items():
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value, raising=False)

    return data_dir


def _patch_polite_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``polite_delay`` with a no-op so the test runs in milliseconds."""

    async def _no_delay() -> None:
        return None

    monkeypatch.setattr(utils, "polite_delay", _no_delay, raising=True)
    # ``scraper.py`` does ``from utils import polite_delay``; that binding has
    # to be patched separately so ``fetch_html`` picks up the no-op.
    monkeypatch.setattr(scraper_mod, "polite_delay", _no_delay, raising=True)


def _expected_refs_from_fixture(fixture_html: str) -> list:
    """Run ``parse_brand_listing`` on the fixture to compute the expected refs.

    No HTTP, no event loop — ``parse_brand_listing`` is a pure method that
    only consumes ``self._known_slugs`` and helper utilities. We construct a
    bare scraper with ``session=None`` because none of the network paths are
    invoked.
    """
    semaphore = asyncio.Semaphore(1)
    test_scraper = TelSpbScraper(session=None, semaphore=semaphore)  # type: ignore[arg-type]
    test_scraper.set_known_slugs({"tcl-rowa"})
    return test_scraper.parse_brand_listing(
        fixture_html,
        "tcl-rowa",
        "https://tel-spb.ru/remont-tv-lcd/tcl-rowa/",
    )


# ---------------------------------------------------------------------------
# The smoke test.
# ---------------------------------------------------------------------------

def test_hyphenated_brand_survives_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_scraper(--brand tcl-rowa)`` saves every Model_Ref + emits coverage JSON."""

    fixture_html = FIXTURE_PATH.read_text(encoding="utf-8")
    data_dir = _redirect_data_paths(monkeypatch, tmp_path)
    _patch_polite_delay(monkeypatch)

    expected_refs = _expected_refs_from_fixture(fixture_html)
    assert expected_refs, "Fixture must yield at least one ModelRef"

    expected_composite_keys = {
        (
            r.brand.lower(),
            utils.normalize_model_name(r.model_name),
            utils.canonical_path(r.url),
        )
        for r in expected_refs
    }
    expected_brand_model_keys = {(b, mn) for (b, mn, _) in expected_composite_keys}

    args = Namespace(
        brand="tcl-rowa",
        max_models=0,
        only_led=False,
        resume=False,
        concurrency=2,
        no_sitemap=True,
        run_id="smoke-test",
        with_csv=False,
        with_jsonl=False,
        export_csv=False,
        export_csv_path="",
    )

    # Mock every URL the pipeline can possibly request:
    # - INDEX_URL: drives ``discover_brands`` → registers ``tcl-rowa``.
    # - Brand root + LED root: BFS roots and listing fetches.
    #   The LED root 404 is intentional — it exercises the failure path
    #   into ``CoverageTracker.record_failure``.
    # - Each Model_Page from the fixture: ``scrape_model`` → ``fetch_html``.
    #
    # ``repeat=True`` so the same URL can be fetched from multiple call
    # sites (BFS uses ``_tracked_fetch``; ``collect_model_refs`` uses
    # ``_fetch_listing_html``) without aioresponses exhausting the mock.
    base = "https://tel-spb.ru"
    model_slugs = ("l32d2900", "l40f3300", "l43p2us", "l50p2us", "l55p1us")

    with aioresponses() as mocked:
        mocked.get(
            f"{base}/remont-tv-lcd/",
            status=200,
            body=INDEX_HTML,
            content_type="text/html; charset=utf-8",
            repeat=True,
        )
        mocked.get(
            f"{base}/remont-tv-lcd/tcl-rowa/",
            status=200,
            body=fixture_html,
            content_type="text/html; charset=utf-8",
            repeat=True,
        )
        mocked.get(
            f"{base}/remont-tv-lcd/tcl-rowa/led",
            status=404,
            body="not found",
            repeat=True,
        )
        for slug in model_slugs:
            mocked.get(
                f"{base}/remont-tv-lcd/tcl-rowa-{slug}",
                status=200,
                body=MODEL_DETAIL_HTML,
                content_type="text/html; charset=utf-8",
                repeat=True,
            )

        exit_code = asyncio.run(main_mod.run_scraper(args))

    assert exit_code == 0, f"run_scraper returned non-zero exit code: {exit_code}"

    # ------------------------------------------------------------------
    # Assertion 1: every Model_Ref from parse_brand_listing landed in DB.
    # ------------------------------------------------------------------
    db_path = data_dir / "tv_repairs.db"
    assert db_path.exists(), f"SQLite DB was not created at {db_path}"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT brand, model_name FROM tv_repairs").fetchall()

    saved_brand_model = {(b.lower(), utils.normalize_model_name(m)) for b, m in rows}

    assert saved_brand_model == expected_brand_model_keys, (
        "Saved (brand, model_name) set does not match parse_brand_listing output:\n"
        f"  saved   ={sorted(saved_brand_model)}\n"
        f"  expected={sorted(expected_brand_model_keys)}"
    )

    # The hyphenated brand specifically must be present (Requirement 2.2 / 7.4).
    saved_brands = {b for (b, _) in saved_brand_model}
    assert "tcl-rowa" in saved_brands, (
        f"Hyphenated brand 'tcl-rowa' missing from DB. Got brands: {saved_brands}"
    )

    # ------------------------------------------------------------------
    # Assertion 2: coverage JSON is well-formed (Requirements 4.2, 4.4).
    # ------------------------------------------------------------------
    coverage_path = data_dir / "coverage_smoke-test.json"
    assert coverage_path.exists(), f"Coverage JSON not written at {coverage_path}"

    report = json.loads(coverage_path.read_text(encoding="utf-8"))
    assert report.get("run_id") == "smoke-test"
    assert isinstance(report.get("brands"), dict), report

    brand_entry = report["brands"].get("tcl-rowa")
    assert brand_entry is not None, (
        f"Brand 'tcl-rowa' missing in coverage report. Brands: "
        f"{sorted(report['brands'].keys())}"
    )

    assert isinstance(brand_entry["discovered_total"], int)
    assert brand_entry["discovered_total"] > 0, brand_entry
    assert isinstance(brand_entry["after_dedup"], int), brand_entry
    assert isinstance(brand_entry["saved"], int), brand_entry
    assert brand_entry["saved"] == len(expected_brand_model_keys), brand_entry
    assert (
        brand_entry["diff"]
        == brand_entry["discovered_total"] - brand_entry["saved"]
    ), brand_entry
    assert isinstance(brand_entry["failures"], list), brand_entry
    # Every failure entry, if any, should carry the documented shape.
    for failure in brand_entry["failures"]:
        assert {"url", "kind", "message"}.issubset(failure.keys()), failure
