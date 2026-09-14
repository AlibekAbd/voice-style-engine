"""Как батч выглядит и где он режется.

Вид и нарезка живут вместе не случайно: где резать, решается по весу
отрисованного текста, а не по числу сообщений. Разведи их — и вес придётся
считать дважды.
"""

import re

from .common import BATCHES, tokens, write_text

# Целевой размер батча. Мелкие предпочтительнее крупных: чем длиннее вход, тем
# сильнее провисает внимание к середине. Поэтому режем по порогу заметно ниже
# названных в спеке пятидесяти тысяч.
BATCH_TOKENS = 40_000

# Переписка плотнее этого режется по месяцам, а не по счётчику токенов: у
# месяца есть смысл на оси года, у «сорока тысяч» его нет. Потолок части при
# этом выше — месяц целиком дороже обычного батча, и это осознанный размен:
# крупных переписок единицы, а сорок кусков вместо дюжины стоят дороже и
# разбираются хуже.
BIG_TOKENS = 200_000
# Потолок части у плотной переписки. Стоит не там, где хотелось бы, а там, где
# позволяет **вывод** модели: пробный прогон показал, что описание батча выходит
# размером со сам батч — 18 886 токенов на входе дали 19 658 на выходе плюс
# четыре тысячи на размышление. При потолке вывода в 65 536 безопасный вход —
# около 58 тысяч.
#
# Из-за этого месяц целиком не влезает и режется по дням. Если вторая спека
# откажется от поштучной раскладки в пользу ссылок на номера сообщений, выход
# перестанет повторять вход и потолок можно поднимать обратно.
MONTH_TOKENS = 55_000

# Безопасный вход по замеру вывода: при поштучной раскладке ответ выходит
# размером со вход, а потолок вывода 65 536. Батч тяжелее — это обрезанный
# разбор, причину которого придётся искать руками. День тяжелее потолка части
# нарезка не делит (день целый по устройству), поэтому негабарит возможен и
# должен быть виден в замере.
SAFE_TOKENS = 58_000

# Длинный хвост. Переписка мельче этого отдельного агента не оправдывает и едет
# сборным батчем вместе с другими такими же.
TAIL_TOKENS = 8_000
TAIL_BATCH = 30_000
# Потолок людей в сборном батче: агент описывает каждого порознь, и тридцать
# портретов за раз он писать не станет.
TAIL_PEOPLE = 20

# Отступ у продолжения многострочного сообщения. Многострочных 3,4%, но в них
# 33 822 строки — это самые содержательные реплики, и каждая рисковала быть
# обрезанной до первой строки.
WRAP = "    "


def _mark(row):
    """Пометка о вложении. У голосового — с длительностью.

    Двухсекундное «ага» голосом и четырёхминутный монолог — разные
    раздражители, и по длительности это видно без единого расшифрованного
    слова.
    """
    kind = row.get("media")
    if not kind:
        return ""
    seconds = row.get("secs")
    if seconds is None:
        return f"[{kind}]"
    return f"[{kind} {seconds // 60}:{seconds % 60:02d}]"


def _line(row, chat_name, with_date=False):
    """Одно сообщение строкой: время до минуты, направление, текст дословно.

    Даты в строке нет, и это не потеря: она стоит заголовком дня над блоком.
    Штамп `2025-09-24 14:28 ` стоит 17 токенов против трёх у слов «Вы как» —
    Gemini режет цифры вдвое мельче букв. На 133 тысячах сообщений повтор даты
    в каждой строке съедал больше трети всей выгрузки.

    В отложенной выборке дата в строке нужна: там куски вырваны из ленты и
    заголовка дня над ними нет.
    """
    who = "я" if row["out"] else (row.get("from") or chat_name)
    body = f"{_mark(row)} {row['text']}".strip()
    # Сообщение в несколько абзацев: продолжение отбивается отступом. Без него
    # вторая строка неотличима от отдельного сообщения, и агент цитирует только
    # первую — «с утра созвонимся план сделаем» вместо «…с меня контент, с тебя
    # код». Отступ стоит почти ноль токенов: пробелы Gemini считает пачками.
    body = body.replace("\n", "\n" + WRAP)
    stamp = f"{row['date'][:10]} " if with_date else ""
    return f"{stamp}{row['date'][11:]} {who}: {body}"


def _quarter(date):
    """Квартал, в который попала дата: ось времени для сводящего прохода."""
    year, month = int(date[:4]), int(date[5:7])
    return f"{year}-Q{(month - 1) // 3 + 1}"


def _days(messages):
    """Сообщения, сгруппированные по дню. Граница батча проходит только по дню."""
    grouped = []
    for row in messages:
        day = row["date"][:10]
        if not grouped or grouped[-1][0] != day:
            grouped.append((day, []))
        grouped[-1][1].append(row)
    return grouped


def _head(chat, part, parts, messages):
    who = _who(chat)
    mine = sum(1 for row in messages if row["out"])
    lines = [
        f"# Переписка: {who}",
        "",
        f"Собеседник: {who}, id {chat['id']}"
        + (", группа" if chat["kind"] == "group" else ""),
        # «Часть 1 из 1» включала в разборе дисклеймер «судить об отношениях
        # целиком нельзя» — ложный для цельной переписки.
        *([f"Часть {part} из {parts}"] if parts > 1 else []),
        f"Период части: {messages[0]['date'][:10]} — {messages[-1]['date'][:10]}",
        f"Сообщений в части: {len(messages)} (мои {mine}, его {len(messages) - mine})",
        f"Всего сообщений с этим собеседником за год: {chat['msgs']}",
        "",
        "Ниже — сообщения подряд, дословно, сгруппированные по дням.",
        "Дата стоит заголовком дня, в строке сообщения — только время.",
        "Формат строки: время, кто, текст.",
        "Несколько сообщений подряд от одного лица в пределах минуты-двух — это одна",
        "мысль, разбитая на части.",
        "",
    ]
    return lines


def _render(chat, messages, part, parts):
    lines = _head(chat, part, parts, messages)
    lines += _body(chat, messages, "##")
    return "\n".join(lines)


def _body(chat, messages, level):
    """Сообщения по дням. Уровень заголовка задаётся снаружи: в сборном батче
    день лежит на ступень глубже, потому что над ним стоит собеседник."""
    lines = []
    for day, rows in _days(messages):
        lines += [f"{level} {day}", ""]
        lines += [_line(row, chat["name"]) for row in rows]
        lines.append("")
    return lines


def _who(chat):
    return chat["name"] + (f" (@{chat['username']})" if chat.get("username") else "")


def _mixed(chats):
    """Сборный батч: несколько коротких переписок в одном файле.

    Собеседник с десятком сообщений отдельного агента не оправдывает: описание
    блоков, написанное по шести репликам, — это описание ни на чём. Поэтому
    короткие переписки едут вместе.

    Но собраны они только по признаку длины, и разбирать их надо порознь.
    Отсюда предупреждение в шапке и раздел на каждого человека: сведение
    нескольких собеседников в одного испортило бы и портреты, и статистику.
    """
    total = sum(len(rows) for _, rows in chats)
    lines = [
        f"# Сборный батч: {len(chats)} коротких переписок",
        "",
        "**В этом файле несколько РАЗНЫХ собеседников.** Они лежат вместе только",
        "потому, что переписка с каждым короткая, — ничем другим они не связаны.",
        "",
        "Разбирать их надо порознь. Портрет собеседника, блоки общения и раскладка",
        "сообщений делаются **по каждому человеку отдельно**, своим разделом.",
        "Сводить их в одного собеседника, искать между ними общее или считать",
        "статистику на весь файл нельзя: это разные люди и разные отношения.",
        "",
        f"Собеседников в файле: {len(chats)}. Сообщений всего: {total}.",
        "",
        "Дата стоит заголовком дня, в строке сообщения — только время.",
        "Формат строки: время, кто, текст дословно. Владелец всегда «я»,",
        "собеседник — своим именем. Несколько строк подряд от одного лица в",
        "пределах минуты-двух — это одна мысль, разбитая на части.",
        "",
    ]
    for number, (chat, rows) in enumerate(chats, 1):
        mine = sum(1 for row in rows if row["out"])
        lines += [
            "---",
            "",
            f"## Собеседник {number} из {len(chats)}: {_who(chat)}",
            "",
            f"id {chat['id']}, сообщений {len(rows)} (мои {mine}, его {len(rows) - mine}),"
            f" период {rows[0]['date'][:10]} — {rows[-1]['date'][:10]}",
            "",
        ]
        lines += _body(chat, rows, "###")
    return "\n".join(lines)


def _months(messages):
    """Сообщения, сгруппированные по месяцу."""
    grouped = []
    for row in messages:
        month = row["date"][:7]
        if not grouped or grouped[-1][0] != month:
            grouped.append((month, []))
        grouped[-1][1].append(row)
    return grouped


def _split(chat, messages, limit=BATCH_TOKENS):
    """Нарезка переписки одного собеседника. Граница проходит по дню.

    Батч — это один собеседник целиком: агенту нужны отношения полностью.
    Делится только то, что в один батч не влезает, и делится по дням, чтобы
    разговор остался целым.

    У самых плотных переписок правило другое — `_split_big`. Годовая переписка
    на полтора миллиона токенов развалилась бы на четыре десятка кусков, и
    разрезы легли бы там, где кончился счётчик, а не там, где кончился период
    жизни.
    """
    if not messages:
        # Сейчас сюда не приходят: пустые чаты отсеиваются раньше. Но шапка
        # батча берёт первое и последнее сообщение, и на пустом списке нарезка
        # падала бы `IndexError` — отказ без объяснения вместо пустого ответа.
        return []
    if _weight(chat, messages) > BIG_TOKENS:
        return _split_big(chat, messages)
    return _pieces(chat, messages, _days(messages), limit)


def _weight(chat, messages):
    return tokens(_render(chat, messages, 1, 1))


def _pieces(chat, messages, grouped, limit):
    """Раскладка по частям: куски идут целиком, разрез между ними."""
    overhead = tokens("\n".join(_head(chat, 1, 1, messages)))
    parts = [[]]
    weight = overhead
    for _, rows in grouped:
        cost = tokens("\n".join(_line(row, chat["name"]) for row in rows))
        if parts[-1] and weight + cost > limit:
            parts.append([])
            weight = overhead
        parts[-1] += rows
        weight += cost
    return parts


def _split_big(chat, messages):
    """Нарезка самой плотной переписки: часть — это месяц.

    Месяц выбран потому, что у него есть смысл, а у «сорока тысяч токенов» его
    нет. Часть, совпадающая с месяцем, кладётся на ось года и сама говорит, к
    какому кварталу относится; произвольный разрез эту ось ломает.

    Месяц, не влезающий и в потолок части, режется по дням — но только он сам,
    соседние месяцы от этого не съезжают.
    """
    parts = []
    for _, rows in _months(messages):
        if _weight(chat, rows) <= MONTH_TOKENS:
            parts.append(rows)
            continue
        parts += _pieces(chat, rows, _days(rows), MONTH_TOKENS)
    return parts


def _slug(chat):
    name = chat.get("username") or chat["name"] or chat["id"]
    name = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ_-]+", "-", str(name)).strip("-").lower()
    return f"{name or 'chat'}-{chat['id'].lstrip('-')}"


def _pack(tail):
    """Короткие переписки, разложенные по сборным батчам.

    Потолков два: размер и число людей. Размер бережёт внимание агента,
    число людей — качество раскладки: агент обязан описать каждого порознь, и
    тридцать портретов в один заход он писать не станет.
    """
    packs = [[]]
    weight = 0
    for chat, messages, cost in sorted(tail, key=lambda row: -row[2]):
        full = weight + cost > TAIL_BATCH
        crowded = len(packs[-1]) >= TAIL_PEOPLE
        if packs[-1] and (full or crowded):
            packs.append([])
            weight = 0
        packs[-1].append((chat, messages))
        weight += cost
    return [pack for pack in packs if pack]


def _entry(name, text, rows, chats, part, parts, kind):
    return {
        "file": name,
        "kind": kind,
        "chat_id": chats[0]["id"] if kind == "solo" else None,
        "chat": chats[0]["name"] if kind == "solo" else None,
        "chats": chats,
        "part": part,
        "parts": parts,
        "msgs": len(rows),
        "chars": len(text),
        "tokens": tokens(text),
        "first": rows[0]["date"],
        "last": rows[-1]["date"],
        # Ось времени для сводящего прохода по крупному собеседнику: части
        # группируются по кварталам, а не по номерам.
        "quarter": _quarter(rows[0]["date"]),
    }


def batches(live, held):
    """Батчи. Отложенная выборка сюда не попадает.

    Батч — один собеседник целиком, и делится только тот, кто не влез. Из
    этого правила есть исключение снизу: с кем переписка на десяток сообщений,
    те едут вместе сборным батчем. Отдельный агент на шесть реплик написал бы
    описание блоков ни на чём.

    `held` — пары (id чата, mid), а не голые mid: номера сообщений глобальны
    только у личек, у каждой супергруппы своя нумерация с единицы. Плоское
    множество вычёркивало из чужих чатов их собственные сообщения молча.
    """
    for path in BATCHES.glob("*.md"):
        path.unlink()
    solo, tail = [], []
    for chat in live:
        messages = [row for row in chat["messages"] if (chat["id"], row["mid"]) not in held]
        if not messages:
            continue
        cost = tokens(_render(chat, messages, 1, 1))
        (tail if cost < TAIL_TOKENS else solo).append((chat, messages, cost))
    made = []
    for chat, messages, _ in solo:
        parts = _split(chat, messages)
        for number, rows in enumerate(parts, 1):
            text = _render(chat, rows, number, len(parts))
            name = f"{_slug(chat)}-{number:02d}.md"
            write_text(BATCHES / name, text)
            card = [{"id": chat["id"], "name": chat["name"], "msgs": len(rows)}]
            made.append(_entry(name, text, rows, card, number, len(parts), "solo"))
    packs = _pack(tail)
    for number, pack in enumerate(packs, 1):
        text = _mixed(pack)
        name = f"сборный-{number:02d}.md"
        write_text(BATCHES / name, text)
        rows = sorted(
            [row for _, messages in pack for row in messages], key=lambda row: row["date"]
        )
        card = [
            {"id": chat["id"], "name": chat["name"], "msgs": len(messages)}
            for chat, messages in pack
        ]
        made.append(_entry(name, text, rows, card, number, len(packs), "tail"))
    return made
