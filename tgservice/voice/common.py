"""Общее для всех шагов сбора: пути, чтение-запись, окно, оценка длины."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config

VOICE = config.OUT / "voice"
RAW = VOICE / "raw"
BATCHES = VOICE / "batches"
FETCH_STATE = VOICE / "fetch_state.json"
RAW_INDEX = VOICE / "raw_index.json"
LIVE_INDEX = VOICE / "live_index.json"
HOLDOUT = VOICE / "holdout.json"
HOLDOUT_TEXT = VOICE / "holdout.md"
BATCH_INDEX = VOICE / "batches_index.json"
MEASURE = VOICE / "measure.json"
MEASURE_TEXT = VOICE / "measure.md"
SMALL_GROUPS = VOICE / "small_groups.json"
# Какие группы взяты в сбор. Решение владельца, принимается по списку из
# `small_groups.json` и хранится идентификаторами.
GROUPS_TAKEN = VOICE / "groups_taken.json"
# Решения владельца: переименования классов, правки к регистрам, выделенные
# собеседники. Единственный файл, который правится руками и переживает
# пересборку; всё остальное в out/voice порождается.
DECISIONS = VOICE / "решения-по-классам.json"
# Карточки собеседников: портрет, блоки, счёт сообщений по выгрузке. По ним
# считаются доли для порога выделения.
CARDS = VOICE / "карточки.json"

DAYS = 365

# Оценка длины в токенах по знакам. Числа измерены `countTokens` на образцах
# каждого рода, а не взяты на глаз: первая версия была придумана и промахнулась
# в полтора раза вниз.
#
# Главное, что показал замер: **цифра стоит целый токен**. Штамп даты и времени
# `2025-09-24 14:28 ` — это семнадцать токенов против трёх у слов «Вы как».
# Буквы, наоборот, дёшевы, а пробелы почти даром.
#
# Корзина «прочее» — знаки препинания и эмодзи, единственная подогнанная:
# остальные измерены прямо.
CYRILLIC_PER_TOKEN = 4.22
LATIN_PER_TOKEN = 5.6
DIGIT_PER_TOKEN = 1.0
SPACE_PER_TOKEN = 28.6
OTHER_PER_TOKEN = 0.5
# Оценка по родам знаков всё равно занижает — медианой на 6,5% против
# настоящего счёта. Занижение опаснее завышения: потолок части перестаёт быть
# потолком. Поправка измерена, а не назначена.
SLACK = 1.07


def _now():
    return datetime.now(timezone.utc)


def since_of(days=DAYS):
    """Начало окна — полночь того дня, что был `days` назад.

    Привязка к полуночи делает прогон повторяемым: два запуска в один день
    берут одно и то же окно, и выгрузка не разъезжается сама по себе.
    """
    day = (_now() - timedelta(days=days)).date()
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def read_json(path, default=None):
    if not Path(path).exists():
        return default
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def write_text(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(value, encoding="utf-8")


def say(text):
    print(text, flush=True)


def tokens(text):
    """Оценка длины в токенах по родам знаков. Проверена против `countTokens`."""
    cyrillic = latin = digits = spaces = other = 0
    for char in text:
        if "Ѐ" <= char <= "ӿ":
            cyrillic += 1
        elif char.isascii() and char.isalpha():
            latin += 1
        elif char.isdigit():
            digits += 1
        elif char.isspace():
            spaces += 1
        else:
            other += 1
    guess = (
        cyrillic / CYRILLIC_PER_TOKEN
        + latin / LATIN_PER_TOKEN
        + digits / DIGIT_PER_TOKEN
        + spaces / SPACE_PER_TOKEN
        + other / OTHER_PER_TOKEN
    )
    return int(guess * SLACK) + 1
