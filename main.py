#!/usr/bin/env python3
"""CLI entry point for tel-spb.ru TV repair scraper."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections import defaultdict
from tqdm.asyncio import tqdm

from config import CONCURRENCY, DATA_DIR, RAW_HTML_LIMIT, SQLITE_FILE
from coverage import CoverageTracker
from discovery import discover_models_from_sitemap
from models import ModelRef
from scraper import TelSpbScraper, build_scraper
from storage import StorageManager
from utils import canonical_path, normalize_model_name, setup_logging

logger = setup_logging()


# Composite dedup key: (brand_lc, normalized_model_name, canonical_path).
# See design.md section 4 — refs differing in any single coordinate are kept.
DedupKey = tuple[str, str, str]


def _dedup_key(ref: ModelRef) -> DedupKey:
  return (
    ref.brand.lower(),
    normalize_model_name(ref.model_name),
    canonical_path(ref.url),
  )


def parse_args() -> argparse.Namespace:
  p = argparse.ArgumentParser(description="Scrape tel-spb.ru TV repair database")
  p.add_argument("--brand", type=str, help="Scrape only this brand slug (e.g. samsung)")
  p.add_argument("--max-models", type=int, default=0, help="Limit models per run (0 = all)")
  p.add_argument("--only-led", action="store_true", help="Only LED brand sub-pages (/brand/led)")
  p.add_argument("--resume", action="store_true", help="Skip URLs already in DB/resume file")
  p.add_argument("--concurrency", type=int, default=CONCURRENCY, help="Max parallel requests")
  p.add_argument(
    "--no-sitemap",
    action="store_true",
    help="Skip sitemap.xml discovery (debug brand-page BFS)",
  )
  p.add_argument(
    "--run-id",
    type=str,
    default="",
    help="Суффикс для опциональных json/csv (--with-jsonl / --with-csv)",
  )
  p.add_argument(
    "--with-csv",
    action="store_true",
    help="Дополнительно писать CSV при парсинге (по умолчанию только SQLite)",
  )
  p.add_argument(
    "--with-jsonl",
    action="store_true",
    help="Дополнительно писать JSONL при парсинге",
  )
  p.add_argument(
    "--export-csv",
    action="store_true",
    help="Экспорт CSV из SQLite (без парсинга), для Excel",
  )
  p.add_argument(
    "--export-csv-path",
    type=str,
    default="",
    help="Куда сохранить CSV при --export-csv (по умолчанию data/csv/tv_repairs.csv)",
  )
  return p.parse_args()


def _write_coverage_report(tracker: CoverageTracker, storage: StorageManager) -> None:
  """Finalize coverage tracker and write JSON report; never raise."""
  try:
    report = tracker.finalize(storage)
    out_path = DATA_DIR / f"coverage_{tracker.run_id}.json"
    tracker.to_json(out_path, report)
    logger.info("Coverage report: %s", out_path)
  except Exception:  # noqa: BLE001
    logger.exception("Failed to write coverage report")


async def run_scraper(args: argparse.Namespace) -> int:
  run_id = args.run_id or "latest"
  storage = StorageManager(
    run_id=run_id,
    write_csv=args.with_csv,
    write_jsonl=args.with_jsonl,
  )
  tracker = CoverageTracker(run_id)
  shutdown = asyncio.Event()
  raw_count = 0

  def on_signal(*_):
    logger.warning("Shutdown signal received — finishing current tasks…")
    shutdown.set()

  loop = asyncio.get_running_loop()
  for sig in (signal.SIGINT, signal.SIGTERM):
    try:
      loop.add_signal_handler(sig, on_signal)
    except NotImplementedError:
      signal.signal(sig, lambda *_: on_signal())

  def save_raw(url: str, html: str) -> None:
    nonlocal raw_count
    if raw_count < RAW_HTML_LIMIT:
      storage.save_raw_html(url, html)
      raw_count += 1

  session, _, scraper = build_scraper(
    concurrency=args.concurrency,
    only_led=args.only_led,
    save_raw_html=save_raw,
    shutdown_event=shutdown,
  )

  exit_code = 0
  try:
    brands = await scraper.discover_brands()
    if args.brand:
      slug = args.brand.strip().lower()
      brands = [(s, u) for s, u in brands if s == slug]
      if not brands:
        brands = [(slug, f"https://tel-spb.ru/remont-tv-lcd/{slug}/")]
        # Make sure the requested slug is recognised by parse_brand_listing.
        scraper.set_known_slugs({slug, *scraper._known_slugs})

    # Sitemap-driven discovery once at run start (Requirement 1.1).
    sitemap_refs: list[ModelRef] = []
    if not args.no_sitemap:
      try:
        known_slugs = {s for s, _ in brands} | scraper._known_slugs
        sitemap_refs = await discover_models_from_sitemap(
          scraper.fetch_html, known_slugs
        )
        logger.info("Sitemap discovery: %s model refs", len(sitemap_refs))
      except asyncio.CancelledError:
        raise
      except Exception as exc:  # noqa: BLE001
        logger.error("Sitemap discovery failed: %s", exc)
        sitemap_refs = []

    all_refs: list[ModelRef] = []
    for brand_slug, brand_root_url in brands:
      if shutdown.is_set():
        break
      refs = await scraper.collect_model_refs(
        brand_slug, brand_root_url, sitemap_refs, tracker
      )
      all_refs.extend(refs)

    # Composite-key dedup (Requirements 3.1, 3.3, 3.4).
    unique: dict[DedupKey, ModelRef] = {}
    for ref in all_refs:
      unique[_dedup_key(ref)] = ref
    refs_list = list(unique.values())

    # Per-brand post-dedup count for the coverage report (Requirement 4.1).
    per_brand_counts: dict[str, int] = defaultdict(int)
    for ref in refs_list:
      per_brand_counts[ref.brand.lower()] += 1
    for brand_slug, count in per_brand_counts.items():
      tracker.record_after_dedup(brand_slug, count)

    if args.resume:
      refs_list = [r for r in refs_list if not storage.is_done(r.brand, r.model_name)]

    if args.max_models and args.max_models > 0:
      refs_list = refs_list[: args.max_models]

    logger.info(
      "Queue: %s models | resume=%s | done already=%s",
      len(refs_list),
      args.resume,
      storage.done_count,
    )

    progress = tqdm(total=len(refs_list), desc="Models", unit="model")

    async def worker(ref: ModelRef) -> None:
      if shutdown.is_set():
        return
      try:
        data = await scraper.scrape_model(ref)
        storage.save(data)
      except asyncio.CancelledError:
        raise
      except Exception as exc:  # noqa: BLE001
        logger.error("Failed %s: %s", ref.url, exc)
        tracker.record_failure(ref.brand, ref.url, "network", str(exc))
      finally:
        progress.update(1)

    chunk_size = args.concurrency
    for i in range(0, len(refs_list), chunk_size):
      if shutdown.is_set():
        break
      batch = refs_list[i : i + chunk_size]
      await asyncio.gather(*(worker(r) for r in batch))
      storage.flush_resume()

    progress.close()
    storage.flush_resume()
    logger.info(
      "Finished. SQLite: %s | records: %s",
      SQLITE_FILE,
      storage.done_count,
    )
    if not args.with_csv:
      logger.info("Excel/CSV: python main.py --export-csv")

  except Exception:
    logger.exception("Fatal error")
    exit_code = 1
  finally:
    # Coverage report always emitted, success or graceful shutdown
    # (Requirements 4.2, 4.3, 4.4).
    _write_coverage_report(tracker, storage)
    await session.close()

  return exit_code


def export_csv_only(args: argparse.Namespace) -> int:
  from pathlib import Path

  from config import CSV_FILE

  storage = StorageManager(run_id=args.run_id or "latest")
  out = Path(args.export_csv_path) if args.export_csv_path else CSV_FILE
  path = storage.export_csv_from_db(out)
  logger.info("Готово: %s", path)
  return 0


def main() -> None:
  args = parse_args()
  if args.export_csv:
    raise SystemExit(export_csv_only(args))
  code = asyncio.run(run_scraper(args))
  raise SystemExit(code)


if __name__ == "__main__":
  main()
