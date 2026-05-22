# tv-scraper-telspb

Async-парсер базы ремонта телевизоров с сайта tel-spb.ru.
Собирает модели по всем брендам, сохраняет в SQLite, опционально в CSV/JSONL,
скачивает превью, генерирует HTML/WXR для импорта в WordPress.

## Стек

- Python 3.10+, asyncio, aiohttp, BeautifulSoup
- SQLite (через стандартный `sqlite3`)
- pytest + Hypothesis для property-based тестов

## Структура

```
config.py        константы (URL, тайминги, лимиты, маппинг полей)
main.py          CLI и оркестрация
scraper.py       обход брендов и парсинг страниц моделей
discovery.py     обход sitemap.xml и BFS под-страниц бренда
coverage.py      per-brand метрики ожидалось/собрано
storage.py       SQLite, JSONL, CSV, resume-стейт
images.py        скачивание и сохранение превью
utils.py         общие хелперы (нормализация, retry, логирование)
models.py        датаклассы TVRepairData / ModelRef
generate_site.py статический сайт из БД
generate_wxr.py  WordPress WXR XML
tests/           pytest + Hypothesis
.kiro/specs/     спеки фич (kiro)
```

## Установка

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Использование

```powershell
# Полный парсинг (только SQLite)
python main.py

# Один бренд
python main.py --brand samsung

# С CSV и JSONL
python main.py --with-csv --with-jsonl

# Резюм с того места, где остановились
python main.py --resume

# Без обхода sitemap (для отладки BFS)
python main.py --no-sitemap

# Экспорт CSV из текущей SQLite
python main.py --export-csv
```

## Тесты

```powershell
pytest
```

## Данные

Папка `data/` хранит runtime-артефакты (SQLite, превью, raw HTML, экспорты)
и в репозитории не коммитится — структура поддерживается `.gitkeep`-файлами.
