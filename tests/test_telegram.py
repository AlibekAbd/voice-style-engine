"""Подменный Telethon: сеть не дёргаем, аккаунт не трогаем."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon.errors import (
    AuthKeyUnregisteredError,
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
    ImportChatInviteRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.types import (
    Channel,
    ChatBannedRights,
    InputPeerChannel,
    Message,
    MessageActionChannelMigrateFrom,
    MessageActionChatMigrateTo,
    MessageMediaPhoto,
    MessageReactions,
    PeerChannel,
    ReactionCount,
    ReactionCustomEmoji,
    ReactionEmoji,
    TextWithEntities,
    User,
)

from tgservice import telegram

try:
    from tgservice.discovery import common as discovery_common
except ModuleNotFoundError:
    # Слой разведки живёт в основном проекте и в репозиторий одного только
    # voice не входит. Без него `say` не проверить — её тесты пропускаются,
    # остальной модуль от разведки не зависит.
    discovery_common = None

нужна_разведка = pytest.mark.skipif(
    discovery_common is None, reason="слой разведки не входит в этот репозиторий"
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


def joins_of(client):
    return [
        request.channel
        for request in client.requests
        if isinstance(request, JoinChannelRequest)
    ]


# ── выгрузка ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_row_keeps_the_dump_format():
    client = Client(messages=[message(mid=248207, sender=person(), replies=SimpleNamespace(replies=3))])

    rows = await telegram.fetch(client, "chat")

    assert rows[0] == {
        "mid": 248207,
        "date": "2026-08-14T07:08",
        "from": "Arman",
        "username": "arman_test",
        "reply_to": None,
        "comments": 3,
        "reactions": 0,
        "text": "привет",
    }


@pytest.mark.asyncio
async def test_media_caption_is_kept():
    """Подпись к картинке лежит там же, где текст: терять её нельзя."""
    client = Client(messages=[message(text="нужен сайт для клиники", sender=person())])

    rows = await telegram.fetch(client, "chat")

    assert rows[0]["text"] == "нужен сайт для клиники"


@pytest.mark.asyncio
async def test_real_media_message_gives_up_its_caption():
    """У настоящего сообщения Telethon подпись лежит в `message`, а `text` бывает пустым."""
    photo = Message(
        id=7,
        peer_id=PeerChannel(1000000001),
        date=moment(),
        message="нужен сайт для клиники",
        media=MessageMediaPhoto(),
    )
    client = Client(messages=[photo])

    rows = await telegram.fetch(client, "chat")

    assert rows[0]["text"] == "нужен сайт для клиники"


@pytest.mark.asyncio
async def test_message_without_text_keeps_its_place():
    """Картинка без подписи и голосовое остаются в ленте: номер нужен разметке."""
    client = Client(messages=[message(mid=5, text=None, sender=person())])

    rows = await telegram.fetch(client, "chat")

    assert [row["mid"] for row in rows] == [5]
    assert rows[0]["text"] == ""


@pytest.mark.asyncio
async def test_anonymous_admin_is_signed_by_his_title():
    client = Client(messages=[message(sender=None, post_author="Админ")])

    rows = await telegram.fetch(client, "chat")

    assert rows[0]["from"] == "Админ"
    assert rows[0]["username"] is None


@pytest.mark.asyncio
async def test_anonymous_admin_without_signature_has_no_author():
    client = Client(messages=[message(sender=None)])

    rows = await telegram.fetch(client, "chat")

    assert rows[0]["from"] is None


@pytest.mark.asyncio
async def test_service_message_is_skipped():
    client = Client(
        messages=[
            message(mid=1, action=SimpleNamespace(), sender=person()),
            message(mid=2, sender=person()),
        ]
    )

    rows = await telegram.fetch(client, "chat")

    assert [row["mid"] for row in rows] == [2]


@pytest.mark.asyncio
async def test_window_edges_are_respected():
    client = Client(
        messages=[
            message(mid=3, date=moment(day=16), sender=person()),
            message(mid=2, date=moment(day=14), sender=person()),
            message(mid=1, date=moment(day=10), sender=person()),
        ]
    )

    rows = await telegram.fetch(client, "chat", since=moment(day=12), until=moment(day=15))

    assert [row["mid"] for row in rows] == [2]


# ── миграция ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_migration_to_supergroup_gives_the_new_id():
    """Иначе чат перечитают целиком как новый и заплатят за разбор второй раз."""
    client = Client(
        messages=[
            message(
                mid=2,
                action=MessageActionChatMigrateTo(channel_id=1000000001),
                sender=person(),
            ),
            message(mid=1, sender=person()),
        ]
    )

    rows = await telegram.fetch(client, "chat")

    assert rows.migrated_to == "-1001000000001"
    assert [row["mid"] for row in rows] == [1]


@pytest.mark.asyncio
async def test_migration_source_gives_the_old_id():
    client = Client(
        messages=[
            message(
                mid=1,
                action=MessageActionChannelMigrateFrom(title="Пример", chat_id=42),
                sender=person(),
            )
        ]
    )

    rows = await telegram.fetch(client, "chat")

    assert rows.migrated_from == "-42"


@pytest.mark.asyncio
async def test_ordinary_chat_reports_no_migration():
    client = Client(messages=[message(sender=person())])

    rows = await telegram.fetch(client, "chat")

    assert rows.migrated_to is None
    assert rows.migrated_from is None


# ── форум ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forum_topic_is_named_in_the_text():
    """Без темы параллельные разговоры сливаются в мешанину."""
    client = Client(
        messages=[message(mid=9, text="ищу подрядчика", sender=person(),
                          reply_to=header(reply_to_msg_id=7, forum_topic=True))],
        topics=[topic(7, "Вакансии")],
    )
    entity = SimpleNamespace(forum=True, username=None, participants_count=None)

    rows = await telegram.fetch(client, entity)

    assert rows[0]["text"] == "[тема: Вакансии] ищу подрядчика"


@pytest.mark.asyncio
async def test_forum_root_message_is_not_a_reply():
    """Формально сообщение темы отвечает её корню: настоящей веткой это не считается."""
    client = Client(
        messages=[message(mid=9, sender=person(), reply_to=header(reply_to_msg_id=7, forum_topic=True))],
        topics=[topic(7, "Вакансии")],
    )
    entity = SimpleNamespace(forum=True)

    rows = await telegram.fetch(client, entity)

    assert rows[0]["reply_to"] is None


@pytest.mark.asyncio
async def test_forum_reply_keeps_its_parent():
    client = Client(
        messages=[
            message(
                mid=11,
                sender=person(),
                reply_to=header(reply_to_msg_id=9, reply_to_top_id=7, forum_topic=True),
            )
        ],
        topics=[topic(7, "Вакансии")],
    )
    entity = SimpleNamespace(forum=True)

    rows = await telegram.fetch(client, entity)

    assert rows[0]["reply_to"] == 9
    assert rows[0]["text"].startswith("[тема: Вакансии]")


@pytest.mark.asyncio
async def test_plain_chat_is_not_asked_about_topics():
    client = Client(messages=[message(sender=person())])

    await telegram.fetch(client, SimpleNamespace(forum=False))

    assert not any(isinstance(r, GetForumTopicsRequest) for r in client.requests)


@pytest.mark.asyncio
async def test_unknown_topic_leaves_text_alone():
    client = Client(
        messages=[message(text="привет", sender=person(),
                          reply_to=header(reply_to_msg_id=99, forum_topic=True))],
        topics=[topic(7, "Вакансии")],
    )

    rows = await telegram.fetch(client, SimpleNamespace(forum=True))

    assert rows[0]["text"] == "привет"


# ── реакции и просмотры ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reactions_are_counted_all_together():
    """Реакция — второй след интереса к заказу: где тредов нет, она единственная."""
    client = Client(messages=[message(sender=person(), reactions=reacted(("👍", 5), ("🔥", 2)))])

    rows = await telegram.fetch(client, "chat")

    assert rows[0]["reactions"] == 7


@pytest.mark.asyncio
async def test_message_without_reactions_counts_zero():
    """Реакций может не быть вовсе: у Telethon это None, а не пустой список."""
    client = Client(messages=[message(sender=person())])

    rows = await telegram.fetch(client, "chat")

    assert rows[0]["reactions"] == 0


@pytest.mark.asyncio
async def test_custom_emoji_reaction_is_counted_too():
    """Платная эмодзи-реакция считается наравне: эмодзи мы не собираем, только счёт."""
    reactions = MessageReactions(
        results=[ReactionCount(reaction=ReactionCustomEmoji(document_id=42), count=3)]
    )
    client = Client(messages=[message(sender=person(), reactions=reactions)])

    rows = await telegram.fetch(client, "chat")

    assert rows[0]["reactions"] == 3


@pytest.mark.asyncio
async def test_channel_post_keeps_its_views():
    client = Client(messages=[message(sender=person(), views=1200)])

    rows = await telegram.fetch(client, "chat")

    assert rows[0]["views"] == 1200


@pytest.mark.asyncio
async def test_group_message_carries_no_views_field():
    """Просмотры Телеграм считает только постам каналов: в группе поля нет вовсе."""
    client = Client(messages=[message(sender=person())])

    rows = await telegram.fetch(client, "chat")

    assert "views" not in rows[0]


# ── где отклик вообще виден ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_answers_are_seen_in_the_thread():
    client = Client()

    assert await telegram.replies_kind(client, SimpleNamespace()) == telegram.REPLIES_THREAD


@pytest.mark.asyncio
async def test_channel_with_a_discussion_group_shows_comments():
    client = Client(
        full=SimpleNamespace(
            full_chat=SimpleNamespace(linked_chat_id=1000000001),
            chats=[SimpleNamespace(id=1000000001, title="Пример чат", username="primer_chat")],
        )
    )

    kind = await telegram.replies_kind(client, SimpleNamespace(broadcast=True))

    assert kind == telegram.REPLIES_COMMENTS


@pytest.mark.asyncio
async def test_channel_without_discussion_has_nowhere_to_answer():
    """Канал-агрегатор: контакт даётся в тексте поста, откликаются в личку."""
    client = Client(full=SimpleNamespace(full_chat=SimpleNamespace(linked_chat_id=None), chats=[]))

    kind = await telegram.replies_kind(client, SimpleNamespace(broadcast=True))

    assert kind == telegram.REPLIES_NONE


@pytest.mark.asyncio
async def test_unanswerable_question_about_the_channel_is_not_a_verdict():
    """Телеграм отказал — признака нет. Придумывать «нет обсуждения» нельзя."""

    class Refusing(Client):
        async def __call__(self, request):
            raise ValueError("канал недоступен")

    kind = await telegram.replies_kind(Refusing(), SimpleNamespace(broadcast=True))

    assert kind is None


# ── группа обсуждения ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_channel_discussion_group_is_found():
    """Отклики на заказы лежат в связанной группе, а не под постом канала."""
    client = Client(
        full=SimpleNamespace(
            full_chat=SimpleNamespace(linked_chat_id=1000000001),
            chats=[
                SimpleNamespace(id=1000000001, title="Пример чат", username="primer_chat")
            ],
        )
    )

    found = await telegram.discussion(client, "channel")

    assert found == {
        "id": "-1001000000001",
        "title": "Пример чат",
        "username": "primer_chat",
    }


@pytest.mark.asyncio
async def test_channel_without_discussion_group():
    client = Client(full=SimpleNamespace(full_chat=SimpleNamespace(linked_chat_id=None), chats=[]))

    assert await telegram.discussion(client, "channel") is None


# ── сессия ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoked_session_tells_how_to_log_in_again():
    client = Client(dialog_error=AuthKeyUnregisteredError(request=None))

    with pytest.raises(telegram.SessionGone) as failure:
        await telegram.chats(client)

    assert "login" in str(failure.value)


@pytest.mark.asyncio
async def test_revoked_session_during_dump_is_named_too():
    client = Client(message_error=AuthKeyUnregisteredError(request=None))

    with pytest.raises(telegram.SessionGone):
        await telegram.fetch(client, "chat")


def test_client_signs_the_session_recognizably(monkeypatch):
    """Безымянную сессию владелец не опознает в списке устройств Телеграма."""
    seen = {}

    class Fake:
        def __init__(self, session, api_id, api_hash, **extra):
            seen.update(extra)

    monkeypatch.setattr(
        telegram.config, "load_env",
        lambda *args, **kwargs: {"TG_API_ID": "42", "TG_API_HASH": "hash"},
    )
    monkeypatch.setattr(telegram, "TelegramClient", Fake)

    telegram.client()

    assert seen["device_model"] == "Desktop"
    assert seen["system_version"] == "Windows 11"


@pytest.mark.asyncio
async def test_start_without_session_tells_how_to_log_in():
    client = Client(authorized=False)

    with pytest.raises(telegram.SessionGone) as failure:
        await telegram.start(client)

    assert "login" in str(failure.value)


@pytest.mark.asyncio
async def test_start_returns_the_connected_client():
    client = Client()

    assert await telegram.start(client) is client
    assert client.connected


# ── флуд-лимит ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flood_wait_is_waited_out_and_announced():
    client = Client(
        messages=[message(sender=person())],
        stumbles=[FloodWaitError(request=None, capture=30)],
    )
    waits, notes = Waits(), []

    rows = await telegram.fetch(client, "chat", notify=notes.append, sleep=waits)

    assert len(rows) == 1
    assert waits and waits[0] >= 30
    assert "30" in " ".join(notes)


@pytest.mark.asyncio
async def test_long_flood_wait_stops_the_chat():
    client = Client(stumbles=[FloodWaitError(request=None, capture=telegram.FLOOD_LIMIT + 1)])
    waits = Waits()

    with pytest.raises(telegram.FloodStop):
        await telegram.fetch(client, "chat", notify=lambda text: None, sleep=waits)

    assert waits == []


@pytest.mark.asyncio
async def test_second_flood_wait_stops_the_chat():
    client = Client(
        stumbles=[
            FloodWaitError(request=None, capture=30),
            FloodWaitError(request=None, capture=30),
        ]
    )

    with pytest.raises(telegram.FloodStop):
        await telegram.fetch(client, "chat", notify=lambda text: None, sleep=Waits())

    assert client.walks == 2


# ── список чатов ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chats_lists_groups_and_channels_only():
    client = Client(
        dialogs=[
            dialog(-100777, "Пример"),
            dialog(-100888, "Канал", is_group=False, is_channel=True),
            SimpleNamespace(id=5, name="Друг", is_group=False, is_channel=False, entity=person()),
        ]
    )

    found = await telegram.chats(client)

    assert [chat["title"] for chat in found] == ["Пример", "Канал"]
    assert found[0] == {
        "id": "-100777",
        "title": "Пример",
        "username": "primer",
        "kind": "group",
        "participants": 6679,
        "archived": False,
    }
    assert found[1]["kind"] == "channel"


@pytest.mark.asyncio
async def test_archive_is_walked_too_and_the_chat_says_so():
    """Архив владелец убрал с глаз сам: осмотреть его надо и сказать, что он архив."""
    client = Client(
        dialogs=[dialog(-100777, "Пример")],
        archived=[dialog(-100888, "Дистанция", archived=True)],
    )

    found = await telegram.chats(client)

    assert [chat["title"] for chat in found] == ["Пример", "Дистанция"]
    assert found[0]["archived"] is False
    assert found[1]["archived"] is True


@pytest.mark.asyncio
async def test_chat_seen_in_both_walks_is_taken_once():
    """Два прохода по диалогам не должны задвоить чат, попавший в обе выдачи."""
    client = Client(
        dialogs=[dialog(-100777, "Пример")],
        archived=[dialog(-100777, "Пример", archived=True)],
    )

    found = await telegram.chats(client)

    assert [chat["id"] for chat in found] == ["-100777"]
    assert found[0]["archived"] is False


@pytest.mark.asyncio
async def test_chat_outside_dialogs_is_fetched_by_its_id():
    """Из чата вышли — в диалогах его нет, а сущность по идентификатору берётся."""
    client = Client()

    assert await telegram.entity_by_id(client, "-100888") is not None


# ── вступление ───────────────────────────────────────────────────────────────


def yes(items):
    return True


def no(items):
    return False


@pytest.mark.asyncio
async def test_join_asks_before_doing_anything():
    client = Client()

    assert await telegram.join(client, ["primer"], no, sleep=Waits()) == []
    assert client.requests == []


@pytest.mark.asyncio
async def test_join_reports_what_happened():
    client = Client()

    done = await telegram.join(client, ["primer"], yes, sleep=Waits())

    assert done == [{"target": "primer", "status": "вступил"}]
    assert joins_of(client) == ["primer"]


@pytest.mark.asyncio
async def test_pending_request_is_not_called_joining():
    """В группе с проверкой на входе читать пока нельзя."""
    client = Client(joins=[InviteRequestSentError(request=None)])
    notes = []

    done = await telegram.join(client, ["closed"], yes, notify=notes.append, sleep=Waits())

    assert done == [{"target": "closed", "status": "заявка подана"}]
    assert "заявка подана" in " ".join(notes)


@pytest.mark.asyncio
async def test_invite_link_goes_through_import():
    client = Client()

    await telegram.join(client, ["https://t.me/+AbCdEf"], yes, sleep=Waits())

    assert [type(r).__name__ for r in client.requests] == ["ImportChatInviteRequest"]
    assert client.requests[0].hash == "AbCdEf"


@pytest.mark.asyncio
async def test_spam_restriction_stops_the_whole_run():
    """Повторять вступления после такого запрета нельзя — станет хуже."""
    client = Client(joins=[PeerFloodError(request=None), None])

    with pytest.raises(telegram.SpamRestricted):
        await telegram.join(client, ["первый", "второй"], yes, sleep=Waits())

    assert joins_of(client) == ["первый"]


@pytest.mark.asyncio
async def test_flood_wait_on_join_stops_the_run():
    client = Client(joins=[FloodWaitError(request=None, capture=300), None])

    with pytest.raises(telegram.FloodStop):
        await telegram.join(client, ["первый", "второй"], yes, sleep=Waits())

    assert joins_of(client) == ["первый"]


@pytest.mark.asyncio
async def test_join_pauses_between_targets():
    client = Client()
    waits = Waits()

    await telegram.join(client, ["один", "два", "три"], yes, sleep=waits)

    assert waits == [telegram.JOIN_PAUSE, telegram.JOIN_PAUSE]


@pytest.mark.asyncio
async def test_join_takes_no_more_than_the_run_limit():
    client = Client()
    notes = []

    done = await telegram.join(
        client,
        [f"чат{number}" for number in range(telegram.JOIN_LIMIT_PER_RUN + 3)],
        yes,
        notify=notes.append,
        sleep=Waits(),
    )

    assert len(done) == telegram.JOIN_LIMIT_PER_RUN
    assert len(joins_of(client)) == telegram.JOIN_LIMIT_PER_RUN
    assert str(telegram.JOIN_LIMIT_PER_RUN) in " ".join(notes)


@pytest.mark.asyncio
async def test_join_stops_at_the_account_cap():
    client = Client(dialogs=[dialog(-100000 - number) for number in range(telegram.CHAT_CAP)])

    with pytest.raises(telegram.ChatCapReached) as failure:
        await telegram.join(client, ["ещё"], yes, sleep=Waits())

    assert str(telegram.CHAT_CAP) in str(failure.value)
    assert joins_of(client) == []


@pytest.mark.asyncio
async def test_join_warns_near_the_account_cap():
    occupied = telegram.CHAT_CAP - telegram.CAP_WARN
    client = Client(dialogs=[dialog(-100000 - number) for number in range(occupied)])
    notes = []

    await telegram.join(client, ["ещё"], yes, notify=notes.append, sleep=Waits())

    assert "потолк" in " ".join(notes).lower()


@pytest.mark.asyncio
async def test_server_side_cap_is_named_plainly():
    client = Client(joins=[ChannelsTooMuchError(request=None)])

    with pytest.raises(telegram.ChatCapReached):
        await telegram.join(client, ["ещё"], yes, sleep=Waits())


# ── выход и папки ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_leave_asks_before_doing_anything():
    client = Client()

    assert await telegram.leave(client, ["-100777"], no) == []
    assert client.requests == []


@pytest.mark.asyncio
async def test_leave_reports_what_it_left():
    client = Client()

    assert await telegram.leave(client, ["-100777"], yes) == ["-100777"]
    assert [type(r).__name__ for r in client.requests] == ["LeaveChannelRequest"]


@pytest.mark.asyncio
async def test_folder_ids_are_strings():
    folder = SimpleNamespace(
        id=8,
        title="Работа",
        pinned_peers=[PeerChannel(1000000001)],
        include_peers=[PeerChannel(1000000002)],
    )
    client = Client(filters=[SimpleNamespace(), folder])

    assert await telegram.folder_ids(client, 1) == {"-1001000000001", "-1001000000002"}


@pytest.mark.asyncio
async def test_missing_folder_is_named_plainly():
    client = Client(filters=[])

    with pytest.raises(ValueError):
        await telegram.folder_ids(client, 3)


@pytest.mark.asyncio
async def test_folders_are_numbered_and_counted():
    """Без номеров папок нечего передать в `dump --folder` и в `folder --to`."""
    client = Client(filters=[
        SimpleNamespace(),  # папка «Все чаты» названия не имеет и номера не занимает
        dialog_filter("Personal", contacts=True, non_contacts=True),
        dialog_filter("Для рассылок", included=[PeerChannel(number) for number in range(7)]),
    ])

    found = await telegram.folders(client)

    assert found[0] == {
        "number": 1,
        "title": "Personal",
        "chats": 0,
        "auto": ["contacts", "non_contacts"],
    }
    assert found[1] == {"number": 2, "title": "Для рассылок", "chats": 7, "auto": []}


@pytest.mark.asyncio
async def test_pinned_chats_of_a_folder_are_counted_too():
    client = Client(filters=[dialog_filter(
        "Папка проб", pinned=[PeerChannel(1000000001)], included=[PeerChannel(1000000002)]
    )])

    assert (await telegram.folders(client))[0]["chats"] == 2


@pytest.mark.asyncio
async def test_folder_number_is_the_one_folder_ids_takes():
    """Номер из списка ведёт в ту же папку: иначе выгрузка пойдёт не по той."""
    client = Client(filters=[
        SimpleNamespace(),
        dialog_filter("Personal"),
        dialog_filter("Папка проб", included=[PeerChannel(1000000001)]),
    ])

    found = await telegram.folders(client)
    number = next(item["number"] for item in found if item["title"] == "Папка проб")

    assert await telegram.folder_ids(client, number) == {"-1001000000001"}


@pytest.mark.asyncio
async def test_folder_title_is_readable_in_the_new_format():
    """Телеграм отдаёт название объектом с разметкой: печатать надо текст."""
    client = Client(filters=[dialog_filter(TextWithEntities(text="Папка проб", entities=[]))])

    assert (await telegram.folders(client))[0]["title"] == "Папка проб"


@pytest.mark.asyncio
async def test_add_to_folder_skips_what_is_already_there():
    folder = SimpleNamespace(
        id=8,
        title="Работа",
        pinned_peers=[],
        include_peers=[InputPeerChannel(channel_id=111, access_hash=0)],
    )
    client = Client(filters=[folder])

    total = await telegram.add_to_folder(client, 1, ["-100111", "-100222"])

    assert total == 2


# ── поиск ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_public_marks_where_we_are_already_in():
    client = Client(
        search=[
            SimpleNamespace(title="Python KZ", username="sample_group", participants_count=9000,
                            broadcast=False, left=False),
            SimpleNamespace(title="Канал", username="kanal", participants_count=100,
                            broadcast=True, left=True),
        ]
    )

    found = await telegram.search_public(client, "python")

    assert found[0] == {
        "title": "Python KZ",
        "username": "sample_group",
        "participants": 9000,
        "kind": "group",
        "joined": True,
    }
    assert found[1]["kind"] == "channel"
    assert found[1]["joined"] is False


# ── чат по юзернейму ─────────────────────────────────────────────────────────


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


@pytest.mark.asyncio
async def test_public_group_is_opened_by_its_username():
    """Незнакомую группу читают по имени: ни выгрузки, ни подписки для этого нет."""
    client = Client(entity=public_group())

    chat_id, entity = await telegram.resolve_username(client, "sample_chat")

    assert chat_id == "-1000000000888"
    assert entity.title == "Образец Чат"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    ["@sample_chat", "https://t.me/sample_chat", "t.me/sample_chat/", "sample_chat"],
)
async def test_username_is_taken_as_the_owner_brought_it(target):
    """Владелец приносит ссылку, а не голое имя: разбирать её должен инструмент."""
    client = Client(entity=public_group())

    chat_id, _ = await telegram.resolve_username(client, target)

    assert chat_id == "-1000000000888"


@pytest.mark.asyncio
async def test_invite_link_is_sent_to_the_join_command():
    """Ссылка-приглашение юзернеймом не является: по ней вступают, а не читают."""
    with pytest.raises(ValueError) as failure:
        await telegram.resolve_username(Client(), "https://t.me/+AbCdEf")

    assert "join --invite" in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal", [UsernameNotOccupiedError(request=None), UsernameInvalidError(request=None),
                ValueError("No user has \"sample_chat\" as username")]
)
async def test_username_nobody_took_is_named_as_missing(refusal):
    client = Client(entity=refusal)

    with pytest.raises(telegram.UsernameGone) as failure:
        await telegram.resolve_username(client, "sample_chat")

    assert "sample_chat" in str(failure.value)


@pytest.mark.asyncio
async def test_closed_chat_is_told_apart_from_a_missing_one():
    """«Не нашёл» и «не пускают» — разные беды, и лечатся они по-разному."""
    client = Client(entity=ChannelPrivateError(request=None))

    with pytest.raises(telegram.ChatClosed) as failure:
        await telegram.resolve_username(client, "closed_example")

    assert "closed_example" in str(failure.value)
    assert "join" in str(failure.value)


@pytest.mark.asyncio
async def test_username_of_a_person_is_not_a_chat():
    client = Client(entity=User(id=55, username="yerzhan", first_name="Ержан"))

    with pytest.raises(telegram.NotAChat) as failure:
        await telegram.resolve_username(client, "yerzhan")

    assert "человек" in str(failure.value)


@pytest.mark.asyncio
async def test_username_of_a_bot_is_not_a_chat_either():
    client = Client(entity=User(id=7, username="wa_bot", first_name="Бот", bot=True))

    with pytest.raises(telegram.NotAChat) as failure:
        await telegram.resolve_username(client, "wa_bot")

    assert "бот" in str(failure.value)


@pytest.mark.asyncio
async def test_closed_chat_is_named_when_the_dump_starts():
    """Отказ может прийти и позже: сущность взялась, а сообщения не отдают."""
    client = Client(message_error=ChannelPrivateError(request=None))

    with pytest.raises(telegram.ChatClosed):
        await telegram.fetch(client, "chat")


# ── что за чат ───────────────────────────────────────────────────────────────


def full_of(**fields):
    """Полные сведения о чате так, как их отдаёт Телеграм."""
    chats = fields.pop("chats", [])
    fields.setdefault("linked_chat_id", None)
    return SimpleNamespace(full_chat=SimpleNamespace(**fields), chats=chats)


@pytest.mark.asyncio
async def test_about_collects_what_is_needed_before_paying():
    """Первое, что спрашивают о незнакомом чате: кто там, о чём и пустят ли."""
    client = Client(
        full=full_of(about="Чат о разработке в Казахстане", pinned_msg_id=12,
                     participants_count=4200, online_count=53, slowmode_seconds=30),
        pinned="Реклама по субботам и только с админом",
    )

    card = await telegram.about(client, public_group())

    assert card["title"] == "Образец Чат"
    assert card["kind"] == "group"
    assert card["participants"] == 4200
    assert card["online"] == 53
    assert card["description"] == "Чат о разработке в Казахстане"
    assert card["pinned"] == "Реклама по субботам и только с админом"
    assert card["replies"] == telegram.REPLIES_THREAD
    assert card["write"] == telegram.WRITE_JOIN
    assert card["joined"] is False
    assert card["slowmode"] == 30
    assert card["linked"] is None


@pytest.mark.asyncio
async def test_about_points_to_the_discussion_group_of_a_channel():
    """В канал не пишут: откликаются в его группе обсуждения, и знать её надо заранее."""
    client = Client(
        full=full_of(
            about="Заказы на разработку", linked_chat_id=1000000001,
            chats=[SimpleNamespace(id=1000000001, title="Второй чат", username="vtoroi_chat")],
        )
    )

    card = await telegram.about(client, public_channel())

    assert card["kind"] == "channel"
    assert card["replies"] == telegram.REPLIES_COMMENTS
    assert card["linked"]["title"] == "Второй чат"
    assert card["write"] == telegram.WRITE_ADMINS


@pytest.mark.asyncio
async def test_about_survives_a_chat_without_a_pinned_message():
    client = Client(full=full_of(about=None, pinned_msg_id=None))

    card = await telegram.about(client, public_group())

    assert card["pinned"] is None
    assert card["description"] is None


@pytest.mark.asyncio
async def test_about_does_not_fall_when_the_pinned_message_is_hidden():
    """Закреп может не отдаться: сведения о чате из-за этого пропадать не должны."""
    client = Client(full=full_of(pinned_msg_id=12), pinned=ValueError("нет доступа"))

    card = await telegram.about(client, public_group())

    assert card["pinned"] is None
    assert card["participants"] == 4200


@pytest.mark.asyncio
async def test_about_of_a_closed_chat_says_so():
    class Refusing(Client):
        async def __call__(self, request):
            raise ChannelPrivateError(request=None)

    with pytest.raises(telegram.ChatClosed):
        await telegram.about(Refusing(), public_group())


def test_chat_where_writing_is_banned_for_everyone():
    banned = ChatBannedRights(until_date=None, send_messages=True)

    assert telegram.write_access(public_group(default_banned_rights=banned)) == telegram.WRITE_BANNED


def test_chat_we_are_already_in_is_open_for_writing():
    assert telegram.write_access(public_group(left=False)) == telegram.WRITE_OPEN


def test_own_channel_is_open_for_writing():
    assert telegram.write_access(public_channel(left=False, creator=True)) == telegram.WRITE_OPEN


# ── окно ─────────────────────────────────────────────────────────────────────


def test_window_by_days():
    start, end = telegram.window(days=14)

    assert end is None
    assert timedelta(days=13, hours=23) < datetime.now(timezone.utc) - start < timedelta(days=14, hours=1)


def test_window_by_dates():
    start, end = telegram.window(since="2026-08-01", until="2026-08-14")

    assert start == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 14, tzinfo=timezone.utc)


def test_window_keeps_a_given_timezone():
    """Свой часовой пояс в дате — не повод объявлять её московской."""
    start, _ = telegram.window(since="2026-08-01T00:00+05:00")

    assert start.utcoffset() == timedelta(hours=5)


def test_window_without_bounds():
    assert telegram.window() == (None, None)


@pytest.mark.asyncio
async def test_обход_подаёт_голос_каждые_две_тысячи_сообщений():
    """Долгий обход без отметок неотличим от повисшего, и его убьют руками.

    В проекте это уже случилось: живой прогон сочли зависшим на десятой минуте
    молчания. Поэтому отметка обязана появляться по ходу, а не по завершении.
    """
    сообщений = 4500
    client = Client(messages=[message(mid=i) for i in range(сообщений, 0, -1)])
    сказанное = []
    await telegram.fetch(client, object(), notify=сказанное.append)

    отметки = [строка for строка in сказанное if "прочитано" in строка]
    assert len(отметки) == сообщений // telegram.WALK_MARK
    assert "прочитано 2000" in отметки[0]


@pytest.mark.asyncio
async def test_короткий_чат_голоса_не_подаёт():
    """Отметка каждые сто сообщений превратила бы лог в шум."""
    client = Client(messages=[message(mid=i) for i in range(50, 0, -1)])
    сказанное = []
    await telegram.fetch(client, object(), notify=сказанное.append)
    assert not [строка for строка in сказанное if "прочитано" in строка]


# --- отправка -------------------------------------------------------------


class Отправщик:
    """Подменный клиент: запоминает, кому и что ушло."""

    def __init__(self, беда=None):
        self.ушло = []
        self.беда = беда

    async def send_message(self, кому, текст):
        if self.беда:
            raise self.беда
        self.ушло.append((кому, текст))
        return SimpleNamespace(id=777)


@нужна_разведка
@pytest.mark.asyncio
async def test_a_message_goes_to_the_username_not_to_a_number():
    # Числовые идентификаторы едут после каждой выгрузки, и подстановка
    # вчерашних однажды увела шесть чужих чатов в стороннюю модель.
    app = Отправщик()

    await telegram.say(app, "user_one", "Брат че делаешь")

    assert app.ушло == [("user_one", "Брат че делаешь")]


@pytest.mark.asyncio
async def test_an_empty_message_is_refused_before_the_network():
    # Пустое сообщение — это ошибка вызова, а не повод дёргать Телеграм.
    app = Отправщик()

    with pytest.raises(ValueError):
        await telegram.say(app, "user_one", "   ")

    assert app.ушло == []


@нужна_разведка
@pytest.mark.asyncio
async def test_a_flood_wait_is_named_not_swallowed():
    app = Отправщик(беда=FloodWaitError(request=None))

    with pytest.raises(telegram.FloodStop):
        await telegram.say(app, "user_one", "привет")


@нужна_разведка
@pytest.mark.asyncio
async def test_a_spam_block_is_named_too():
    app = Отправщик(беда=PeerFloodError(request=None))

    with pytest.raises(telegram.SpamRestricted):
        await telegram.say(app, "user_one", "привет")


class Папка:
    """Подменная папка Телеграма."""

    def __init__(self):
        self.id = 7
        self.title = "Маркетологи"
        self.include_peers = []
        self.pinned_peers = []


class Складыватель:
    def __init__(self):
        self.папка = Папка()
        self.запросы = []
        self.сущности = {}

    async def __call__(self, request):
        self.запросы.append(request)
        return SimpleNamespace(filters=[self.папка])

    async def get_entity(self, кто):
        return self.сущности[кто]

    async def get_input_entity(self, кто):
        return кто


@pytest.mark.asyncio
async def test_a_chat_is_added_to_a_folder_by_username(monkeypatch):
    # Числовые идентификаторы едут после каждой выгрузки. Юзернейм не меняется,
    # и добавлять по нему надёжнее, чем по вчерашнему числу.
    app = Складыватель()
    app.сущности["devstorm"] = SimpleNamespace(id=1)
    monkeypatch.setattr(telegram, "get_peer_id", lambda e: getattr(e, "id", e))

    сколько = await telegram.add_to_folder(app, 1, ["devstorm"])

    assert сколько == 1
    assert app.папка.include_peers == [SimpleNamespace(id=1)]


@pytest.mark.asyncio
async def test_a_chat_already_in_the_folder_is_not_doubled(monkeypatch):
    app = Складыватель()
    уже = SimpleNamespace(id=1)
    app.папка.include_peers = [уже]
    app.сущности["devstorm"] = уже
    monkeypatch.setattr(telegram, "get_peer_id", lambda e: getattr(e, "id", e))

    сколько = await telegram.add_to_folder(app, 1, ["devstorm"])

    assert сколько == 1


@pytest.mark.asyncio
async def test_a_numeric_addressee_is_refused_before_the_network():
    # Правило «адресат — юзернейм, а не число» жило только в докстринге: число
    # принималось молча, а подстановка вчерашних id однажды увела шесть чужих
    # чатов с личной перепиской в стороннюю модель. Отправку отменить нельзя.
    app = Отправщик()

    with pytest.raises(ValueError, match="юзернейм"):
        await telegram.say(app, "-1001000000001", "привет")
    with pytest.raises(ValueError, match="юзернейм"):
        await telegram.say(app, 123456789, "привет")

    assert app.ушло == []


@нужна_разведка
@pytest.mark.asyncio
async def test_a_revoked_session_in_say_is_named_not_dumped():
    # Весь модуль обещает «трассировки Telethon до команд не доходят» — say
    # отдавал сырой стек вместо SessionGone с подсказкой про login.
    app = Отправщик(беда=UnauthorizedError(request=None, message="сессия отозвана"))

    with pytest.raises(telegram.SessionGone):
        await telegram.say(app, "user_one", "привет")


@pytest.mark.asyncio
async def test_a_pinned_chat_counts_as_already_in_the_folder():
    # known собирался только из include_peers: закреплённый чат добавлялся в
    # include вторым экземпляром, и Телеграм получал фильтр с дублем.
    folder = SimpleNamespace(
        id=8,
        title="Работа",
        pinned_peers=[InputPeerChannel(channel_id=111, access_hash=0)],
        include_peers=[],
    )
    client = Client(filters=[folder])

    await telegram.add_to_folder(client, 1, ["-100111"])

    assert folder.include_peers == []


# --- чёрный список у двери -------------------------------------------------
#
# 08.09.2026 два пинга ушли ботам, лежавшим в чёрном списке с 02.09: проверку
# перед рассылкой делал разовый скрипт, и он искал не тот ключ. Правило
# «перед любым исходящим сверяться со списком» жило в памяти агента. Теперь
# оно живёт в единственной двери наружу: `say` сверяется сам.


@pytest.fixture(autouse=True)
def память_круга(tmp_path, monkeypatch):
    """Чёрный список и журнал отправок — в tmp: `say` читает первый и пишет
    во второй, и ни один тест не должен дотянуться до боевых файлов."""
    if discovery_common is not None:
        monkeypatch.setattr(discovery_common, "BLACKLIST", tmp_path / "blacklist.json")
        monkeypatch.setattr(discovery_common, "OUTREACH", tmp_path / "outreach.jsonl")
    return tmp_path


@pytest.fixture
def чёрный_список(память_круга):
    return память_круга / "blacklist.json"


@pytest.fixture
def журнал(память_круга):
    return память_круга / "outreach.jsonl"


@нужна_разведка
@pytest.mark.asyncio
async def test_a_known_bot_is_refused_before_the_network(чёрный_список):
    чёрный_список.write_text('{"боты": [{"username": "baIt_bot", "подтверждён": "2026-09-02"}]}',
                             encoding="utf-8")
    app = Отправщик()

    with pytest.raises(telegram.KnownBot, match="baIt_bot"):
        await telegram.say(app, "@BAIT_BOT", "привет")

    assert app.ушло == []


@нужна_разведка
@pytest.mark.asyncio
async def test_a_blacklist_of_another_shape_blocks_sending(чёрный_список):
    # Сломанная проверка хуже отсутствующей: пока список не читается, не идёт никто.
    чёрный_список.write_text('{"contacts": [{"contact": "baIt_bot"}]}', encoding="utf-8")
    app = Отправщик()

    with pytest.raises(LookupError):
        await telegram.say(app, "user_two", "привет")

    assert app.ушло == []


@нужна_разведка
@pytest.mark.asyncio
async def test_no_blacklist_yet_does_not_block_sending(чёрный_список):
    app = Отправщик()

    await telegram.say(app, "user_two", "привет")

    assert app.ушло == [("user_two", "привет")]


# --- журнал отправок пишет сама дверь --------------------------------------
#
# Строка журнала ставилась руками после каждой отправки. Это маркер намерения,
# а не следа: забытая строка означает второе письмо тому же человеку.


@нужна_разведка
@pytest.mark.asyncio
async def test_a_sent_message_is_journaled_by_say_itself(журнал):
    from tgservice.discovery import orders
    app = Отправщик()

    await telegram.say(app, "@user_two", "привет", про="сайт для салона")

    assert orders.писали_из() == ["user_two"]
    (строка,) = orders.common.read_lines(журнал)
    assert строка["text"] == "привет"
    assert строка["about"] == "сайт для салона"
    assert строка["message_id"] == "777"
    assert строка["when"].endswith("+00:00")


@нужна_разведка
@pytest.mark.asyncio
async def test_a_refused_message_leaves_no_journal_line(журнал):
    app = Отправщик(беда=PeerFloodError(request=None))

    with pytest.raises(telegram.SpamRestricted):
        await telegram.say(app, "user_two", "привет")

    assert not журнал.exists()
