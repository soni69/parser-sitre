#!/usr/bin/env python3
"""
Генератор WXR (WordPress eXtended RSS) из SQLite базы ТВ-запчастей.

Создаёт XML-файл для импорта в WordPress через:
  Инструменты → Импорт → WordPress

Каждая модель ТВ становится записью Custom Post Type 'tv_model'.
Характеристики сохраняются как мета-поля (custom fields).

Использование:
  python generate_wxr.py

Результат: data/wp_import/tv_models_import.xml
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "data" / "tv_repairs.db"
OUTPUT_DIR = PROJECT_ROOT / "data" / "wp_import"
OUTPUT_FILE = OUTPUT_DIR / "tv_models_import.xml"

# Сайт
SITE_URL = "https://bn94.ru"
AUTHOR_LOGIN = "admin"
AUTHOR_DISPLAY = "Admin"


def slugify(text: str) -> str:
    """Транслит + slug для WordPress."""
    # Простая транслитерация
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")[:200]


def make_post_slug(brand: str, model_name: str) -> str:
    """Создать slug для записи."""
    raw = f"{brand}-{model_name}"
    return slugify(raw)


def escape_cdata(text: str) -> str:
    """Обернуть в CDATA."""
    if not text:
        return "<![CDATA[]]>"
    # Экранируем ]]> внутри CDATA
    text = text.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{text}]]>"


def build_content_html(row: dict) -> str:
    """Сгенерировать HTML-контент записи (то что видит пользователь)."""
    parts = []

    # Заголовок
    title = row.get("full_title") or f"{row['brand'].upper()} {row['model_name']}"
    parts.append(f"<h2>Запчасти для {title}</h2>")

    # Превью
    if row.get("preview_image"):
        filename = os.path.basename(row["preview_image"])
        parts.append(
            f'<img src="{SITE_URL}/wp-content/uploads/tv-previews/{filename}" '
            f'alt="{title}" style="max-width:300px;" />'
        )

    # Таблица характеристик
    specs = [
        ("Шасси", row.get("chassis")),
        ("Панель", row.get("panel")),
        ("Подсветка", row.get("backlight")),
        ("Инвертор", row.get("inverter")),
        ("T-CON", row.get("tcon")),
        ("Тюнер", row.get("tuner")),
        ("MainBoard", row.get("mainboard")),
        ("IC MainBoard", row.get("mainboard_ic")),
        ("Блок питания (PSU)", row.get("psu")),
        ("PWM Power", row.get("pwm_power")),
    ]

    has_specs = any(v for _, v in specs)
    if has_specs:
        parts.append("<h3>Основные компоненты</h3>")
        parts.append('<table class="shop_attributes">')
        for label, val in specs:
            if val:
                parts.append(f"<tr><th>{label}</th><td>{escape(val)}</td></tr>")
        parts.append("</table>")

    # Характеристики панели
    panel_specs = [
        ("Диагональ", row.get("panel_diagonal")),
        ("Разрешение", row.get("panel_resolution")),
        ("Активная область", row.get("panel_active_area")),
        ("Яркость", row.get("panel_brightness")),
        ("Контраст", row.get("panel_contrast")),
        ("Цвета", row.get("panel_display_colors")),
        ("Частота", row.get("panel_frequency")),
        ("Тип подсветки", row.get("panel_lamp_type")),
        ("Напряжение", row.get("panel_voltage")),
    ]

    has_panel = any(v for _, v in panel_specs)
    if has_panel:
        parts.append("<h3>Характеристики панели</h3>")
        parts.append('<table class="shop_attributes">')
        for label, val in panel_specs:
            if val:
                parts.append(f"<tr><th>{label}</th><td>{escape(val)}</td></tr>")
        parts.append("</table>")

    # Прочие компоненты
    other = row.get("other_parts")
    if other:
        try:
            other_dict = json.loads(other) if isinstance(other, str) else other
            if isinstance(other_dict, dict) and other_dict:
                parts.append("<h3>Прочие компоненты</h3>")
                parts.append('<table class="shop_attributes">')
                for k, v in other_dict.items():
                    if v:
                        parts.append(f"<tr><th>{escape(k)}</th><td>{escape(v)}</td></tr>")
                parts.append("</table>")
        except (json.JSONDecodeError, TypeError):
            pass

    return "\n".join(parts)


def build_meta_xml(row: dict) -> str:
    """Мета-поля для записи."""
    meta_fields = {
        "tv_brand": row.get("brand", ""),
        "tv_model_name": row.get("model_name", ""),
        "tv_chassis": row.get("chassis", ""),
        "tv_panel": row.get("panel", ""),
        "tv_backlight": row.get("backlight", ""),
        "tv_inverter": row.get("inverter", ""),
        "tv_tcon": row.get("tcon", ""),
        "tv_tuner": row.get("tuner", ""),
        "tv_mainboard": row.get("mainboard", ""),
        "tv_mainboard_ic": row.get("mainboard_ic", ""),
        "tv_psu": row.get("psu", ""),
        "tv_pwm_power": row.get("pwm_power", ""),
        "tv_panel_diagonal": row.get("panel_diagonal", ""),
        "tv_panel_resolution": row.get("panel_resolution", ""),
        "tv_year": row.get("year", ""),
        "tv_preview_image": row.get("preview_image", ""),
    }

    lines = []
    for key, val in meta_fields.items():
        if val:
            lines.append(f"""        <wp:postmeta>
            <wp:meta_key>{escape(key)}</wp:meta_key>
            <wp:meta_value>{escape_cdata(str(val))}</wp:meta_value>
        </wp:postmeta>""")
    return "\n".join(lines)


def generate_wxr():
    """Основная функция генерации WXR."""
    print(f"Загрузка данных из {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM tv_repairs ORDER BY brand, model_name").fetchall()
    conn.close()
    rows = [dict(r) for r in rows]
    print(f"  Загружено {len(rows)} моделей")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Собираем уникальные бренды для категорий
    brands = sorted(set(r["brand"] for r in rows))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pub_date = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")

    # Начало XML
    xml_parts = [f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"
    xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"
    xmlns:content="http://purl.org/rss/1.0/modules/content/"
    xmlns:wfw="http://wellformedweb.org/CommentAPI/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:wp="http://wordpress.org/export/1.2/"
>
<channel>
    <title>BN94.ru - База ТВ запчастей</title>
    <link>{SITE_URL}</link>
    <description>Справочник запчастей для ремонта телевизоров</description>
    <pubDate>{pub_date}</pubDate>
    <language>ru-RU</language>
    <wp:wxr_version>1.2</wp:wxr_version>
    <wp:base_site_url>{SITE_URL}</wp:base_site_url>
    <wp:base_blog_url>{SITE_URL}</wp:base_blog_url>

    <wp:author>
        <wp:author_login>{escape_cdata(AUTHOR_LOGIN)}</wp:author_login>
        <wp:author_display_name>{escape_cdata(AUTHOR_DISPLAY)}</wp:author_display_name>
    </wp:author>
"""]

    # Категории (бренды) как таксономия tv_brand
    for i, brand in enumerate(brands, 1):
        brand_slug = slugify(brand)
        xml_parts.append(f"""    <wp:term>
        <wp:term_id>{i}</wp:term_id>
        <wp:term_taxonomy>tv_brand</wp:term_taxonomy>
        <wp:term_slug>{brand_slug}</wp:term_slug>
        <wp:term_name>{escape_cdata(brand.upper())}</wp:term_name>
    </wp:term>
""")

    # Записи (модели)
    for idx, row in enumerate(rows, 1):
        brand = row["brand"]
        model_name = row["model_name"]
        title = row.get("full_title") or f"{brand.upper()} {model_name}"
        slug = make_post_slug(brand, model_name)
        content = build_content_html(row)
        meta_xml = build_meta_xml(row)
        brand_slug = slugify(brand)

        xml_parts.append(f"""    <item>
        <title>{escape(title)}</title>
        <link>{SITE_URL}/tv-database/{slug}/</link>
        <pubDate>{pub_date}</pubDate>
        <dc:creator>{escape_cdata(AUTHOR_LOGIN)}</dc:creator>
        <content:encoded>{escape_cdata(content)}</content:encoded>
        <excerpt:encoded>{escape_cdata(f"Запчасти и характеристики {title}")}</excerpt:encoded>
        <wp:post_id>{idx}</wp:post_id>
        <wp:post_date>{now}</wp:post_date>
        <wp:post_date_gmt>{now}</wp:post_date_gmt>
        <wp:post_name>{escape(slug)}</wp:post_name>
        <wp:status>publish</wp:status>
        <wp:post_type>tv_model</wp:post_type>
        <category domain="tv_brand" nicename="{brand_slug}">{escape_cdata(brand.upper())}</category>
{meta_xml}
    </item>
""")

        if idx % 500 == 0:
            print(f"  Обработано {idx}/{len(rows)}...")

    # Закрытие XML
    xml_parts.append("""</channel>
</rss>
""")

    # Запись файла
    full_xml = "".join(xml_parts)
    OUTPUT_FILE.write_text(full_xml, encoding="utf-8")
    size_mb = OUTPUT_FILE.stat().st_size / (1024 * 1024)
    print(f"\nГотово! Файл: {OUTPUT_FILE}")
    print(f"Размер: {size_mb:.1f} МБ")
    print(f"Записей: {len(rows)}")
    print(f"\n--- Инструкция по импорту ---")
    print(f"1. Установи плагин 'Custom Post Type UI' в WordPress")
    print(f"   Создай тип записи: tv_model (Модели ТВ)")
    print(f"   Создай таксономию: tv_brand (Бренды ТВ)")
    print(f"2. Загрузи превью-картинки в /wp-content/uploads/tv-previews/")
    print(f"3. Админка → Инструменты → Импорт → WordPress")
    print(f"   Загрузи файл: {OUTPUT_FILE.name}")
    print(f"4. URL страниц будет: {SITE_URL}/tv-database/brand-model/")


if __name__ == "__main__":
    generate_wxr()
