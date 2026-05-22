"""Application configuration."""

from __future__ import annotations

from pathlib import Path

# Site
BASE_URL = "https://tel-spb.ru"
INDEX_PATH = "/remont-tv-lcd/"
INDEX_URL = f"{BASE_URL}{INDEX_PATH}"

# Concurrency & politeness
CONCURRENCY = 6
MIN_DELAY_SEC = 0.8
MAX_DELAY_SEC = 2.0
REQUEST_TIMEOUT_SEC = 45
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.5

# Resume & debug
RAW_HTML_LIMIT = 50

# Paths (relative to project root)
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PREVIEW_DIR = DATA_DIR / "previews"
JSON_DIR = DATA_DIR / "json"
CSV_DIR = DATA_DIR / "csv"
LOGS_DIR = PROJECT_ROOT / "logs"
PREVIEW_SIZE = (300, 300)

JSONL_FILE = JSON_DIR / "tv_repairs.jsonl"
CSV_FILE = CSV_DIR / "tv_repairs.csv"
SQLITE_FILE = DATA_DIR / "tv_repairs.db"
RESUME_FILE = DATA_DIR / "resume_state.json"

# По умолчанию при парсинге — только SQLite (CSV/JSONL — по флагам CLI)
WRITE_CSV_ON_SAVE = False
WRITE_JSONL_ON_SAVE = False

DEFAULT_RUN_ID = "latest"

# HTTP
DEFAULT_HEADERS = {
  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
  "Connection": "keep-alive",
}

USER_AGENTS = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edg/122.0.0.0 Safari/537.36",
]

# URL patterns
BRAND_PAGE_RE = r"/remont-tv-lcd/([a-z0-9-]+)/?(?:led)?/?$"
MODEL_PAGE_RE = r"/remont-tv-lcd/([a-z0-9]+)-([a-z0-9][a-z0-9_-]*)"

# Discovery (sitemap + brand sub-page BFS)
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
MAX_BRAND_SUBPAGES = 200
MAX_BRAND_DEPTH = 3

# Fields mapped from page labels (RU/EN)
FIELD_LABEL_MAP = {
  "chassis": ("chassis", "chassis/version", "шасси", "chassis/version:"),
  "panel": ("panel", "панель", "матрица", "matrix"),
  "backlight": (
    "lamp backlight",
    "backlight",
    "подсветка",
    "подсветка ccfl/eefl",
    "led backlight",
  ),
  "inverter": ("inverter", "inverter (backlight)", "инвертор"),
  "tcon": ("t-con", "tcon", "t con"),
  "mainboard": ("mainboard", "main board", "основная плата"),
  "mainboard_ic": ("ic mainboard", "ic main board", "mainboard ic"),
  "psu": ("power supply", "power supply (psu)", "psu", "блок питания"),
  "pwm_power": ("pwm power", "pwm inverter", "pwm"),
  "year": ("год выпуска", "year", "год"),
  "tuner": ("tuner", "тюнер", "цифровой тюнер"),
}

# Panel datasheet block (EN labels in <br> separated paragraph)
PANEL_SPEC_LABEL_MAP = {
  "panel_diagonal": (
    "diagonal size",
    "panel diagonal",
    "диагональ",
    "диагональ экрана",
  ),
  "panel_resolution": ("resolution", "разрешение"),
  "panel_active_area": ("active area", "активная область"),
  "panel_brightness": ("brightness", "яркость", "brightness typ"),
  "panel_contrast": ("contrast", "контраст", "contrast ratio"),
  "panel_display_colors": (
    "display colors",
    "display colour",
    "colors",
    "colour",
    "цвета",
  ),
  "panel_frequency": ("frequency", "частота", "refresh rate"),
  "panel_lamp_type": ("lamp type", "тип ламп", "backlight type"),
  "panel_voltage": ("voltage", "напряжение", "operating voltage"),
}

OTHER_PART_KEYWORDS = (
  "mosfet",
  "eeprom",
  "wifi",
  "bluetooth",
  "ir",
  "кнопки",
  "динамики",
  "антенна",
  "subboard",
  "sub board",
  "плата",
)

