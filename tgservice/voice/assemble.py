"""Сборка и замер: то, что получается на выходе шага.

Ни сети, ни решений — только порядок вызовов и счёт. Оба шага переписывают
свои файлы целиком, поэтому гонять их можно сколько угодно.
"""

import statistics

from .batch import SAFE_TOKENS, _line, batches
from .common import (
    BATCH_INDEX,
    FETCH_STATE,
    HOLDOUT,
    HOLDOUT_TEXT,
    LIVE_INDEX,
    MEASURE,
    MEASURE_TEXT,
    RAW,
    read_json,
    say,
    write_json,
    write_text,
    _now,
)
from .select import holdout, live_chats


def holdout_text(items):
    """Отложенная выборка человеку в руки. Проверять по ней будет третья спека."""
    lines = [
        "# Отложенная выборка",
        "",
        f"Обменов: {len(items)}. Собеседников: {len({row['chat_id'] for row in items})}.",
        "",
        "Эти сообщения в батчи не попали. Агент, снимающий манеру, их не видел.",
        "",
    ]
    for number, item in enumerate(items, 1):
        who = item["chat"] + (f" (@{item['username']})" if item.get("username") else "")
        lines += [f"## {number}. {who}", ""]
        if item["context"]:
            lines.append("Перед этим:")
            lines += [f"- {_line(row, item['chat'], with_date=True)}" for row in item["context"]]
            lines.append("")
        lines.append("Входящее:")
        lines += [f"- {_line(row, item['chat'], with_date=True)}" for row in item["incoming"]]
        lines.append("")
        waited = item["answered_in_minutes"]
        when = "сразу" if waited < 2 else f"через {waited} мин"
        if waited >= 120:
            when = f"через {waited // 60} ч"
        lines.append(f"Ответ владельца ({when}):")
        lines += [f"- {_line(row, item['chat'], with_date=True)}" for row in item["reply"]]
        lines.append("")
    return "\n".join(lines)


def build():
    """Отбор живых, отсев мусора, батчи. Всё, кроме отложенной выборки,
    считается заново.

    Выборка отделяется один раз и дальше не тасуется: пересборка после
    довыгрузки выбрала бы другие обмены, а старые уже видели агенты в прежних
    батчах — заголовок «модель их не видела» стал бы ложью, и проверка по
    третьей спеке перестала бы что-либо доказывать.
    """
    if not any(RAW.glob("*.json")):
        raise LookupError("выгрузки нет: сначала python -m tgservice.voice fetch")
    live, skipped, junk_total = live_chats()
    say(f"живых чатов: {len(live)}, отсеяно чатов: {len(skipped)}")
    prior = read_json(HOLDOUT)
    if prior and prior.get("items"):
        held = prior["items"]
        say(f"отложенная выборка уже отделена ({len(held)} обменов) — не пересобирается")
    else:
        held = holdout(live)
        write_json(
            HOLDOUT,
            {
                "made": _now().isoformat()[:16],
                "count": len(held),
                "items": held,
            },
        )
        write_text(HOLDOUT_TEXT, holdout_text(held))
        say(f"отложенная выборка: {len(held)} обменов у {len({row['chat_id'] for row in held})} собеседников")
    # Пары (чат, mid): номера сообщений глобальны только у личек.
    held_keys = {(item["chat_id"], mid) for item in held for mid in item["mids"]}
    write_json(
        LIVE_INDEX,
        {
            "live": [{k: v for k, v in row.items() if k != "messages"} for row in live],
            "skipped": skipped,
            "junk": junk_total,
        },
    )
    made = batches(live, held_keys)
    write_json(BATCH_INDEX, {"made": _now().isoformat()[:16], "batches": made})
    say(f"батчей: {len(made)}")
    return live, skipped, junk_total, held, made


def measure():
    """Замер: цифрами определяется, сколько агентов запускать и сколько это займёт."""
    live_index = read_json(LIVE_INDEX)
    batch_index = read_json(BATCH_INDEX)
    held = read_json(HOLDOUT)
    if not (live_index and batch_index and held):
        raise LookupError("нечего мерить: сначала python -m tgservice.voice build")
    live = live_index["live"]
    made = batch_index["batches"]
    sizes = [row["tokens"] for row in made] or [0]
    # Частей у собеседника — это сколько батчей его содержат. У сборного батча
    # собеседников много, и каждому он засчитывается как одна часть.
    parts = {}
    for row in made:
        for card in row["chats"]:
            parts[card["id"]] = parts.get(card["id"], 0) + 1
    big = sorted(
        [row for row in live if parts.get(row["id"], 0) > 1],
        key=lambda row: row["msgs"],
        reverse=True,
    )
    state = read_json(FETCH_STATE, {})
    skipped = live_index["skipped"]
    # Негабарит — это обрезанный вывод модели на разборе: день тяжелее потолка
    # нарезка не делит, и такую часть надо видеть до того, как за неё заплатят.
    oversized = [row["name"] for row in made if row["tokens"] > SAFE_TOKENS]
    numbers = {
        "made": _now().isoformat()[:16],
        "window_since": state.get("since"),
        "chats_fetched": len(state.get("chats", {})),
        "live_chats": len(live),
        "skipped_chats": len(skipped),
        "messages_raw": sum(row["raw"] for row in live + skipped),
        "messages_in_dead_chats": sum(row["raw"] for row in skipped),
        "messages_clean": sum(row["msgs"] for row in live),
        "messages_mine": sum(row["mine"] for row in live),
        "junk": live_index["junk"],
        "batches": len(made),
        "tokens_total": sum(sizes),
        "tokens_median": int(statistics.median(sizes)),
        "tokens_max": max(sizes),
        "tokens_min": min(sizes),
        "big_threshold": "больше одного батча",
        "big_chats": len(big),
        "batches_solo": sum(1 for row in made if row["kind"] == "solo"),
        "batches_tail": sum(1 for row in made if row["kind"] == "tail"),
        "tail_chats": len({card["id"] for row in made if row["kind"] == "tail" for card in row["chats"]}),
        "holdout": held["count"],
        "holdout_chats": len({row["chat_id"] for row in held["items"]}),
        "oversized": oversized,
        "per_chat": [
            {
                "chat": row["name"],
                "id": row["id"],
                "msgs": row["msgs"],
                "mine": row["mine"],
                "theirs": row["theirs"],
                "parts": parts.get(row["id"], 0),
                "first": row["first"],
                "last": row["last"],
            }
            for row in live
        ],
    }
    write_json(MEASURE, numbers)
    write_text(MEASURE_TEXT, measure_text(numbers, big))
    say(MEASURE_TEXT.read_text(encoding="utf-8"))
    return numbers


def measure_text(numbers, big):
    lines = [
        "# Замер собранного",
        "",
        f"Окно: с {numbers['window_since']} по сегодня. Чатов в обходе: {numbers['chats_fetched']}.",
        "",
        "## Сколько чего",
        "",
        "| Что | Сколько |",
        "|---|---|",
        f"| Живых собеседников | {numbers['live_chats']} |",
        f"| Чатов отсеяно | {numbers['skipped_chats']} |",
        f"| Сообщений выгружено | {numbers['messages_raw']} |",
        f"| Из них в неживых чатах | {numbers['messages_in_dead_chats']} |",
        f"| Сообщений в работе, после отсева | {numbers['messages_clean']} |",
        f"| Из них моих | {numbers['messages_mine']} |",
        f"| Батчей | {numbers['batches']} |",
        f"| Из них по одному собеседнику | {numbers['batches_solo']} |",
        f"| Сборных, из коротких переписок | {numbers['batches_tail']} |",
        f"| Собеседников в сборных | {numbers['tail_chats']} |",
        f"| Токенов всего | {numbers['tokens_total']} |",
        f"| Размер батча: медиана | {numbers['tokens_median']} |",
        f"| Размер батча: максимум | {numbers['tokens_max']} |",
        f"| Размер батча: минимум | {numbers['tokens_min']} |",
        f"| Крупных собеседников | {numbers['big_chats']} |",
        f"| Отложенная выборка | {numbers['holdout']} обменов у {numbers['holdout_chats']} собеседников |",
        "",
        "Токены — оценка по знакам, не счёт токенизатором: к модели на этом шаге",
        "не обращались.",
    ]
    if numbers.get("oversized"):
        lines += [
            "",
            "## Негабаритные батчи",
            "",
            f"Вход больше безопасных {SAFE_TOKENS} токенов: вывод модели на разборе",
            "может оборваться на потолке. Причина обычно — день тяжелее потолка части,",
            "его нарезка не делит.",
            "",
        ]
        lines += [f"- {name}" for name in numbers["oversized"]]
    lines += [
        "",
        "## Что отсеяно",
        "",
        "| Причина | Сообщений |",
        "|---|---|",
    ]
    for why, count in sorted(numbers["junk"].items(), key=lambda row: -row[1]):
        lines.append(f"| {why} | {count} |")
    lines += [
        "",
        "## Крупные собеседники",
        "",
        "Крупный — тот, чья переписка в один батч не влезла. Такому агент увидит",
        "куски отношений вместо отношений целиком, и по нему нужен сводящий проход.",
        "",
        "| Собеседник | Сообщений | Частей |",
        "|---|---|---|",
    ]
    for row in big:
        lines.append(f"| {row['name']} | {row['msgs']} | {_parts_of(numbers, row['id'])} |")
    lines += [
        "",
        "## Сообщений по каждому собеседнику",
        "",
        "| Собеседник | Всего | Мои | Его | Частей | Первое | Последнее |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in numbers["per_chat"]:
        lines.append(
            f"| {row['chat']} | {row['msgs']} | {row['mine']} | {row['theirs']} |"
            f" {row['parts']} | {row['first']} | {row['last']} |"
        )
    return "\n".join(lines) + "\n"


def _parts_of(numbers, chat_id):
    for row in numbers["per_chat"]:
        if row["id"] == chat_id:
            return row["parts"]
    return 0
