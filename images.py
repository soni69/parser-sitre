"""Download and resize TV preview images to WebP."""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from typing import Optional

import aiohttp
from PIL import Image

from config import BASE_URL, PREVIEW_DIR, PREVIEW_SIZE
from utils import random_user_agent

logger = logging.getLogger("telspb_scraper")


def preview_filename(brand: str, model_name: str) -> str:
  slug = f"{brand.lower()}-{model_name.lower()}"
  slug = "".join(c if c.isalnum() or c in "-_" else "" for c in slug)
  return f"{slug}.webp"


def preview_path(brand: str, model_name: str) -> Path:
  return PREVIEW_DIR / preview_filename(brand, model_name)


async def fetch_and_save_preview(
  session: aiohttp.ClientSession,
  image_url: str,
  dest: Path,
  *,
  size: tuple[int, int] = PREVIEW_SIZE,
) -> Optional[str]:
  """Download image, resize to size, save WebP. Returns relative path for CSV."""
  if not image_url:
    return None
  dest.parent.mkdir(parents=True, exist_ok=True)
  if dest.exists() and dest.stat().st_size > 0:
    return dest.relative_to(PREVIEW_DIR.parent).as_posix()

  headers = {"User-Agent": random_user_agent()}
  try:
    async with session.get(image_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
      if resp.status != 200:
        logger.debug("Preview HTTP %s: %s", resp.status, image_url)
        return None
      data = await resp.read()
  except Exception as exc:  # noqa: BLE001
    logger.debug("Preview download failed %s: %s", image_url, exc)
    return None

  try:
    img = Image.open(BytesIO(data))
    if img.mode not in ("RGB", "RGBA"):
      img = img.convert("RGB")
    elif img.mode == "RGBA":
      bg = Image.new("RGB", img.size, (255, 255, 255))
      bg.paste(img, mask=img.split()[3])
      img = bg
    img.thumbnail(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, (255, 255, 255))
    ox = (size[0] - img.width) // 2
    oy = (size[1] - img.height) // 2
    canvas.paste(img, (ox, oy))
    canvas.save(dest, format="WEBP", quality=85, method=6)
    return dest.relative_to(PREVIEW_DIR.parent).as_posix()
  except Exception as exc:  # noqa: BLE001
    logger.debug("Preview convert failed %s: %s", image_url, exc)
    return None


def resolve_image_url(src: str, page_url: str) -> str:
  from urllib.parse import urljoin

  if not src:
    return ""
  if src.startswith("//"):
    return "https:" + src
  if src.startswith("http"):
    return src
  base = page_url.rsplit("/", 1)[0] + "/"
  if src.startswith("/"):
    return urljoin(BASE_URL, src)
  return urljoin(base, src)
