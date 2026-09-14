"""Всё общение с Телеграмом. Про модели и разбор этот модуль ничего не знает.

Наружу отдаются готовые строки выгрузки, списки чатов и понятные отказы:
`SessionGone`, `FloodStop`, `SpamRestricted`, `ChatCapReached`. Трассировки
Telethon до команд не доходят.
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChannelsTooMuchError,
    FloodWaitError,
    InviteRequestSentError,
    PeerFloodError,
    UnauthorizedError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import (
    GetFullChannelRequest,
    JoinChannelRequest,
    LeaveChannelRequest,
)
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.messages import (
    GetDialogFiltersRequest,
    GetForumTopicsRequest,
    GetFullChatRequest,
    ImportChatInviteRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.types import (
    Channel,
    ChannelForbidden,
    Chat,
    ChatForbidden,
    MessageActionChannelMigrateFrom,
    MessageActionChatMigrateTo,
    PeerChannel,
    PeerChat,
    User,
)
from telethon.utils import get_peer_id

from . import config

JOIN_PAUSE = 90
JOIN_LIMIT_PER_RUN = 5
CHAT_CAP = 500
CAP_WARN = 25
FLOOD_LIMIT = 300
FLOOD_RETRIES = 5
TOPIC_LIMIT = 100

# Через сколько прочитанных сообщений обход подаёт голос.
SPEECH_MARK = 5000

# Сколько сообщений Телеграм отдаёт за один запрос по номерам.
IDS_PER_CALL = 100

# Сколько ждём, пока другая задача отпустит файл сессии.
SESSION_WAIT = 30
SESSION_WAIT_LIMIT = 3 * 60 * 60

# Служебные аккаунты Телеграма. Личным диалогом выглядят, собеседника не имеют.
SERVICE_ACCOUNTS = frozenset({777000})

LOGIN_HINT = "сессия Телеграма недействительна, войдите заново: python -m tgservice login"

# Подпись сессии в списке устройств Телеграма: без неё владелец не поймёт, чей
# это сеанс, когда будет разбирать активные.
DEVICE = "Desktop"
SYSTEM = "Windows 11"

# Автоматический набор папки: эти чаты попадают в неё сами, вручную их не добавляли.
AUTO_SETS = ("contacts", "non_contacts", "groups", "broadcasts", "bots")

# Где по чату виден отклик на заказ. Признак чата, а не сообщения: он один на всю
# выгрузку и решается устройством чата.
REPLIES_THREAD = "тред"  # обычная группа: отвечают прямо в тред
REPLIES_COMMENTS = "комментарии"  # канал с группой обсуждения: отклики в комментариях
REPLIES_NONE = "нет"  # канал без обсуждения: откликнуться в Телеграме негде

# Можно ли в чате писать. Спрашивается до вступления: сходить в чат, где пишут
# только админы, значит потратить время на то, чем нельзя воспользоваться.
WRITE_OPEN = "можно"
WRITE_JOIN = "после вступления"
WRITE_ADMINS = "только админы"
WRITE_BANNED = "участникам запрещено"

# Сколько знаков закреплённого сообщения показываем: правила про рекламу занимают
# абзац, а полотно на три экрана в справке о чате читать никто не станет.
PINNED_LIMIT = 700


class SessionGone(RuntimeError):
    """Сессия отозвана или её нет: нужен повторный вход."""


class FloodStop(RuntimeError):
    """Телеграм просит ждать слишком долго: чат откладываем."""


class KnownBot(RuntimeError):
    """Адресат — бот-приманка из чёрного списка. Письмо ему не уходит."""


class SpamRestricted(RuntimeError):
    """Аккаунт ограничен за спам: вступления прекращаются, повторы запрещены."""


class ChatCapReached(RuntimeError):
    """Аккаунт упёрся в потолок групп и каналов."""


class UsernameGone(RuntimeError):
    """Такого юзернейма в Телеграме нет: он свободен или написан с ошибкой."""


class NotAChat(RuntimeError):
    """Юзернейм занят, но за ним человек или бот, а не группа и не канал."""


class ChatClosed(RuntimeError):
    """Чат есть, но без вступления Телеграм его не отдаёт."""


class Rows(list):
    """Строки выгрузки. Признаки миграции живут атрибутами рядом со списком."""

    migrated_to = None
    migrated_from = None


def _silent(text):
    """Молчаливый рассказчик по умолчанию."""


def client():
    values = config.load_env()
    api_id, api_hash = config.require(values, ["TG_API_ID", "TG_API_HASH"])
    return TelegramClient(
        str(config.SESSION), int(api_id), api_hash,
        device_model=DEVICE, system_version=SYSTEM,
    )


async def start(app):
    """Подключение с проверкой сессии."""
    await app.connect()
    try:
        authorized = await app.is_user_authorized()
    except UnauthorizedError as error:
        raise SessionGone(LOGIN_HINT) from error
    if not authorized:
        raise SessionGone(LOGIN_HINT)
    return app


async def queued(notify=_silent, sleep=asyncio.sleep):
    """Клиент, дождавшийся своей очереди к файлу сессии.

    Сессия одна на все задачи, а её файл — SQLite, и на запись он пускает
    одного. Рядом может идти обход другого агента, и тогда подключение падает
    замком. Своё дело в этом случае откладывается: чужой процесс убивать
    нельзя, за ним чья-то незаконченная работа.
    """
    waited = 0
    while True:
        app = None
        try:
            # Замок ловится и в конструкторе клиента: Telethon читает SQLite
            # уже там. Вне try такая попытка падала трассировкой вместо
            # очереди.
            app = client()
            return await start(app)
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) or waited >= SESSION_WAIT_LIMIT:
                raise
            # Замок ловится после того, как соединение поднято. Неприбранная
            # попытка оставляет за собой открытый сокет, а попыток за три часа
            # набирается три сотни.
            if app is not None:
                try:
                    await app.disconnect()
                except Exception:
                    pass
            if not waited:
                notify("сессию держит другая задача — встаём в очередь")
            await sleep(SESSION_WAIT)
            waited += SESSION_WAIT
            if waited % (SESSION_WAIT * 10) == 0:
                notify(f"ждём сессию: {waited // 60} мин")


async def dialogs(app):
    """Диалоги основного списка и архива: пары «диалог, лежит ли он в архиве».

    Обход идёт двумя проходами: `iter_dialogs` архива не показывает, и без
    второго прохода чаты, которые владелец сам убрал с глаз, не осматривал бы
    никто. Показанные идентификаторы запоминаются, поэтому чат, попавший в обе
    выдачи, отдаётся один раз — и с тем признаком, с которым встретился первым.
    """
    seen = set()
    try:
        for archived in (False, True):
            async for dialog in app.iter_dialogs(archived=archived):
                if dialog.id in seen:
                    continue
                seen.add(dialog.id)
                # `Dialog.archived` — это `folder_id is not None`, ему и верим.
                yield dialog, bool(getattr(dialog, "archived", archived))
    except UnauthorizedError as error:
        raise SessionGone(LOGIN_HINT) from error


async def entity_by_id(app, chat_id):
    """Сущность чата по идентификатору, без всяких диалогов.

    Чат, от которого владелец отписался, в диалогах не появляется, а читаться
    может: сессия помнит его сущность, а публичный канал Телеграм отдаёт и без
    подписки. Отказ Телеграма уходит наверх как есть — его назовут в итоге.
    """
    try:
        return await app.get_entity(int(chat_id))
    except UnauthorizedError as error:
        raise SessionGone(LOGIN_HINT) from error


# Отказы по юзернейму. Три разные беды, и лечатся они по-разному: опечатку
# исправляют, в закрытый чат вступают, а человеку пишут в личку.
NO_USERNAME = "юзернейма @{name} в Телеграме нет: проверь написание"
NOT_A_CHAT = "@{name} — это {who}, а не группа и не канал: разбирать нечего"
CLOSED_CHAT = (
    "@{name} — закрытый чат: без вступления Телеграм его не отдаёт "
    "(вступить: join --username {name})"
)
INVITE_LINK = (
    "{target} — ссылка-приглашение, а не юзернейм: по ней вступают — "
    "join --invite {target}"
)
NO_NAME = "не сказано, какой чат открывать: --username sample_chat"

# Хвосты ссылок Телеграма: владелец приносит адрес из браузера, а не голое имя.
LINK_HEADS = ("https://", "http://")
LINK_HOSTS = ("t.me/", "telegram.me/", "telegram.dog/")


def username_of(target):
    """Юзернейм из того, что принесли: `@name`, `https://t.me/name`, `name`."""
    text = str(target or "").strip()
    for head in LINK_HEADS:
        if text.lower().startswith(head):
            text = text[len(head):]
    for host in LINK_HOSTS:
        if text.lower().startswith(host):
            text = text[len(host):]
    # Номер темы форума и метки перехода к юзернейму отношения не имеют.
    text = text.split("?")[0].split("/")[0]
    return text.strip().lstrip("@")


def message_link(mid, username=None, chat_id=None):
    """Адрес сообщения. У публичного чата по имени, у закрытого по номеру.

    Единственный формирователь таких ссылок: отчёт и суточный свод зовут его,
    а не собирают адрес каждый по-своему.
    """
    name = (username or "").strip()
    if name:
        return f"https://t.me/{name}/{mid}"
    номер = str(chat_id or "").lstrip("-")
    return f"https://t.me/c/{номер[3:] if номер.startswith('100') else номер}/{mid}"


def _who(entity):
    """Кто скрывается за юзернеймом, если это не чат."""
    return "бот" if getattr(entity, "bot", False) else "человек"


async def resolve_username(app, target):
    """Чат по юзернейму: пара «идентификатор, сущность».

    Так в инструмент попадает чат, о котором он раньше не слышал: индекс тут не
    поможет — попасть в него можно только после выгрузки, а выгрузка требует
    выбора. Публичную группу Телеграм отдаёт по имени и без подписки, поэтому
    сущность берётся напрямую и круг размыкается.

    Отказы разводятся по смыслу: юзернейма нет вовсе, за ним не чат, чат
    закрыт. Одно «не нашёл» на все три случая заставило бы владельца гадать.
    """
    name = username_of(target)
    if not name:
        raise ValueError(NO_NAME)
    if name.startswith("+"):
        raise ValueError(INVITE_LINK.format(target=str(target).strip()))
    try:
        entity = await app.get_entity(name)
    except UnauthorizedError as error:
        raise SessionGone(LOGIN_HINT) from error
    except (UsernameNotOccupiedError, UsernameInvalidError) as error:
        raise UsernameGone(NO_USERNAME.format(name=name)) from error
    except ChannelPrivateError as error:
        raise ChatClosed(CLOSED_CHAT.format(name=name)) from error
    except ValueError as error:
        # Свободный юзернейм Telethon называет сам, до всякого запроса к серверу.
        raise UsernameGone(NO_USERNAME.format(name=name)) from error
    if isinstance(entity, (ChannelForbidden, ChatForbidden)):
        raise ChatClosed(CLOSED_CHAT.format(name=name))
    if not isinstance(entity, (Channel, Chat)):
        raise NotAChat(NOT_A_CHAT.format(name=name, who=_who(entity)))
    return str(get_peer_id(entity)), entity


def kind_of(entity):
    """Канал или группа. Супергруппа для разбора — обычная группа."""
    return "channel" if getattr(entity, "broadcast", False) else "group"




async def chats(app):
    """Список групп и каналов с метаданными. Архив входит в охват наравне."""
    found = []
    async for dialog, archived in dialogs(app):
        if not (dialog.is_group or dialog.is_channel):
            continue
        entity = dialog.entity
        found.append(
            {
                "id": str(dialog.id),
                "title": dialog.name,
                "username": getattr(entity, "username", None),
                "kind": "channel" if dialog.is_channel and not dialog.is_group else "group",
                "participants": getattr(entity, "participants_count", None),
                "archived": archived,
            }
        )
    return found


async def people(app):
    """Личные диалоги владельца. Архив входит в охват наравне.

    Боты и служебные аккаунты Телеграма сюда не попадают: манеры речи в
    переписке с автоответчиком нет, а обходить их за год — потраченное время.
    Переписка с самим собой («Избранное») тоже не диалог: второй стороны в ней
    не бывает.
    """
    me = await app.get_me()
    found = []
    async for dialog, archived in dialogs(app):
        if not dialog.is_user:
            continue
        entity = dialog.entity
        if getattr(entity, "bot", False) or getattr(entity, "is_self", False):
            continue
        if entity.id == me.id or entity.id in SERVICE_ACCOUNTS:
            continue
        found.append(
            {
                "id": str(dialog.id),
                "title": dialog.name,
                "username": getattr(entity, "username", None),
                "phone": getattr(entity, "phone", None),
                "kind": "dm",
                "archived": archived,
                "entity": entity,
            }
        )
    return found


def _seconds(message):
    """Длительность наговоренного, если она есть.

    Берётся сразу при чтении: длительность лежит в том же объекте сообщения, и
    спрашивать её вторым обходом значило бы читать год дважды.
    """
    document = getattr(message, "document", None)
    for attribute in getattr(document, "attributes", ()) or ():
        seconds = getattr(attribute, "duration", None)
        if seconds is not None:
            return int(seconds)
    return None


def _media_kind(message):
    """Чем сообщение было, если текста в нём нет. Голосовое и кружок разведены."""
    media = getattr(message, "media", None)
    if media is None:
        return None
    if getattr(message, "voice", None):
        return "голосовое"
    if getattr(message, "video_note", None):
        return "кружок"
    if getattr(message, "sticker", None):
        return "стикер"
    if getattr(message, "photo", None):
        return "фото"
    if getattr(message, "video", None):
        return "видео"
    if getattr(message, "document", None):
        return "файл"
    return type(media).__name__


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
    media = _media_kind(message)
    if media:
        row["media"] = media
        seconds = _seconds(message)
        if seconds is not None:
            row["secs"] = seconds
    reply_to = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
    if reply_to:
        row["reply_to"] = reply_to
    if in_group:
        name, username = _author(message)
        row["from"] = name
        row["from_username"] = username
        sender_id = getattr(message, "sender_id", None)
        if sender_id is not None:
            row["from_id"] = str(sender_id)
    return row


async def speech(app, entity, since=None, in_group=False, notify=_silent, sleep=asyncio.sleep):
    """Сообщения чата за окно в виде, годном для разбора манеры речи.

    От `fetch` отличается тем, что сохраняется: направление, пересылка,
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
            if error.seconds > FLOOD_LIMIT or waits >= FLOOD_RETRIES:
                raise FloodStop(
                    f"Телеграм просит подождать {error.seconds} с — чат отложен"
                ) from error
            waits += 1
            notify(f"флуд-лимит: ждём {error.seconds} с и читаем чат дальше")
            await sleep(error.seconds + 2)
        except ChannelPrivateError as error:
            raise ChatClosed(
                "Телеграм не отдаёт сообщения: чат закрыт или нас из него убрали"
            ) from error
        except UnauthorizedError as error:
            raise SessionGone(LOGIN_HINT) from error


async def lengths(app, entity, ids, notify=_silent, sleep=asyncio.sleep):
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
                if error.seconds > FLOOD_LIMIT:
                    raise FloodStop(
                        f"Телеграм просит подождать {error.seconds} с — чат отложен"
                    ) from error
                notify(f"флуд-лимит: ждём {error.seconds} с")
                await sleep(error.seconds + 2)
            except UnauthorizedError as error:
                raise SessionGone(LOGIN_HINT) from error
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


async def _filters(app):
    """Папки Телеграма по порядку. Безымянная папка «Все чаты» номера не занимает."""
    result = await app(GetDialogFiltersRequest())
    return [f for f in getattr(result, "filters", result) if hasattr(f, "title")]


async def _folder(app, number):
    """Папка Телеграма по её номеру в списке."""
    filters = await _filters(app)
    if number < 1 or number > len(filters):
        raise ValueError(f"нет папки номер {number}: всего папок {len(filters)}")
    return filters[number - 1]


def _peers(folder):
    """Чаты, добавленные в папку руками: закреплённые считаются наравне."""
    return list(getattr(folder, "pinned_peers", [])) + list(getattr(folder, "include_peers", []))


def _title(folder):
    """Название папки. Телеграм отдаёт его объектом с разметкой, печатать надо текст."""
    title = getattr(folder, "title", None)
    return getattr(title, "text", title)


async def folders(app):
    """Папки с номерами: номер, название, сколько чатов вручную, автоматический набор.

    Номер тот же, что берут `folder_ids` и `add_to_folder`, иначе выгрузка пошла
    бы не по той папке.
    """
    return [
        {
            "number": number,
            "title": _title(folder),
            "chats": len(_peers(folder)),
            "auto": [name for name in AUTO_SETS if getattr(folder, name, False)],
        }
        for number, folder in enumerate(await _filters(app), 1)
    ]


async def folder_ids(app, number):
    """Идентификаторы чатов из папки Телеграма по её номеру."""
    folder = await _folder(app, number)
    return {str(get_peer_id(peer)) for peer in _peers(folder)}


async def add_to_folder(app, number, chats):
    """Добавляет чаты в папку Телеграма по её номеру.

    Чат берётся числом или юзернеймом. Юзернейм надёжнее: числовые
    идентификаторы едут после каждой выгрузки, и подстановка вчерашних однажды
    увела шесть чужих чатов с личной перепиской в стороннюю модель.
    """
    folder = await _folder(app, number)
    # Закреплённые — тоже «уже в папке»: без них закреплённый чат добавлялся
    # в include вторым экземпляром, и Телеграм получал фильтр с дублем.
    known = {get_peer_id(peer) for peer in _peers(folder)}
    for chat in chats:
        кого = int(chat) if str(chat).lstrip("-").isdigit() else str(chat)
        entity = await app.get_entity(кого)
        if get_peer_id(entity) in known:
            continue
        folder.include_peers.append(await app.get_input_entity(entity))
    await app(UpdateDialogFilterRequest(id=folder.id, filter=folder))
    return len(folder.include_peers)


async def _topics(app, entity):
    """Темы форумной группы: номер -> название. Обычный чат тем не имеет."""
    if not getattr(entity, "forum", False):
        return None
    result = await app(
        GetForumTopicsRequest(
            peer=entity, offset_date=None, offset_id=0, offset_topic=0, limit=TOPIC_LIMIT
        )
    )
    return {
        topic.id: topic.title
        for topic in getattr(result, "topics", [])
        if getattr(topic, "title", None)
    }


def _migration(rows, action):
    """Служебное сообщение о переезде: новый идентификатор нужен состоянию."""
    if isinstance(action, MessageActionChatMigrateTo):
        rows.migrated_to = str(get_peer_id(PeerChannel(action.channel_id)))
    elif isinstance(action, MessageActionChannelMigrateFrom):
        rows.migrated_from = str(get_peer_id(PeerChat(action.chat_id)))


def _author(message):
    """Имя и юзернейм. У анонимного админа вместо имени его подпись."""
    try:
        sender = message.sender
    except Exception:
        sender = None
    name = None
    username = None
    if sender is not None:
        name = " ".join(
            filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)])
        ) or getattr(sender, "title", None)
        username = getattr(sender, "username", None)
    if not name:
        name = getattr(message, "post_author", None)
    return name or None, username


def _thread(message, topics):
    """Номер родителя и название темы. В форуме корень темы ответом не считается."""
    head = getattr(message, "reply_to", None)
    reply_to = getattr(head, "reply_to_msg_id", None)
    if topics is None or head is None or not getattr(head, "forum_topic", False):
        return reply_to, None
    top = getattr(head, "reply_to_top_id", None)
    if top:
        return reply_to, topics.get(top)
    return None, topics.get(reply_to)


def _reactions(message):
    """Сколько реакций собрало сообщение — всеми эмодзи разом.

    Telethon отдаёт их объектом `MessageReactions` в поле `reactions`, а внутри —
    список `ReactionCount` со своим счётчиком на каждую эмодзи. Реакций может не
    быть вовсе, и тогда поля нет: `None` здесь обычнее объекта.

    Считаем сумму, а какими эмодзи откликались — нет: в отчёте это число
    конкуренции за заказ, а не настроение читателей.
    """
    counts = getattr(getattr(message, "reactions", None), "results", None) or ()
    return sum(getattr(item, "count", 0) or 0 for item in counts)


# Через сколько прочитанных сообщений подавать голос. Чат на десятки тысяч
# сообщений читается часами, и без отметок работающий обход неотличим от
# повисшего — на этом в проекте уже убили живой прогон руками.
WALK_MARK = 2000


async def _walk(app, entity, since, until, limit, min_id, topics, notify=_silent):
    """Один проход по чату. Флуд-лимит наверх, разбор строк здесь."""
    rows = Rows()
    seen = 0
    async for message in app.iter_messages(entity, limit=limit, min_id=min_id):
        seen += 1
        if seen % WALK_MARK == 0:
            # Дата важнее счёта: по ней видно, сколько окна осталось пройти.
            дошли = getattr(message, "date", None)
            notify(f"прочитано {seen}, дошли до {дошли.date() if дошли else 'неизвестно'}")
        date = getattr(message, "date", None)
        if since and date and date < since:
            break
        action = getattr(message, "action", None)
        if action is not None:
            _migration(rows, action)
            continue
        if until and date and date > until:
            continue
        # Подпись к картинке, голосовому или опросу лежит в том же поле, что и
        # текст: `Message.text` отдаёт `Message.message` через разметку клиента.
        # У сообщения без клиента это свойство пустое, поэтому подпись берётся
        # из `message` — иначе она молча пропадёт.
        text = (getattr(message, "text", None) or getattr(message, "message", None) or "").strip()
        reply_to, topic = _thread(message, topics)
        if topic:
            text = f"[тема: {topic}] {text}".strip()
        name, username = _author(message)
        comments = getattr(getattr(message, "replies", None), "replies", None)
        row = {
            "mid": message.id,
            "date": date.isoformat()[:16] if date else None,
            "from": name,
            "username": username,
            "reply_to": reply_to,
            "comments": comments or 0,
            "reactions": _reactions(message),
            # Сообщение без текста место в ленте держит: номер нужен разметке.
            "text": text,
        }
        # Просмотры Телеграм считает только постам каналов. В группе их нет вовсе,
        # и `"views": null` в каждой строке выгрузки — это вес без сведений.
        views = getattr(message, "views", None)
        if views is not None:
            row["views"] = views
        rows.append(row)
    return rows


async def fetch(
    app,
    entity,
    since=None,
    until=None,
    limit=4000,
    min_id=0,
    notify=_silent,
    sleep=asyncio.sleep,
):
    """Сообщения чата за окно. Флуд-лимит пережидается один раз."""
    waited = False
    while True:
        try:
            topics = await _topics(app, entity)
            return await _walk(app, entity, since, until, limit, min_id, topics, notify)
        except FloodWaitError as error:
            if waited:
                raise FloodStop(
                    f"Телеграм просит подождать ещё {error.seconds} с — чат отложен"
                ) from error
            if error.seconds > FLOOD_LIMIT:
                raise FloodStop(
                    f"Телеграм просит подождать {error.seconds} с, это больше {FLOOD_LIMIT} —"
                    " чат отложен"
                ) from error
            notify(f"флуд-лимит: ждём {error.seconds} с и читаем чат заново")
            await sleep(error.seconds + 2)
            waited = True
        except ChannelPrivateError as error:
            # Сущность взялась, а сообщения не отдают: чат закрыли или нас из
            # него убрали. «Телеграм отказал» тут ничего владельцу не говорит.
            raise ChatClosed(
                "Телеграм не отдаёт сообщения: чат закрыт или нас из него убрали"
            ) from error
        except UnauthorizedError as error:
            raise SessionGone(LOGIN_HINT) from error


def _linked(full):
    """Группа обсуждения из полных сведений о канале."""
    linked = getattr(getattr(full, "full_chat", None), "linked_chat_id", None)
    if not linked:
        return None
    title = None
    username = None
    for chat in getattr(full, "chats", []):
        if getattr(chat, "id", None) == linked:
            title = getattr(chat, "title", None)
            username = getattr(chat, "username", None)
    return {"id": str(get_peer_id(PeerChannel(linked))), "title": title, "username": username}


async def discussion(app, channel):
    """Группа обсуждения канала: отклики на посты лежат там, а не под постом."""
    return _linked(await app(GetFullChannelRequest(channel)))


def write_access(entity):
    """Можно ли писать в чат — до того, как в него вступили.

    Вопрос стоит рядом с «что за чат»: в канале-агрегаторе постят только админы,
    и предлагать там услуги некому. Права участника Телеграм отдаёт вместе с
    сущностью, отдельной просьбы для них не нужно.
    """
    if getattr(entity, "broadcast", False):
        if getattr(entity, "creator", False) or getattr(entity, "admin_rights", None):
            return WRITE_OPEN
        return WRITE_ADMINS
    banned = getattr(entity, "default_banned_rights", None)
    if getattr(banned, "send_messages", False):
        return WRITE_BANNED
    if getattr(entity, "left", False):
        return WRITE_JOIN
    return WRITE_OPEN


async def _full(app, entity):
    """Полные сведения о чате. У обычной группы и у канала разные запросы."""
    if isinstance(entity, Chat):
        return await app(GetFullChatRequest(entity.id))
    return await app(GetFullChannelRequest(entity))


async def _pinned_text(app, entity, mid):
    """Текст закреплённого сообщения: в нём обычно и лежат правила про рекламу.

    Закреп может не отдаться — тогда его просто нет. Ронять из-за него всю
    справку о чате нельзя: остальное в ней важнее.
    """
    if not mid:
        return None
    try:
        message = await app.get_messages(entity, ids=mid)
    except Exception:
        return None
    text = (getattr(message, "text", None) or getattr(message, "message", None) or "").strip()
    if len(text) > PINNED_LIMIT:
        text = text[:PINNED_LIMIT].rstrip() + "…"
    return text or None


async def about(app, entity):
    """Всё о чате, что видно до выгрузки: люди, описание, правила, куда откликаться.

    Это первый вопрос о незнакомом чате — «что там и стоит ли туда идти», — и
    отвечать на него разбором за деньги незачем: Телеграм отдаёт всё это даром.
    """
    try:
        full = await _full(app, entity)
    except UnauthorizedError as error:
        raise SessionGone(LOGIN_HINT) from error
    except ChannelPrivateError as error:
        raise ChatClosed(
            CLOSED_CHAT.format(name=getattr(entity, "username", None) or getattr(entity, "id", ""))
        ) from error
    inside = getattr(full, "full_chat", None)
    linked = _linked(full)
    description = (getattr(inside, "about", None) or "").strip()
    broadcast = bool(getattr(entity, "broadcast", False))
    return {
        "id": str(get_peer_id(entity)),
        "title": getattr(entity, "title", None),
        "username": getattr(entity, "username", None),
        "kind": kind_of(entity),
        "forum": bool(getattr(entity, "forum", False)),
        "participants": (
            getattr(inside, "participants_count", None)
            or getattr(entity, "participants_count", None)
        ),
        "online": getattr(inside, "online_count", None),
        "description": description or None,
        "pinned": await _pinned_text(app, entity, getattr(inside, "pinned_msg_id", None)),
        "linked": linked,
        # Тот же расклад, что и в выгрузке, но без второго запроса: связанную
        # группу мы только что видели в тех же полных сведениях.
        "replies": (
            REPLIES_THREAD if not broadcast
            else REPLIES_COMMENTS if linked
            else REPLIES_NONE
        ),
        "write": write_access(entity),
        "joined": not getattr(entity, "left", False),
        "slowmode": getattr(inside, "slowmode_seconds", None) or None,
    }


async def replies_kind(app, entity):
    """Где по этому чату виден отклик на заказ. Считается один раз на чат.

    Обычная группа — отвечают в тред, отклики на виду. Канал с привязанной
    группой обсуждения — отклики лежат комментариями, и Телеграм считает их сам.
    Канал без обсуждения — откликнуться в Телеграме негде: контакт даётся в
    тексте поста, а пишут ему в личку, и увидеть эти отклики нельзя ничем.

    Телеграм отказал — возвращается None: «обсуждения нет» здесь было бы
    выдумкой, а отчёт по такой выдумке объявил бы отклики невидимыми.
    """
    if not getattr(entity, "broadcast", False):
        return REPLIES_THREAD
    try:
        linked = await discussion(app, entity)
    except UnauthorizedError as error:
        raise SessionGone(LOGIN_HINT) from error
    except Exception:
        return None
    return REPLIES_COMMENTS if linked else REPLIES_NONE


async def search_public(app, query, limit=50):
    """Публичные чаты по слову в названии или юзернейме."""
    result = await app(SearchRequest(q=query, limit=limit))
    return [
        {
            "title": getattr(chat, "title", None),
            "username": getattr(chat, "username", None),
            "participants": getattr(chat, "participants_count", None),
            "kind": "channel" if getattr(chat, "broadcast", False) else "group",
            "joined": not getattr(chat, "left", True),
        }
        for chat in result.chats
    ]


async def _join_one(app, target):
    """Вступление по ссылке-приглашению или по юзернейму."""
    if "joinchat" in target or "+" in target:
        code = target.split("/")[-1].lstrip("+")
        return await app(ImportChatInviteRequest(code))
    return await app(JoinChannelRequest(target))


async def join(app, targets, confirm, notify=_silent, sleep=asyncio.sleep):
    """Вступление с паузами и потолком за запуск. Без согласия ничего не делает."""
    targets = list(targets)
    if len(targets) > JOIN_LIMIT_PER_RUN:
        notify(
            f"за один запуск вступаем не больше чем в {JOIN_LIMIT_PER_RUN} чатов:"
            f" отложено {len(targets) - JOIN_LIMIT_PER_RUN}"
        )
        targets = targets[:JOIN_LIMIT_PER_RUN]

    occupied = len(await chats(app))
    if occupied + len(targets) > CHAT_CAP:
        raise ChatCapReached(
            f"потолок аккаунта {CHAT_CAP} групп и каналов: занято {occupied},"
            f" просят вступить ещё в {len(targets)}"
        )
    if occupied + len(targets) > CHAT_CAP - CAP_WARN:
        notify(f"близко к потолку аккаунта: занято {occupied} из {CHAT_CAP}")

    if not confirm(targets):
        return []

    done = []
    for position, target in enumerate(targets):
        if position:
            await sleep(JOIN_PAUSE)
        try:
            await _join_one(app, target)
            status = "вступил"
        except InviteRequestSentError:
            status = "заявка подана"
        except PeerFloodError as error:
            raise SpamRestricted(
                "аккаунт ограничен за спам: вступления остановлены, повторять нельзя"
            ) from error
        except ChannelsTooMuchError as error:
            raise ChatCapReached(
                f"Телеграм отказал: у аккаунта уже предельное число чатов (около {CHAT_CAP})"
            ) from error
        except FloodWaitError as error:
            raise FloodStop(
                f"Телеграм просит подождать {error.seconds} с: вступления остановлены"
            ) from error
        notify(f"{target}: {status}")
        done.append({"target": target, "status": status})
    return done


async def leave(app, chat_ids, confirm, notify=_silent):
    """Выход из чатов после подтверждения."""
    chat_ids = list(chat_ids)
    if not confirm(chat_ids):
        return []
    left = []
    for chat_id in chat_ids:
        entity = await app.get_entity(int(chat_id))
        await app(LeaveChannelRequest(entity))
        notify(f"{chat_id}: вышли")
        left.append(chat_id)
    return left


def _moment(text):
    """Дата из строки. Свой часовой пояс сохраняется, иначе считаем UTC."""
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def window(days=None, since=None, until=None):
    """Границы окна как пара дат с часовым поясом."""
    end = _moment(until) if until else None
    if since:
        start = _moment(since)
    elif days:
        start = datetime.now(timezone.utc) - timedelta(days=days)
    else:
        start = None
    return start, end


async def say(app, кому, текст, про=""):
    """Отправить сообщение. Адресат — юзернейм, а не число.

    Числовые идентификаторы едут после каждой выгрузки, и подстановка
    вчерашних однажды увела шесть чужих чатов с личной перепиской в стороннюю
    модель. Юзернейм не меняется.

    Пустое сообщение — ошибка вызова, а не повод дёргать Телеграм: отправку
    отменить нельзя, и лишний запрос к аккаунту владельца тут ни к чему.

    **Чёрный список приманок сверяется здесь, а не у вызывающего.** 08.09.2026
    два пинга ушли ботам, лежавшим в списке с 02.09: правило «перед любым
    исходящим сверяться» жило в памяти агента, и разовый скрипт проверил не тот
    ключ. Это единственная дверь наружу, поэтому сверка стоит в ней: обойти её
    можно только сознательно. Список не читается — не уходит никто: сломанная
    проверка хуже отсутствующей.

    **Журнал отправок пишется здесь же**, после того как Телеграм принял
    сообщение: время, адресат, дословный текст, номер сообщения. `про` — о
    чём письмо, одной строкой. Строка руками не дублируется.
    """
    if not (текст or "").strip():
        raise ValueError("отправлять нечего: текст пуст")
    if str(кому).strip().lstrip("-").isdigit():
        # Правило из докстринга, теперь и в коде: число принималось молча, а
        # защита от повторения дорогого инцидента жила только в прозе.
        raise ValueError(
            f"адресат «{кому}» — число, а отправка идёт только по юзернейму:"
            " числовые идентификаторы едут после каждой выгрузки"
        )
    # Импорт внутри: `discovery.orders` сам импортирует этот модуль ради
    # ссылок на сообщения, а слой разведки ниже клиента. Сверка всё равно
    # должна стоять у двери, поэтому цикл разорван здесь, а не убран.
    from .discovery import orders
    запись = orders.запись_бота(username_of(кому))
    if запись:
        raise KnownBot(
            f"адресат «{кому}» — бот-приманка {запись['username']}, в чёрном "
            f"списке с {запись.get('подтверждён', '?')}: {запись.get('чем', '')}"
        )
    try:
        отправлено = await app.send_message(кому, текст)
    except UnauthorizedError as error:
        raise SessionGone(LOGIN_HINT) from error
    except FloodWaitError as error:
        raise FloodStop(f"Телеграм просит подождать: {error}") from error
    except PeerFloodError as error:
        raise SpamRestricted(
            "аккаунт ограничен за рассылку — отправка недоступна"
        ) from error
    orders.записать_отправку(username_of(кому), текст, про=про,
                             message_id=getattr(отправлено, "id", None))
    return отправлено
