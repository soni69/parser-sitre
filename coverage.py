"""Per-brand coverage tracking and JSON report emission.

See design.md section 6 for the full rationale. The tracker is a thin
state machine over plain dicts: discovery counts are accumulated by source
(`sitemap`, `brand_page`, `sub_page`), the post-dedup count is recorded once
per brand, and per-URL failures are appended in order. `finalize` reads the
saved-row count back from `storage.count_saved_for_brand(brand)` and emits one
INFO log line per brand; `to_json` serialises the report under the run id.
"""

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
    """Mutable counters and failure log for a single brand."""

    discovered: dict[str, int] = field(default_factory=dict)  # source -> count
    after_dedup: int = 0
    saved: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)


class CoverageTracker:
    """Collects per-brand discovery / dedup / failure metrics for one run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._brands: dict[str, _BrandStats] = {}

    def _get(self, brand: str) -> _BrandStats:
        return self._brands.setdefault(brand.lower(), _BrandStats())

    def record_discovered(self, brand: str, source: Source, count: int) -> None:
        """Add `count` Model_Refs discovered for `brand` from `source`."""
        stats = self._get(brand)
        stats.discovered[source] = stats.discovered.get(source, 0) + count

    def record_after_dedup(self, brand: str, count: int) -> None:
        """Set the post-dedup ref count for `brand` (overwrites prior value)."""
        self._get(brand).after_dedup = count

    def record_failure(
        self, brand: str, url: str, kind: FailureKind, message: str
    ) -> None:
        """Append a failure record (`url`, `kind`, truncated `message`) for `brand`."""
        self._get(brand).failures.append(
            {"url": url, "kind": kind, "message": message[:240]}
        )

    def finalize(self, storage) -> dict[str, dict]:
        """Read saved counts from `storage`, emit INFO logs, return the report dict.

        For every tracked brand, calls ``storage.count_saved_for_brand(brand)``
        and produces a per-brand entry containing the discovered breakdown,
        totals, post-dedup count, saved count, the discovered/saved diff, and
        the in-order list of failures.
        """
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
        """Write `{"run_id": ..., "brands": report}` as UTF-8 JSON to `path`."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"run_id": self.run_id, "brands": report},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
