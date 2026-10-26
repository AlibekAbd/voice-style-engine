"""Чтение Телеграма для разбора манеры речи: переписка целиком и длительности голосовых.

Подключение, сессия, диалоги и отказы — общие, из `tgservice.telegram`.
Здесь только то, что нужно одному voice: строка сообщения со всеми
признаками манеры и досылка длительностей по номерам.
"""

import asyncio

from telethon.errors import ChannelPrivateError, FloodWaitError, UnauthorizedError

from .. import telegram

# Через сколько прочитанных сообщений обход подаёт голос.
SPEECH_MARK = 5000

# Сколько сообщений Телеграм отдаёт за один запрос по номерам.
IDS_PER_CALL = 100


def _speech_row(message, in_group):
    """Одно сообщение так, как его читает разбор манеры речи.

    Дословный текст обязателен: на нём держится и банк фраз, и проверка на
    выдумки. Поэтому `strip` тут нет — отбивка пустой строкой внутри сообщения
    это тоже манера.
    """
    date = getattr(message, "date", None)
    row = {
        "mid": message.id,
        "date": date.isoformat()[:16] if date else None,
        "out": bool(getattr(message, "out", False)),
        "text": getattr(message, "text", None) or getattr(message, "message", None) or "",
    }
    if getattr(message, "action", None) is not None:
        row["service"] = True
    if getattr(message, "fwd_from", None) is not None:
        row["fwd"] = True
    media = telegram._media_kind(message)
    if media:
        row["media"] = media
        seconds = telegram._seconds(message)
        if seconds is not None:
            row["secs"] = seconds
    reply_to = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
    if reply_to:
        row["reply_to"] = reply_to
    if in_group:
        name, username = telegram._author(message)
        row["from"] = name
        row["from_username"] = username
        sender_id = getattr(message, "sender_id", None)
        if sender_id is not None:
            row["from_id"] = str(sender_id)
    return row


async def speech(app, entity, since=None, in_group=False, notify=telegram._silent,
                 sleep=asyncio.sleep):
    """Сообщения чата за окно в виде, годном для разбора манеры речи.

    От `discovery.tg.fetch` отличается тем, что сохраняется: направление, пересылка,
    служебность, вид вложения. Отсев мусора здесь не делается — что считать
    мусором, решает разбор, а выгрузка обязана быть полной.

    Флуд-лимит не отбрасывает уже прочитанное: обход продолжается с того
    сообщения, на котором встал. Годовой обход по чату на десять тысяч
    сообщений иначе начинался бы заново с каждой паузы.
    """
    rows = []
    offset = 0
    waits = 0
    while True:
        try:
            async for message in app.iter_messages(entity, limit=None, offset_id=offset):
                offset = message.id
                date = getattr(message, "date", None)
                if since and date and date < since:
                    return rows
                rows.append(_speech_row(message, in_group))
                # Переписка на десятки тысяч сообщений читается минутами. Без
                # отметок обход неотличим от зависшего, и его убивают руками.
                if len(rows) % SPEECH_MARK == 0:
                    notify(f"прочитано {len(rows)}, дошли до {str(date)[:10]}")
            return rows
        except FloodWaitError as error:
            if error.seconds > telegram.FLOOD_LIMIT or waits >= telegram.FLOOD_RETRIES:
                raise telegram.FloodStop(
                    f"Телеграм просит подождать {error.seconds} с — чат отложен"
                ) from error
            waits += 1
            notify(f"флуд-лимит: ждём {error.seconds} с и читаем чат дальше")
            await sleep(error.seconds + 2)
        except ChannelPrivateError as error:
            raise telegram.ChatClosed(
                "Телеграм не отдаёт сообщения: чат закрыт или нас из него убрали"
            ) from error
        except UnauthorizedError as error:
            raise telegram.SessionGone(telegram.LOGIN_HINT) from error


async def lengths(app, entity, ids, notify=telegram._silent, sleep=asyncio.sleep):
    """Длительность голосовых по номерам сообщений: пары «номер, секунды».

    Спрашивается отдельно от выгрузки и по номерам, а не обходом: длительность
    понадобилась после того, как год уже прочитан, и читать его заново ради
    одного поля значило бы сорок минут работы вместо минуты. Телеграм отдаёт до
    сотни сообщений за запрос.
    """
    found = {}
    for start in range(0, len(ids), IDS_PER_CALL):
        chunk = ids[start : start + IDS_PER_CALL]
        while True:
            try:
                messages = await app.get_messages(entity, ids=chunk)
                break
            except FloodWaitError as error:
                if error.seconds > telegram.FLOOD_LIMIT:
                    raise telegram.FloodStop(
                        f"Телеграм просит подождать {error.seconds} с — чат отложен"
                    ) from error
                notify(f"флуд-лимит: ждём {error.seconds} с")
                await sleep(error.seconds + 2)
            except UnauthorizedError as error:
                raise telegram.SessionGone(telegram.LOGIN_HINT) from error
        for message in messages:
            # Удалённое сообщение приходит пустым местом: номер есть, объекта нет.
            if message is None:
                continue
            document = getattr(message, "document", None)
            for attribute in getattr(document, "attributes", ()) or ():
                seconds = getattr(attribute, "duration", None)
                if seconds is not None:
                    found[message.id] = int(seconds)
                    break
    return found
