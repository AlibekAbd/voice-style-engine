"""Доступ к Телеграму — ядро: сессия, диалоги, юзернеймы, папки, вступление.

Подменный Telethon: сеть не дёргаем, аккаунт не трогаем.
"""

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
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import (
    InputPeerChannel,
    PeerChannel,
    TextWithEntities,
    User,
)

from tgservice import telegram
from tests.telegram_fakes import (
    Client,
    dialog,
    dialog_filter,
    moment,
    person,
    public_group,
    Waits,
)


def joins_of(client):
    return [
        request.channel
        for request in client.requests
        if isinstance(request, JoinChannelRequest)
    ]


# ── сессия ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoked_session_tells_how_to_log_in_again():
    client = Client(dialog_error=AuthKeyUnregisteredError(request=None))

    with pytest.raises(telegram.SessionGone) as failure:
        await telegram.chats(client)

    assert "login" in str(failure.value)


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


# --- адресат без юзернейма: закрытая группа или личка по названию ----------
#
# 09.10.2026 надо было ответить в закрытую группу «Sample Studio KZ | Proj»:
# юзернейма у неё нет, а `say` по правилу не берёт числа. Чат находится по
# точному названию в момент отправки; похожий не подставляется никогда, а
# число принимается только вместе с названием, когда оба указывают на один чат.


def беседа(chat_id, name, username=None, is_user=False, last="", when=None, members=None):
    return SimpleNamespace(
        id=chat_id,
        name=name,
        is_user=is_user,
        is_group=not is_user,
        is_channel=False,
        archived=False,
        date=when or moment(),
        message=SimpleNamespace(message=last),
        entity=SimpleNamespace(username=username, participants_count=members),
    )


@pytest.mark.asyncio
async def test_a_closed_group_is_found_by_its_exact_title():
    группа = беседа(-5000000000001, "Sample Studio KZ | Proj")
    app = Client(dialogs=[беседа(-1, "Другое"), группа])

    чат = await telegram.чат_по_названию(app, "Sample Studio KZ | Proj")

    assert чат.id == -5000000000001
    assert чат.entity is группа.entity
    assert чат.title == "Sample Studio KZ | Proj"


@pytest.mark.asyncio
async def test_a_private_chat_is_found_by_its_title_too():
    # Писать просят и в личку: имя собеседника в диалогах — то же название.
    app = Client(dialogs=[беседа(42, "Нурлан Брат", is_user=True)])

    чат = await telegram.чат_по_названию(app, "Нурлан Брат")

    assert чат.id == 42


@pytest.mark.asyncio
async def test_case_and_extra_spaces_do_not_matter():
    app = Client(dialogs=[беседа(-7, "Sample Studio KZ | Proj")])

    чат = await telegram.чат_по_названию(app, "  sample studio kz  |  proj ")

    assert чат.id == -7


@pytest.mark.asyncio
async def test_a_chat_in_the_archive_is_found():
    app = Client(archived=[беседа(-8, "Старая группа")])

    чат = await telegram.чат_по_названию(app, "Старая группа")

    assert чат.id == -8


@pytest.mark.asyncio
async def test_a_missing_title_stops_and_names_the_similar_ones():
    # Похожий чат не подставляется: отправку не отменить, а «почти то же
    # название» бывает совсем другой группой.
    app = Client(dialogs=[беседа(-7, "Sample Studio KZ | Proj"), беседа(-9, "Рецепты")])

    with pytest.raises(telegram.ChatNotFound) as беда:
        await telegram.чат_по_названию(app, "Sample Studio KZ")

    assert "Sample Studio KZ | Proj" in str(беда.value)
    assert "Рецепты" not in str(беда.value)


@pytest.mark.asyncio
async def test_two_chats_under_one_title_stop_and_describe_each():
    app = Client(dialogs=[
        беседа(-11, "Команда", members=5, last="созвон в 15"),
        беседа(22, "Команда", is_user=True, last="ок"),
    ])

    with pytest.raises(telegram.ChatAmbiguous) as беда:
        await telegram.чат_по_названию(app, "Команда")

    текст = str(беда.value)
    assert "-11" in текст and "22" in текст
    assert "группа" in текст and "личка" in текст
    assert "созвон в 15" in текст


@pytest.mark.asyncio
async def test_an_id_together_with_the_title_picks_one_of_the_twins():
    app = Client(dialogs=[беседа(-11, "Команда"), беседа(22, "Команда", is_user=True)])

    чат = await telegram.чат_по_названию(app, "Команда", chat_id=22)

    assert чат.id == 22


@pytest.mark.asyncio
async def test_an_id_that_disagrees_with_the_title_is_refused():
    # Ради этого числа и запрещены: вчерашний номер подставили, а под ним
    # оказался другой чат. Название с чужим номером не сходится — отказ.
    app = Client(dialogs=[беседа(-11, "Команда"), беседа(-12, "Чужая группа")])

    with pytest.raises(telegram.ChatNotFound):
        await telegram.чат_по_названию(app, "Команда", chat_id=-12)


@pytest.mark.asyncio
async def test_a_missing_title_with_a_number_is_named_as_missing_not_as_a_mismatch():
    # Номер к названию добавляют, когда чатов несколько. Если же названия нет
    # вовсе, ответ «номер не сходится» уводит искать не там.
    app = Client(dialogs=[беседа(-7, "Sample Studio KZ | Proj")])

    with pytest.raises(telegram.ChatNotFound) as беда:
        await telegram.чат_по_названию(app, "Sample Studio KZ", chat_id=-7)

    assert "Sample Studio KZ | Proj" in str(беда.value)
    assert "разные чаты" not in str(беда.value)
