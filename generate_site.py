#!/usr/bin/env python3
"""
Генератор статического сайта-справочника ТВ-запчастей.
Стиль: WoodMart / BN94.ru

Создаёт полностью кликабельный сайт:
  - Главная (список брендов)
  - Страница бренда (список моделей)
  - Страница модели (характеристики + фото)

Запуск:
  python generate_site.py
  python generate_site.py --serve   (сгенерировать + запустить сервер)

Результат: site_output/
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

# Пути
PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "data" / "tv_repairs.db"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
OUTPUT_DIR = PROJECT_ROOT / "site_output"
PREVIEWS_SRC = PROJECT_ROOT / "data" / "previews"


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    return s.strip("-")[:200]


def load_data() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM tv_repairs ORDER BY brand, model_name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


SPEC_LABELS = {
    "chassis": "Шасси",
    "panel": "Панель",
    "backlight": "Подсветка",
    "inverter": "Инвертор",
    "tcon": "T-CON",
    "tuner": "Тюнер",
    "mainboard": "MainBoard",
    "mainboard_ic": "IC MainBoard",
    "psu": "Блок питания (PSU)",
    "pwm_power": "PWM Power",
}

PANEL_SPEC_LABELS = {
    "panel_diagonal": "Диагональ",
    "panel_resolution": "Разрешение",
    "panel_active_area": "Активная область",
    "panel_brightness": "Яркость",
    "panel_contrast": "Контраст",
    "panel_display_colors": "Цвета",
    "panel_frequency": "Частота",
    "panel_lamp_type": "Тип подсветки",
    "panel_voltage": "Напряжение",
}


def build_model_context(row: dict) -> dict:
    specs = [(label, row[field]) for field, label in SPEC_LABELS.items() if row.get(field)]
    panel_specs = [(label, row[field]) for field, label in PANEL_SPEC_LABELS.items() if row.get(field)]

    other_parts = []
    raw_other = row.get("other_parts")
    if raw_other:
        try:
            parts = json.loads(raw_other) if isinstance(raw_other, str) else raw_other
            if isinstance(parts, dict):
                other_parts = [(k, v) for k, v in parts.items() if v]
        except (json.JSONDecodeError, TypeError):
            pass

    preview_file = None
    if row.get("preview_image"):
        preview_file = os.path.basename(row["preview_image"])

    return {
        "brand": row["brand"],
        "brand_upper": row["brand"].upper(),
        "model_name": row["model_name"],
        "full_title": row.get("full_title") or f"{row['brand'].upper()} {row['model_name']}",
        "year": row.get("year"),
        "preview_file": preview_file,
        "slug": slugify(row["model_name"]),
        "specs": specs,
        "panel_specs": panel_specs,
        "other_parts": other_parts,
        "panel_diagonal": row.get("panel_diagonal"),
    }


def generate():
    print(f"Загрузка данных из {DB_PATH}...")
    rows = load_data()
    print(f"  {len(rows)} моделей загружено")

    # Группировка по брендам
    brands: dict[str, list[dict]] = {}
    for row in rows:
        brands.setdefault(row["brand"], []).append(row)

    # Очистка и подготовка
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)

    # Копируем превью
    previews_dst = OUTPUT_DIR / "previews"
    if PREVIEWS_SRC.exists():
        shutil.copytree(PREVIEWS_SRC, previews_dst)
        print(f"  Превью скопировано: {len(list(previews_dst.iterdir()))} файлов")

    # Jinja2
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
    tpl_index = env.get_template("index.html")
    tpl_brand = env.get_template("brand.html")
    tpl_model = env.get_template("model.html")

    # Главная
    brand_list = []
    for slug in sorted(brands.keys()):
        brand_list.append({
            "slug": slug,
            "name": slug.upper(),
            "count": len(brands[slug]),
        })

    index_html = tpl_index.render(
        brands=brand_list,
        total_models=len(rows),
        total_brands=len(brands),
    )
    (OUTPUT_DIR / "index.html").write_text(index_html, encoding="utf-8")

    # Бренды и модели
    pages = 1
    for brand_slug, model_rows in brands.items():
        brand_dir = OUTPUT_DIR / brand_slug
        brand_dir.mkdir(exist_ok=True)

        model_contexts = []
        for row in model_rows:
            ctx = build_model_context(row)
            model_contexts.append(ctx)

            # Страница модели
            model_html = tpl_model.render(model=ctx)
            (brand_dir / f"{ctx['slug']}.html").write_text(model_html, encoding="utf-8")
            pages += 1

        # Страница бренда
        brand_html = tpl_brand.render(
            brand_name=brand_slug.upper(),
            brand_slug=brand_slug,
            models=model_contexts,
        )
        (brand_dir / "index.html").write_text(brand_html, encoding="utf-8")
        pages += 1

    print(f"\n  Сгенерировано {pages} страниц")
    print(f"  Папка: {OUTPUT_DIR}")
    return OUTPUT_DIR


def serve(directory: Path, port: int = 8080):
    """Запустить локальный HTTP-сервер."""
    import http.server
    import socketserver

    os.chdir(directory)
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        url = f"http://localhost:{port}"
        print(f"\n  Сервер запущен: {url}")
        print(f"  Открой в браузере: {url}")
        print(f"  Для остановки: Ctrl+C\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Сервер остановлен.")


if __name__ == "__main__":
    out = generate()
    if "--serve" in sys.argv:
        serve(out)
    else:
        print(f"\n  Для просмотра в браузере запусти:")
        print(f"    python generate_site.py --serve")
        print(f"  Затем открой: http://localhost:8080")
