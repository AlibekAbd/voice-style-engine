"""Пути, ключи и имена моделей."""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
CODE_DIR = PACKAGE_DIR.parent
OUT = CODE_DIR / "out"
DUMP = OUT / "dump"
READ = OUT / "read"
SESSION = CODE_DIR / "session" / "user"
INDEX = OUT / "dump_index.json"
# Постоянный рабочий список чатов: решение владельца о том, что взято в работу.
# Живёт рядом с индексом, но переживает любую перевыгрузку — чат в нём опознаётся
# идентификатором, а не местом в списке.
ROSTER = OUT / "roster.json"
# Реестр чатов: что уже смотрели и чем это кончилось. Переживает прогон, поэтому
# лежит среди постоянных данных, а не в папке разведки.
REGISTRY = OUT / "registry.json"
META = OUT / "chat_meta.json"
STATE = OUT / "state.json"
USAGE = OUT / "usage.jsonl"
PRICES = OUT / "prices.json"

MODEL_PRO = "gemini-3.1-pro-preview"


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
