"""Persistence: JSON Lines, CSV, SQLite, resume state."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Set

from config import (
  CSV_DIR,
  CSV_FILE,
  DATA_DIR,
  JSON_DIR,
  JSONL_FILE,
  LOGS_DIR,
  PREVIEW_DIR,
  RAW_DIR,
  RESUME_FILE,
  SQLITE_FILE,
  WRITE_CSV_ON_SAVE,
  WRITE_JSONL_ON_SAVE,
)
from models import TVRepairData
from utils import canonical_path, is_empty_placeholder, latinize_export, setup_logging

logger = setup_logging()

# Excel (RU/EU) ожидает точку с запятой; UTF-8 BOM — utf-8-sig
CSV_DELIMITER = ";"


def _cell(value: Any) -> str:
  if value is None:
    return ""
  text = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
  text = re.sub(r"\s+", " ", text).strip()
  if is_empty_placeholder(text):
    return ""
  return latinize_export(text)


def _format_other_parts(parts: dict[str, str] | None) -> str:
  if not parts:
    return ""
  return " | ".join(f"{_cell(k)}: {_cell(v)}" for k, v in parts.items())


def _parse_json_field(raw: Any, default: Any) -> Any:
  if raw is None or raw == "":
    return default
  if isinstance(raw, (dict, list)):
    return raw
  try:
    return json.loads(raw)
  except (json.JSONDecodeError, TypeError):
    return default


class StorageManager:
  """Writes scraped records to JSONL, CSV, SQLite; tracks resume progress."""

  CSV_COLUMNS = [
    "brand",
    "model_name",
    "full_title",
    "preview",
    "chassis",
    "panel",
    "panel_diagonal",
    "panel_resolution",
    "panel_active_area",
    "panel_brightness",
    "panel_contrast",
    "panel_display_colors",
    "panel_frequency",
    "panel_lamp_type",
    "panel_voltage",
    "backlight",
    "inverter",
    "t-con",
    "tuner",
    "mainboard",
    "mainboard_ic",
    "psu",
    "pwm_power",
    "other_parts",
    "year",
  ]

  # CSV header -> dataclass field
  CSV_FIELD_MAP = {
    "preview": "preview_image",
    "t-con": "tcon",
  }

  def __init__(
    self,
    run_id: str = "latest",
    *,
    write_csv: bool = WRITE_CSV_ON_SAVE,
    write_jsonl: bool = WRITE_JSONL_ON_SAVE,
  ) -> None:
    self.run_id = run_id
    self.write_csv = write_csv
    self.write_jsonl = write_jsonl
    self._ensure_dirs()
    self.jsonl_path = JSONL_FILE if run_id == "latest" else JSON_DIR / f"tv_repairs_{run_id}.jsonl"
    self.csv_path = CSV_FILE if run_id == "latest" else CSV_DIR / f"tv_repairs_{run_id}.csv"
    self.db_path = SQLITE_FILE
    self._csv_header_written: set[str] = set()
    self._csv_lock_logged = False
    self._csv_skip = not write_csv
    self._active_csv_path = self.csv_path
    if write_csv and self.csv_path.exists() and self.csv_path.stat().st_size > 0:
      self._csv_header_written.add(str(self.csv_path.resolve()))
    self._csv_fallback_path = self.csv_path.with_name(
      f"{self.csv_path.stem}_live{self.csv_path.suffix}"
    )
    self._init_sqlite()
    self._done_keys: Set[str] = self._load_resume_keys()
    logger.info("Storage: SQLite %s (%s rows)", self.db_path, self.done_count)

  @property
  def active_csv_path(self) -> Path:
    return self._active_csv_path

  @property
  def csv_used_fallback(self) -> bool:
    return self._active_csv_path != self.csv_path

  @property
  def csv_disabled(self) -> bool:
    return self._csv_skip

  def _ensure_dirs(self) -> None:
    for d in (DATA_DIR, JSON_DIR, CSV_DIR, RAW_DIR, LOGS_DIR, PREVIEW_DIR):
      d.mkdir(parents=True, exist_ok=True)

  def _init_sqlite(self) -> None:
    with sqlite3.connect(self.db_path) as conn:
      conn.execute("PRAGMA journal_mode=WAL")
      conn.execute("PRAGMA synchronous=NORMAL")
      conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tv_repairs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          brand TEXT NOT NULL,
          model_name TEXT NOT NULL,
          full_title TEXT,
          chassis TEXT,
          panel TEXT,
          backlight TEXT,
          inverter TEXT,
          tcon TEXT,
          tuner TEXT,
          mainboard TEXT,
          mainboard_ic TEXT,
          psu TEXT,
          pwm_power TEXT,
          panel_diagonal TEXT,
          panel_resolution TEXT,
          panel_active_area TEXT,
          panel_brightness TEXT,
          panel_contrast TEXT,
          panel_display_colors TEXT,
          panel_frequency TEXT,
          panel_lamp_type TEXT,
          panel_voltage TEXT,
          preview_image TEXT,
          other_parts TEXT,
          year TEXT,
          UNIQUE(brand, model_name)
        )
        """
      )
      self._migrate_sqlite(conn)
      conn.execute("CREATE INDEX IF NOT EXISTS idx_tv_brand ON tv_repairs(brand)")
      conn.commit()

  def _migrate_sqlite(self, conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tv_repairs)")}
    new_cols = (
      "tuner",
      "panel_diagonal",
      "panel_resolution",
      "panel_active_area",
      "panel_brightness",
      "panel_contrast",
      "panel_display_colors",
      "panel_frequency",
      "panel_lamp_type",
      "panel_voltage",
      "preview_image",
    )
    for col in new_cols:
      if col not in existing:
        conn.execute(f"ALTER TABLE tv_repairs ADD COLUMN {col} TEXT")

    # Drop removed columns by rebuilding table if they still exist
    removed_cols = {"url", "parsed_at", "raw_html_snippet"}
    if removed_cols & existing:
      keep_cols = [
        row[1] for row in conn.execute("PRAGMA table_info(tv_repairs)")
        if row[1] not in removed_cols
      ]
      cols_str = ", ".join(keep_cols)
      conn.execute(f"CREATE TABLE IF NOT EXISTS tv_repairs_new AS SELECT {cols_str} FROM tv_repairs")
      conn.execute("DROP TABLE tv_repairs")
      conn.execute("ALTER TABLE tv_repairs_new RENAME TO tv_repairs")
      # Recreate the unique constraint and index
      # SQLite doesn't support adding constraints after rename, so create unique index
      conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tv_brand_model ON tv_repairs(brand, model_name)")
      conn.execute("CREATE INDEX IF NOT EXISTS idx_tv_brand ON tv_repairs(brand)")

  def _load_resume_keys(self) -> Set[str]:
    done: Set[str] = set()
    if RESUME_FILE.exists():
      try:
        data = json.loads(RESUME_FILE.read_text(encoding="utf-8"))
        done.update(data.get("done_keys", []))
        # Backward compat: migrate old done_urls to done_keys
        done.update(data.get("done_urls", []))
      except json.JSONDecodeError:
        logger.warning("Corrupt resume file, starting fresh list merge")

    with sqlite3.connect(self.db_path) as conn:
      for (brand, model_name) in conn.execute("SELECT brand, model_name FROM tv_repairs"):
        done.add(f"{brand}:{model_name}".lower())
    return done

  def is_done(self, brand: str, model_name: str) -> bool:
    return f"{brand}:{model_name}".lower() in self._done_keys

  def mark_done(self, brand: str, model_name: str) -> None:
    self._done_keys.add(f"{brand}:{model_name}".lower())

  def flush_resume(self) -> None:
    RESUME_FILE.write_text(
      json.dumps({"done_keys": sorted(self._done_keys)}, ensure_ascii=False, indent=2),
      encoding="utf-8",
    )

  def save_raw_html(self, url: str, html: str) -> None:
    slug = canonical_path(url).strip("/").replace("/", "_")
    path = RAW_DIR / f"{slug}.html"
    path.write_text(html, encoding="utf-8")
    logger.debug("Saved raw HTML: %s", path.name)

  def save(self, item: TVRepairData) -> None:
    item = self._latinize_item(item)
    self._upsert_sqlite(item)
    if self.write_jsonl:
      self._append_jsonl(item)
    if self.write_csv:
      self._append_csv_safe(item)
    self.mark_done(item.brand, item.model_name)

  def _latinize_item(self, item: TVRepairData) -> TVRepairData:
    """Все текстовые поля — латиница; preview без изменений."""
    d = item.to_dict()
    skip = {"preview_image"}
    for key, val in d.items():
      if key in skip:
        continue
      if isinstance(val, str):
        d[key] = latinize_export(val)
      elif isinstance(val, dict):
        d[key] = {
          latinize_export(str(k)): latinize_export(str(v))
          for k, v in val.items()
          if latinize_export(str(v))
        }
    return TVRepairData.from_dict(d)

  def save_batch(self, items: Iterable[TVRepairData]) -> None:
    for item in items:
      self.save(item)

  def _append_jsonl(self, item: TVRepairData) -> None:
    with self.jsonl_path.open("a", encoding="utf-8") as f:
      f.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")

  def _append_csv_safe(self, item: TVRepairData) -> None:
    if self._csv_skip:
      return
    try:
      self._append_csv_to(self._active_csv_path, item)
    except OSError as exc:
      if exc.errno != 13 and not isinstance(exc, PermissionError):
        raise
      self._switch_csv_after_lock(item, exc)

  def _switch_csv_after_lock(self, item: TVRepairData, exc: OSError) -> None:
    if self._active_csv_path == self.csv_path:
      self._active_csv_path = self._csv_fallback_path
      if not self._csv_lock_logged:
        logger.warning(
          "CSV заблокирован (%s): %s. Закройте tv_repairs.csv в Excel. "
          "Парсинг продолжается; CSV пишется в %s. SQLite и JSONL без изменений.",
          self.csv_path,
          exc,
          self._csv_fallback_path,
        )
        self._csv_lock_logged = True
      try:
        self._append_csv_to(self._active_csv_path, item)
      except OSError as exc2:
        if not self._csv_skip:
          logger.error(
            "CSV отключён (%s). Данные только в %s и %s. "
            "После парсинга: python main.py --export-csv",
            exc2,
            self.db_path,
            self.jsonl_path,
          )
          self._csv_skip = True
    elif not self._csv_skip:
      logger.error("CSV отключён: %s", exc)
      self._csv_skip = True

  def _append_csv_to(self, path: Path, item: TVRepairData) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    row = self._csv_row(item)
    with path.open("a", encoding="utf-8-sig", newline="") as f:
      writer = csv.DictWriter(
        f,
        fieldnames=self.CSV_COLUMNS,
        delimiter=CSV_DELIMITER,
        quoting=csv.QUOTE_MINIMAL,
        extrasaction="ignore",
      )
      if key not in self._csv_header_written:
        writer.writeheader()
        self._csv_header_written.add(key)
      writer.writerow(row)

  def _csv_row(self, item: TVRepairData) -> dict[str, str]:
    d = item.to_dict()
    row: dict[str, str] = {}
    for col in self.CSV_COLUMNS:
      field = self.CSV_FIELD_MAP.get(col, col)
      val = d.get(field)
      if col == "other_parts":
        row[col] = _format_other_parts(_parse_json_field(val, {}))
      else:
        row[col] = _cell(val)
    return row

  def export_csv_from_db(self, output_path: Path | None = None) -> Path:
    """Пересобрать CSV из SQLite (удобно после исправления формата)."""
    path = output_path or self.csv_path
    path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(self.db_path) as conn:
      conn.row_factory = sqlite3.Row
      rows = conn.execute(
        "SELECT * FROM tv_repairs ORDER BY brand, model_name"
      ).fetchall()

    with path.open("w", encoding="utf-8-sig", newline="") as f:
      writer = csv.DictWriter(
        f,
        fieldnames=self.CSV_COLUMNS,
        delimiter=CSV_DELIMITER,
        quoting=csv.QUOTE_MINIMAL,
        extrasaction="ignore",
      )
      writer.writeheader()
      for row in rows:
        writer.writerow(self._csv_row_from_sqlite(dict(row)))

    logger.info("Exported %s rows to %s", len(rows), path)
    return path

  def _csv_row_from_sqlite(self, row: dict[str, Any]) -> dict[str, str]:
    return self._csv_row(TVRepairData.from_dict(dict(row)))

  def _upsert_sqlite(self, item: TVRepairData) -> None:
    d = item.to_dict()
    with sqlite3.connect(self.db_path) as conn:
      conn.execute(
        """
        INSERT INTO tv_repairs (
          brand, model_name, full_title, chassis, panel, backlight,
          inverter, tcon, tuner, mainboard, mainboard_ic, psu, pwm_power,
          panel_diagonal, panel_resolution, panel_active_area, panel_brightness,
          panel_contrast, panel_display_colors, panel_frequency, panel_lamp_type,
          panel_voltage, preview_image, other_parts, year
        ) VALUES (
          :brand, :model_name, :full_title, :chassis, :panel, :backlight,
          :inverter, :tcon, :tuner, :mainboard, :mainboard_ic, :psu, :pwm_power,
          :panel_diagonal, :panel_resolution, :panel_active_area, :panel_brightness,
          :panel_contrast, :panel_display_colors, :panel_frequency, :panel_lamp_type,
          :panel_voltage, :preview_image, :other_parts, :year
        )
        ON CONFLICT(brand, model_name) DO UPDATE SET
          full_title=excluded.full_title,
          chassis=excluded.chassis,
          panel=excluded.panel,
          backlight=excluded.backlight,
          inverter=excluded.inverter,
          tcon=excluded.tcon,
          tuner=excluded.tuner,
          mainboard=excluded.mainboard,
          mainboard_ic=excluded.mainboard_ic,
          psu=excluded.psu,
          pwm_power=excluded.pwm_power,
          panel_diagonal=excluded.panel_diagonal,
          panel_resolution=excluded.panel_resolution,
          panel_active_area=excluded.panel_active_area,
          panel_brightness=excluded.panel_brightness,
          panel_contrast=excluded.panel_contrast,
          panel_display_colors=excluded.panel_display_colors,
          panel_frequency=excluded.panel_frequency,
          panel_lamp_type=excluded.panel_lamp_type,
          panel_voltage=excluded.panel_voltage,
          preview_image=excluded.preview_image,
          other_parts=excluded.other_parts,
          year=excluded.year
        """,
        {
          **{k: d.get(k) for k in (
            "brand", "model_name", "full_title", "chassis", "panel",
            "backlight", "inverter", "tcon", "tuner", "mainboard", "mainboard_ic",
            "psu", "pwm_power", "panel_diagonal", "panel_resolution",
            "panel_active_area", "panel_brightness", "panel_contrast",
            "panel_display_colors", "panel_frequency", "panel_lamp_type",
            "panel_voltage", "preview_image", "year",
          )},
          "other_parts": json.dumps(d.get("other_parts") or {}, ensure_ascii=False),
        },
      )
      conn.commit()

  @property
  def done_count(self) -> int:
    return len(self._done_keys)

  def count_saved_for_brand(self, slug: str) -> int:
    """Count rows in tv_repairs whose brand matches `slug` case-insensitively."""
    with sqlite3.connect(self.db_path) as conn:
      (count,) = conn.execute(
        "SELECT COUNT(*) FROM tv_repairs WHERE LOWER(brand)=?",
        (slug.lower(),),
      ).fetchone()
    return int(count)
