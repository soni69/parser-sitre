"""Shared utilities: logging, delays, HTTP helpers."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from functools import wraps
from typing import Awaitable, Callable, Optional, TypeVar
from urllib.parse import urljoin, urlparse

from config import (
  BASE_URL,
  LOGS_DIR,
  MAX_DELAY_SEC,
  MIN_DELAY_SEC,
  RETRY_BACKOFF_BASE,
  USER_AGENTS,
)

T = TypeVar("T")


def setup_logging(level: int = logging.INFO) -> logging.Logger:
  LOGS_DIR.mkdir(parents=True, exist_ok=True)
  logger = logging.getLogger("telspb_scraper")
  if logger.handlers:
    return logger

  logger.setLevel(level)
  fmt = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
  )

  fh = logging.FileHandler(LOGS_DIR / "scraper.log", encoding="utf-8")
  fh.setFormatter(fmt)
  logger.addHandler(fh)

  ch = logging.StreamHandler()
  ch.setFormatter(fmt)
  logger.addHandler(ch)

  return logger


def random_user_agent() -> str:
  return random.choice(USER_AGENTS)


async def polite_delay() -> None:
  await asyncio.sleep(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC))


def normalize_url(href: str, base: str = BASE_URL) -> str:
  if not href:
    return ""
  href = href.strip()
  if href.startswith("//"):
    href = "https:" + href
  return urljoin(base, href)


def canonical_path(url: str) -> str:
  parsed = urlparse(url)
  path = parsed.path.rstrip("/") or "/"
  return path.lower()


def slugify_model(text: str) -> str:
  return re.sub(r"[^a-z0-9]+", "", text.lower())


# Значения-заглушки с сайта → пустая ячейка в CSV/БД
EMPTY_MARKERS = frozenset({
  "noname",
  "no name",
  "n/a",
  "na",
  "-",
  "—",
  "none",
  "null",
  "unknown",
  "нет",
  "н/д",
  "н.д.",
  "отсутствует",
  "not available",
})


def is_empty_placeholder(text: str) -> bool:
  if not text or not str(text).strip():
    return True
  norm = re.sub(r"\s+", " ", str(text).strip().lower())
  return norm in EMPTY_MARKERS or norm.replace(" ", "") == "noname"


# Частые русские слова в значениях → английские (до транслитерации)
_RU_EN_REPLACEMENTS: tuple[tuple[str, str], ...] = (
  (r"\bесть\b", "yes"),
  (r"\bнет\b", "no"),
  (r"\bили\b", "or"),
  (r"\bлибо\b", "or"),
  (r"\bсм\b", "cm"),
  (r"\bмм\b", "mm"),
  (r"\bкг\b", "kg"),
  (r"\bвт\b", "W"),
  (r"\bгц\b", "Hz"),
  (r"\bс\s+подставкой\b", "with stand"),
  (r"\bбез\s+подставки\b", "without stand"),
  (r"\bпамятью\s+на\b", "memory"),
)


def latinize_export(text: str) -> str:
  """Кириллица → латиница (ASCII) для CSV/DB, без ошибок в Excel и старых системах."""
  if not text:
    return ""
  text = re.sub(r"\s+", " ", str(text)).strip()
  text = re.sub(r"\.\s*Ремонт[^.]*$", "", text, flags=re.I).strip(" .")
  text = re.sub(r"\.\s*Remont[^.]*$", "", text, flags=re.I).strip(" .")
  for pattern, repl in _RU_EN_REPLACEMENTS:
    text = re.sub(pattern, repl, text, flags=re.I)
  try:
    from unidecode import unidecode

    text = unidecode(text)
  except ImportError:
    text = text.encode("ascii", errors="ignore").decode("ascii")
  text = re.sub(r"\s+", " ", text).strip()
  return text


def clean_value(text: str) -> str:
  if not text:
    return ""
  text = re.sub(r"\s+", " ", str(text)).strip()
  text = re.sub(r"^(?:либо|или)\s+", "", text, flags=re.I)
  text = text.strip(" ,;:/")
  text = re.sub(r"\bNoName\b", "", text, flags=re.I)
  text = re.sub(r"\s+", " ", text).strip(" ,;:/")
  if is_empty_placeholder(text):
    return ""
  return text


def split_model_names(raw: str) -> list[str]:
  raw = clean_value(raw)
  if not raw:
    return []
  parts = re.split(r"\s+(?:/|\||,)\s+|\s{2,}|\s+и\s+", raw, flags=re.I)
  if len(parts) == 1:
    parts = re.findall(r"[A-Z]{1,3}[A-Z0-9][A-Z0-9._-]*", raw) or [raw]
  return [clean_value(p) for p in parts if clean_value(p)]


def label_matches(label: str, aliases: tuple[str, ...]) -> bool:
  norm = re.sub(r"[^a-z0-9а-яё]+", " ", label.lower()).strip()
  for alias in aliases:
    alias_norm = re.sub(r"[^a-z0-9а-яё]+", " ", alias.lower()).strip()
    if norm == alias_norm or norm.startswith(alias_norm):
      return True
  return False


def retry_async(
  max_retries: int = 3,
  backoff_base: float = RETRY_BACKOFF_BASE,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
  def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    @wraps(func)
    async def wrapper(*args, **kwargs) -> T:
      logger = logging.getLogger("telspb_scraper")
      last_exc: Optional[Exception] = None
      for attempt in range(1, max_retries + 1):
        try:
          return await func(*args, **kwargs)
        except asyncio.CancelledError:
          raise
        except Exception as exc:  # noqa: BLE001
          last_exc = exc
          # 4xx (кроме 429) — повторять бессмысленно
          status = getattr(exc, "status", None)
          if status is not None and 400 <= status < 500 and status != 429:
            break
          if attempt >= max_retries:
            break
          wait = backoff_base ** attempt + random.uniform(0.2, 0.8)
          logger.warning(
            "%s failed (attempt %s/%s): %s — retry in %.1fs",
            func.__name__,
            attempt,
            max_retries,
            exc,
            wait,
          )
          await asyncio.sleep(wait)
      assert last_exc is not None
      raise last_exc

    return wrapper

  return decorator


# Brand-slug-aware Model_Page URL matcher (design section 3).
#
# The regex below allows hyphenated brand slugs (`[a-z0-9][a-z0-9-]*?`) — unlike
# the legacy `MODEL_PAGE_RE` in `config.py` which required `[a-z0-9]+`. The lazy
# quantifier makes the brand portion the SHORTEST viable slug; this is correct
# for any single-hyphen brand and is best-effort for unknown brands. When the
# caller supplies `known_brand_slugs`, ambiguity (e.g. `tcl-rowa-32led` when
# both `tcl` and `tcl-rowa` are known brands) is resolved by preferring the
# LONGEST matching known slug.
MODEL_PATH_RE = re.compile(
  r"^/remont-tv-lcd/(?P<brand>[a-z0-9][a-z0-9-]*?)-(?P<model>[a-z0-9][a-z0-9_-]*)$",
  re.IGNORECASE,
)

_MODEL_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def match_model_url(
  path: str,
  known_brand_slugs: Optional[set[str]] = None,
) -> Optional[tuple[str, str]]:
  """Parse a Model_Page URL path into ``(brand_slug, model_slug)`` or ``None``.

  Strategy:
    1. If ``known_brand_slugs`` is provided, try the LONGEST slug that
       prefixes the segment after ``/remont-tv-lcd/`` and is followed by ``-``
       and a non-empty model slug. This unambiguously resolves cases like
       ``tcl-rowa-32led`` when both ``tcl`` and ``tcl-rowa`` are known brands.
    2. Otherwise fall back to the lazy regex ``MODEL_PATH_RE``. The lazy
       quantifier makes the brand portion the shortest viable slug; this is
       correct for any single-hyphen scheme and is documented as best-effort
       for unknown brands.

  Paths that contain a ``/`` after ``/remont-tv-lcd/<slug>`` are rejected —
  those are Sub_Pages, not Model_Pages.
  """
  if not path:
    return None

  p = path.lower().rstrip("/")
  if not p.startswith("/remont-tv-lcd/"):
    return None

  tail = p[len("/remont-tv-lcd/"):]
  if not tail:
    return None
  if "/" in tail:  # this is a sub-page, not a model page
    return None

  if known_brand_slugs:
    # Normalise the known set to lower-case once; callers may pass mixed case.
    candidates = sorted(
      (s.lower() for s in known_brand_slugs if tail.startswith(s.lower() + "-")),
      key=len,
      reverse=True,
    )
    for slug in candidates:
      model = tail[len(slug) + 1:]
      if model and _MODEL_SLUG_RE.fullmatch(model):
        return slug, model

  m = MODEL_PATH_RE.match("/" + p.lstrip("/"))
  if not m:
    return None
  return m.group("brand"), m.group("model")


def normalize_model_name(name: str) -> str:
  """Lower-cased, whitespace-collapsed model name for dedup keys."""
  return re.sub(r"\s+", " ", (name or "").strip()).lower()
