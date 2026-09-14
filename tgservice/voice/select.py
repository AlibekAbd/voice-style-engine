"""Отбор: что считать мусором, какой чат живой, что уходит в отложенную выборку.

Правила и ничего кроме правил: сети здесь нет, файлов почти нет, всё считается
из переданных сообщений. Поэтому проверяется тестами дословно.
"""

import re
from datetime import datetime, timedelta

from .common import RAW, read_json

# Живой чат: сообщения с обеих сторон и больше трёх за период. Отбор по виду
# имени контакта не годится — «Санжар Брат» и «q7» живые люди с плотной
# перепиской, а «имя плюс фамилия» их отсечёт.
MIN_MESSAGES = 4

LINK = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+", re.IGNORECASE)

# Пауза, после которой сообщения одной стороны считаются разными заходами, а не
# одной мыслью, разбитой на части.
SERIES_GAP = timedelta(minutes=10)

HOLDOUT_SIZE = 30
# Входящее короче этого в выборку не берём. Порог стоял на двенадцати знаках по
# соображению «по „ок" проверять нечего» — и вычёркивал из экзамена короткие
# перебросы, а типичная реплика владельца это три слова. Ответ на «ты где?» —
# такая же манера, как ответ на письмо, и проверять надо обе.
HOLDOUT_MIN_CHARS = 2
# Насколько поздний ответ ещё считается ответом. Половина ответов уходит в ту
# же минуту, но дольше десяти часов отвечено 675 раз, дольше суток — 376: не
# заметил мессенджер, был в разъезде. Это ответы, и манера у них своя. Неделя —
# граница, за которой начинается уже другой разговор, а не поздний ответ.
HOLDOUT_REPLY_HOURS = 24 * 7


def junk(row):
    """Почему сообщение не годится для разбора манеры. `None` — годится.

    Отсев узкий: служебные сообщения Телеграма, пересланное без собственного
    комментария, ссылка без текста. Короткие «ок», «принял», «гоу» остаются —
    это характерная манера владельца, и в разборе они нужны.
    """
    if row.get("service"):
        return "служебное"
    if row.get("fwd"):
        return "пересланное"
    text = (row.get("text") or "").strip()
    if not text:
        # Своё голосовое — это манера говорить, а не манера переписки, и в
        # разбор она не идёт: решение владельца. А входящее остаётся пометкой
        # без содержания: по нему видно, на что владелец отвечает. Ответ,
        # висящий в пустоте, читается как ответ ни на что.
        if row["out"]:
            return "своё без текста"
        return None if row.get("media") else "без текста"
    if not LINK.sub("", text).strip():
        return "ссылка без текста"
    return None


def clean(messages):
    """Сообщения, годные для разбора, и счёт отсеянного по причинам."""
    kept = []
    dropped = {}
    for row in messages:
        why = junk(row)
        if why:
            dropped[why] = dropped.get(why, 0) + 1
            continue
        kept.append(row)
    return kept, dropped


# --- шаг 3: отбор живых --------------------------------------------------


def alive(meta, messages):
    """Живой ли чат: обе стороны, больше трёх сообщений, собеседник — человек.

    Отбор по виду имени не годится: «Санжар Брат», «q7», «.» — живые люди с
    плотной перепиской, и требование «имя плюс фамилия» их отсечёт. А вот
    отсутствие имени вместе с отсутствием юзернейма — это не человек: так
    выглядит бот, аккаунт которого удалили. Флаг `bot` у него уже не стоит, и
    на выгрузке он проходит за личный диалог. Три таких нашлось, и владелец
    разговаривал с ними командами `/start`.
    """
    if not (meta.get("name") or "").strip() and not meta.get("username"):
        return "собеседник без имени и юзернейма — не человек"
    if len(messages) < MIN_MESSAGES:
        return "мало сообщений"
    mine = sum(1 for row in messages if row["out"])
    if mine == 0:
        return "владелец молчит"
    if mine == len(messages):
        return "собеседник молчит"
    return None


def live_chats():
    """Живые чаты с очищенными сообщениями. Порядок — по числу сообщений."""
    live = []
    skipped = []
    # Отсев считается только по живым чатам. Складывать его с отсевом в
    # мёртвых значит получить число, которое не сходится ни с чем: сообщения
    # мёртвого чата уже вычтены целиком вместе с чатом.
    junk_total = {}
    for path in sorted(RAW.glob("*.json")):
        record = read_json(path)
        meta = record["meta"]
        kept, dropped = clean(record["messages"])
        why = alive(meta, kept)
        if not why:
            for reason, count in dropped.items():
                junk_total[reason] = junk_total.get(reason, 0) + count
        row = {
            "id": meta["id"],
            "name": meta["name"],
            "username": meta.get("username"),
            "kind": meta["kind"],
            "raw": meta["msgs"],
            "msgs": len(kept),
            "mine": sum(1 for item in kept if item["out"]),
            "first": kept[0]["date"] if kept else None,
            "last": kept[-1]["date"] if kept else None,
        }
        row["theirs"] = row["msgs"] - row["mine"]
        if why:
            skipped.append({**row, "why": why})
            continue
        live.append({**row, "messages": kept})
    live.sort(key=lambda row: row["msgs"], reverse=True)
    return live, skipped, junk_total


# --- шаг 4: отложенная выборка -------------------------------------------


def _block(messages, start):
    """Всё, что одна сторона написала, пока другая молчала.

    Пауза внутри не рвёт: человек написал в девять, добавил в одиннадцать, а
    владелец открыл мессенджер в семь вечера и прочитал всё разом. Для него это
    один раздражитель, и отвечает он на него целиком.
    """
    out = messages[start]["out"]
    end = start
    while end + 1 < len(messages) and messages[end + 1]["out"] == out:
        end += 1
    return start, end


def _series(messages, start):
    """Один заход: сообщения одной стороны подряд и без долгой паузы.

    Здесь пауза рвёт. Владелец ответил «ок», а через три часа дописал
    «кстати» — это два разных захода, и манера у них разная. Годится для
    ответной части обмена; для входящей нужен `_block`, там всё наоборот.
    """
    out = messages[start]["out"]
    end = start
    while end + 1 < len(messages) and messages[end + 1]["out"] == out:
        if _moment(messages[end + 1]) - _moment(messages[end]) > SERIES_GAP:
            break
        end += 1
    return start, end


def _moment(row):
    return datetime.fromisoformat(row["date"])


def _exchanges(messages):
    """Все места, где на входящее пришёл ответ владельца.

    Обмен — это входящая серия целиком и ответная серия целиком. Серия
    восстанавливается по времени и по стороне: несколько сообщений подряд от
    одного лица — это одна мысль, разбитая на части.
    """
    found = []
    index = 0
    while index < len(messages):
        head, tail = _block(messages, index)
        index = tail + 1
        if messages[head]["out"] or index >= len(messages):
            continue
        reply_head, reply_tail = _series(messages, index)
        incoming = messages[head : tail + 1]
        reply = messages[reply_head : reply_tail + 1]
        chars = sum(len(row["text"]) for row in incoming)
        # Входящее из одного голосового весит ноль знаков, но раздражитель
        # это настоящий: ответ на голосовое — такая же манера, как ответ на
        # текст, и выкидывать его из экзамена нельзя.
        if chars < HOLDOUT_MIN_CHARS and not any(row.get("media") for row in incoming):
            continue
        gap = _moment(reply[0]) - _moment(incoming[-1])
        if gap > timedelta(hours=HOLDOUT_REPLY_HOURS):
            continue
        found.append(
            {
                "at": head,
                "context": messages[max(0, head - 6) : head],
                "incoming": incoming,
                "reply": reply,
                # Ответ через десять часов — сам по себе манера: у него бывают
                # извинения за молчание, которых у мгновенного ответа нет.
                "answered_in_minutes": int(gap.total_seconds() // 60),
            }
        )
    return found


def holdout(live, size=HOLDOUT_SIZE):
    """Отложенная выборка: около тридцати входящих с ответами владельца.

    Отделяется здесь, при сборе, — это последний момент, когда модель ещё
    ничего не видела. Разбор, увидевший те же диалоги, по которым потом
    проверяется, покажет отличный результат и ничего этим не докажет.

    Собеседники берутся из всех трёх слоёв по плотности переписки, чтобы в
    выборку попали и те, с кем владелец говорит каждый день, и те, с кем
    перекинулся десятком сообщений. По одному обмену с человека: тридцать
    обменов с одним собеседником проверяли бы один тон из многих.
    """
    usable = [row for row in live if _exchanges(row["messages"])]
    if not usable:
        return []
    step = max(1, len(usable) // 3)
    layers = [usable[:step], usable[step : step * 2], usable[step * 2 :]]

    # Корзины разнообразия. Экзамен, собранный из одних длинных деловых писем,
    # проверит манеру, которой владелец пользуется малую часть времени: обычная
    # его реплика — три слова. Поэтому вид чата и длина входящего разводятся по
    # корзинам, и выборка набирается из всех по очереди.
    def bucket(chat, choice):
        chars = sum(len(row["text"]) for row in choice["incoming"])
        size_of = "короткое" if chars < 25 else ("среднее" if chars < 120 else "длинное")
        return f"{'группа' if chat['kind'] == 'group' else 'личка'}, {size_of}"

    picked = []
    seen_buckets = {}
    while len(picked) < size and any(layers):
        for layer in layers:
            if not layer or len(picked) >= size:
                continue
            chat = layer.pop(0)
            options = _exchanges(chat["messages"])
            # Из своих обменов берём тот, что пополнит самую пустую корзину.
            options.sort(key=lambda one: (seen_buckets.get(bucket(chat, one), 0),
                                          one["at"]))
            choice = options[0]
            seen_buckets[bucket(chat, choice)] = seen_buckets.get(bucket(chat, choice), 0) + 1
            picked.append(
                {
                    "chat_id": chat["id"],
                    "chat": chat["name"],
                    "username": chat.get("username"),
                    "kind": chat["kind"],
                    "chat_msgs": chat["msgs"],
                    "context": choice["context"],
                    "incoming": choice["incoming"],
                    "reply": choice["reply"],
                    "answered_in_minutes": choice["answered_in_minutes"],
                    "mids": [row["mid"] for row in choice["incoming"] + choice["reply"]],
                }
            )
    picked.sort(key=lambda row: row["incoming"][0]["date"])
    return picked
