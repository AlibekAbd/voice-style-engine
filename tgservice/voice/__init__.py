"""Снятие манеры переписки владельца: от выгрузки до файла стиля и скилла.

Весь конвейер трёх спек. Сбор денег не стоит; шаги из `analyse` обращаются к
Gemini и стоят денег — они помечены в VOICE.md.

    python -m tgservice.voice fetch      # выгрузка, продолжается после обрыва
    python -m tgservice.voice build      # отбор, отсев, выборка, батчи
    python -m tgservice.voice measure    # замер
    python -m tgservice.voice groups     # обзор малолюдных групп
    python -m tgservice.voice lengths    # починка: дослать длительности

Разложено по тому, что от чего зависит:

    common    пути, чтение-запись, окно, оценка длины
    collect   шаги, которые ходят в Телеграм
    select    правила отбора: мусор, живые чаты, отложенная выборка
    batch     как батч выглядит и где он режется
    assemble  сборка и замер
    analyse   обращения к Gemini: разбор, классификация, своды — платно
    verify    сверка ответов агентов с выгрузкой
    classify  карточки, классы, регистры, порции, веса
    compose   сборка файла стиля, решения владельца, обезличивание
    traits    измеримые признаки манеры по корпусу
    trial     замер файла по отложенной выборке
    skillpack раскладка готового файла по скиллу

`collect` требует сети, `select` — чистые правила, и это разделение не
косметическое: правила проверяются тестами без единого запроса.
"""

from .batch import (
    BATCH_TOKENS,
    BIG_TOKENS,
    MONTH_TOKENS,
    TAIL_BATCH,
    TAIL_PEOPLE,
    TAIL_TOKENS,
    batches,
)
from .assemble import build, holdout_text, measure, measure_text
from .collect import GROUP_CAP, VOICE_KINDS, fetch, groups, lengths, taken_groups
from .common import (
    BATCHES,
    BATCH_INDEX,
    DAYS,
    FETCH_STATE,
    HOLDOUT,
    HOLDOUT_TEXT,
    LIVE_INDEX,
    MEASURE,
    RAW,
    VOICE,
    read_json,
    say,
    since_of,
    tokens,
    write_json,
    write_text,
)
from .select import (
    HOLDOUT_MIN_CHARS,
    HOLDOUT_REPLY_HOURS,
    MIN_MESSAGES,
    SERIES_GAP,
    alive,
    clean,
    holdout,
    junk,
    live_chats,
)
