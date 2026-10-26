"""Пути, ключи и имена моделей ядра.

Пути и модель заказов — в `tgservice/discovery/settings.py`.
"""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
CODE_DIR = PACKAGE_DIR.parent
OUT = CODE_DIR / "out"
SESSION = CODE_DIR / "session" / "user"
# Расшифровки голосовых: по файлу на чат, имя — идентификатор чата. Лежат рядом
# с выгрузками, а не внутри них: выгрузку перезаписывает каждый прогон, а за
# расшифровку заплачено, и терять её из-за перевыгрузки нельзя.
SCRIBE = OUT / "scribe"
SCRIBE_USAGE = OUT / "scribe_usage.jsonl"
# Память отправки: боты-приманки, подтверждённые ответом, и журнал отправленных
# писем. Читает и пишет их только `outbox`.
BLACKLIST = OUT / "discovery" / "blacklist.json"
OUTREACH = OUT / "discovery" / "outreach.jsonl"

# Модель расшифровки. Scribe v2 сам определяет язык и держит смесь русского с
# казахским, поэтому подсказка языка в запросе не ставится.
MODEL_STT = "scribe_v2"


def env_path():
    """Ищет .env вверх по дереву от пакета, не выходя за корень репозитория."""
    for folder in [CODE_DIR, *CODE_DIR.parents]:
        candidate = folder / ".env"
        if candidate.exists():
            return candidate
        if (folder / ".git").exists():
            break
    raise FileNotFoundError(
        f"не найден .env: искал от {CODE_DIR} вверх до корня репозитория"
    )


def load_env(path=None):
    """Читает .env в словарь, отбрасывая комментарии, пробелы и кавычки."""
    source = Path(path) if path else env_path()
    values = {}
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def require(values, keys):
    """Возвращает значения ключей, ругаясь на недостающие."""
    missing = [key for key in keys if not values.get(key)]
    if missing:
        raise KeyError(f"в .env не заполнено: {', '.join(missing)}")
    return [values[key] for key in keys]
