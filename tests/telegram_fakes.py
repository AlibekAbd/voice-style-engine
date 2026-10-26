"""Подменный Telethon для тестов ядра и заказов: сеть не дёргаем, аккаунт не трогаем."""

from datetime import datetime, timezone
from types import SimpleNamespace

from telethon.tl.functions.channels import (
    GetFullChannelRequest,
    JoinChannelRequest,
    LeaveChannelRequest,
)
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.messages import (
    GetDialogFiltersRequest,
    GetForumTopicsRequest,
    ImportChatInviteRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.types import (
    Channel,
    InputPeerChannel,
    MessageReactions,
    PeerChannel,
    ReactionCount,
    ReactionEmoji,
)



def moment(day=14, hour=7, minute=8):
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)


def message(
    mid=1,
    text="привет",
    date=None,
    sender=None,
    action=None,
    reply_to=None,
    replies=None,
    post_author=None,
    reactions=None,
    views=None,
):
    return SimpleNamespace(
        id=mid,
        text=text,
        date=date or moment(),
        sender=sender,
        action=action,
        reply_to=reply_to,
        replies=replies,
        post_author=post_author,
        reactions=reactions,
        views=views,
    )


def reacted(*pairs):
    """Реакции сообщения так, как их отдаёт Telethon: список счётчиков по эмодзи."""
    return MessageReactions(
        results=[
            ReactionCount(reaction=ReactionEmoji(emoticon=emoji), count=count)
            for emoji, count in pairs
        ]
    )


def person(first="Arman", last=None, username="arman_test"):
    return SimpleNamespace(first_name=first, last_name=last, username=username, title=None)


def header(reply_to_msg_id=None, reply_to_top_id=None, forum_topic=False):
    return SimpleNamespace(
        reply_to_msg_id=reply_to_msg_id,
        reply_to_top_id=reply_to_top_id,
        forum_topic=forum_topic,
    )


def dialog(
    chat_id=-100777, name="Пример", is_group=True, is_channel=False, entity=None, archived=False
):
    return SimpleNamespace(
        id=chat_id,
        name=name,
        is_group=is_group,
        is_channel=is_channel,
        archived=archived,
        entity=entity or SimpleNamespace(username="primer", participants_count=6679),
    )


def topic(topic_id=7, title="Вакансии"):
    return SimpleNamespace(id=topic_id, title=title)


def dialog_filter(title="Работа", pinned=(), included=(), **auto):
    """Папка Телеграма: ручные чаты списками, автоматический набор флагами."""
    return SimpleNamespace(
        id=8,
        title=title,
        pinned_peers=list(pinned),
        include_peers=list(included),
        contacts=auto.get("contacts", False),
        non_contacts=auto.get("non_contacts", False),
        groups=auto.get("groups", False),
        broadcasts=auto.get("broadcasts", False),
        bots=auto.get("bots", False),
    )


class Client:
    """Подменный клиент: отдаёт заготовленное и кидает заказанные ошибки."""

    def __init__(
        self,
        dialogs=(),
        archived=(),
        messages=(),
        stumbles=(),
        topics=None,
        full=None,
        search=(),
        filters=(),
        joins=(),
        authorized=True,
        dialog_error=None,
        message_error=None,
        entity=None,
        pinned=None,
    ):
        # Что Телеграм отдаёт на просьбу открыть чат: сущность или отказ.
        self.entity = entity
        self.pinned = pinned
        self.dialogs = list(dialogs)
        self.archived = list(archived)
        self.messages = list(messages)
        self.stumbles = list(stumbles)
        self.topics = topics
        self.full = full
        self.search = list(search)
        self.filters = list(filters)
        self.joins = list(joins)
        self.authorized = authorized
        self.dialog_error = dialog_error
        self.message_error = message_error
        self.requests = []
        self.walks = 0
        self.connected = False

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        if isinstance(self.authorized, Exception):
            raise self.authorized
        return self.authorized

    def iter_messages(self, entity, limit=None, min_id=0):
        client = self

        async def walk():
            client.walks += 1
            if client.message_error:
                raise client.message_error
            if client.stumbles:
                raise client.stumbles.pop(0)
            for item in client.messages:
                yield item

        return walk()

    def iter_dialogs(self, archived=None):
        """Как настоящий: архив приходит только по отдельной просьбе."""
        client = self

        async def walk():
            if client.dialog_error:
                raise client.dialog_error
            for item in (client.archived if archived else client.dialogs):
                yield item

        return walk()

    async def get_entity(self, value):
        if self.entity is not None:
            if isinstance(self.entity, Exception):
                raise self.entity
            return self.entity
        return PeerChannel(abs(int(value)) % 1000)

    async def get_messages(self, entity, ids=None):
        """Закреплённое сообщение: Телеграм отдаёт его отдельной просьбой по номеру."""
        if isinstance(self.pinned, Exception):
            raise self.pinned
        return SimpleNamespace(text=self.pinned) if self.pinned else None

    async def get_input_entity(self, value):
        number = getattr(value, "channel_id", None) or abs(int(getattr(value, "id", value))) % 1000
        return InputPeerChannel(channel_id=number, access_hash=0)

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, GetForumTopicsRequest):
            return SimpleNamespace(topics=list(self.topics or []))
        if isinstance(request, GetFullChannelRequest):
            return self.full
        if isinstance(request, SearchRequest):
            return SimpleNamespace(chats=list(self.search))
        if isinstance(request, GetDialogFiltersRequest):
            return SimpleNamespace(filters=list(self.filters))
        if isinstance(request, (JoinChannelRequest, ImportChatInviteRequest)):
            outcome = self.joins.pop(0) if self.joins else None
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if isinstance(request, (LeaveChannelRequest, UpdateDialogFilterRequest)):
            return True
        raise AssertionError(f"неожиданный запрос: {type(request).__name__}")


class Waits(list):
    """Подменная пауза: копит, сколько нас просили спать."""

    async def __call__(self, seconds):
        self.append(seconds)


def public_group(number=888, title="Образец Чат", username="sample_chat", people=4200, **flags):
    """Публичная группа так, как её отдаёт Телеграм по юзернейму: подписки нет."""
    flags.setdefault("left", True)
    return Channel(
        id=number, title=title, photo=None, date=None, username=username,
        megagroup=True, participants_count=people, **flags
    )


def public_channel(number=999, title="Второй канал", username="vtoroi", **flags):
    flags.setdefault("left", True)
    return Channel(
        id=number, title=title, photo=None, date=None, username=username,
        broadcast=True, participants_count=12000, **flags
    )
