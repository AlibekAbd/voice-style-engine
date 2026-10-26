"""Чтение переписки для разбора манеры речи — на подменном клиенте Телеграма.

Сеть не дёргаем: клиент отдаёт заготовленные сообщения новыми вперёд, как
настоящий `iter_messages`, и кидает заказанные отказы.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon.errors import ChannelPrivateError, FloodWaitError

from tgservice import telegram
from tgservice.voice import tg


def moment(day, hour=10):
    return datetime(2026, 8, day, hour, 0, tzinfo=timezone.utc)


def message(mid, day, text="", out=False, **extra):
    fields = dict(id=mid, date=moment(day), text=text, out=out, action=None,
                  fwd_from=None, media=None, reply_to=None, sender=None,
                  post_author=None, sender_id=None)
    fields.update(extra)
    return SimpleNamespace(**fields)


def voice_note(mid, day, seconds, out=False):
    document = SimpleNamespace(attributes=[SimpleNamespace(duration=seconds)])
    return message(mid, day, out=out, media=object(), voice=True, document=document)


class Переписка:
    """Подменный клиент: сообщения новыми вперёд, обход с `offset_id`.

    `отказы` — что кинуть при очередном обходе после `сколько` отданных
    сообщений: так флуд-лимит прерывает чтение посередине.
    """

    def __init__(self, messages=(), отказы=(), ответы=()):
        self.messages = sorted(messages, key=lambda m: -m.id)
        self.отказы = list(отказы)
        self.ответы = list(ответы)
        self.обходы = []
        self.запросы = []

    def iter_messages(self, entity, limit=None, offset_id=0):
        client = self
        client.обходы.append(offset_id)

        async def walk():
            отказ = client.отказы.pop(0) if client.отказы else None
            отдано = 0
            for item in client.messages:
                if offset_id and item.id >= offset_id:
                    continue
                if отказ and отдано == отказ[0]:
                    raise отказ[1]
                отдано += 1
                yield item
            if отказ:
                raise отказ[1]

        return walk()

    async def get_messages(self, entity, ids=None):
        self.запросы.append(list(ids))
        if self.ответы:
            ответ = self.ответы.pop(0)
            if isinstance(ответ, Exception):
                raise ответ
        by_id = {m.id: m for m in self.messages}
        return [by_id.get(i) for i in ids]


class Паузы(list):
    async def __call__(self, seconds):
        self.append(seconds)


# --- speech ----------------------------------------------------------------


def test_speech_keeps_everything_the_style_analysis_reads():
    автор = SimpleNamespace(first_name="Мадина", last_name=None, username="madina_test", title=None)
    app = Переписка([
        message(1, 1, "старое, за окном"),
        message(2, 5, "  привет,\n\nбрат  ", out=True),
        message(3, 6, "переслал", fwd_from=object(), reply_to=SimpleNamespace(reply_to_msg_id=2)),
        message(4, 7, action=object()),
        voice_note(5, 8, 42),
        message(6, 9, "в группе", sender=автор, sender_id=777),
    ])

    rows = asyncio.run(tg.speech(app, object(), since=moment(2), in_group=False))

    by_mid = {row["mid"]: row for row in rows}
    assert sorted(by_mid) == [2, 3, 4, 5, 6]  # за окном — не берётся
    # Текст дословно: отбивка и пробелы — тоже манера.
    assert by_mid[2]["text"] == "  привет,\n\nбрат  "
    assert by_mid[2]["out"] is True
    assert by_mid[2]["date"] == "2026-08-05T10:00"
    assert by_mid[3]["fwd"] is True and by_mid[3]["reply_to"] == 2
    assert by_mid[4]["service"] is True
    assert by_mid[5]["media"] == "голосовое" and by_mid[5]["secs"] == 42
    assert "from" not in by_mid[6]


def test_speech_in_a_group_names_the_author():
    автор = SimpleNamespace(first_name="Мадина", last_name=None, username="madina_test", title=None)
    app = Переписка([message(6, 9, "в группе", sender=автор, sender_id=777)])

    (row,) = asyncio.run(tg.speech(app, object(), in_group=True))

    assert row["from"] == "Мадина"
    assert row["from_username"] == "madina_test"
    assert row["from_id"] == "777"


def test_speech_waits_out_a_flood_and_goes_on_from_where_it_stopped():
    # Годовой обход не начинается заново с каждой паузы: уже прочитанное
    # остаётся, второй обход идёт с номера, на котором встал первый.
    app = Переписка(
        [message(mid, 10 + mid % 5) for mid in range(1, 7)],
        отказы=[(3, FloodWaitError(request=None, capture=30))],
    )
    паузы = Паузы()
    сказанное = []

    rows = asyncio.run(tg.speech(app, object(), notify=сказанное.append, sleep=паузы))

    assert [row["mid"] for row in rows] == [6, 5, 4, 3, 2, 1]
    assert app.обходы == [0, 4]
    assert паузы == [32]
    assert any("флуд-лимит" in строка for строка in сказанное)


def test_speech_puts_off_the_chat_when_telegram_asks_too_long():
    app = Переписка(
        [message(1, 10)],
        отказы=[(0, FloodWaitError(request=None, capture=telegram.FLOOD_LIMIT + 1))],
    )
    паузы = Паузы()

    with pytest.raises(telegram.FloodStop):
        asyncio.run(tg.speech(app, object(), sleep=паузы))

    assert паузы == []


def test_speech_of_a_closed_chat_is_named():
    app = Переписка([message(1, 10)], отказы=[(0, ChannelPrivateError(request=None))])

    with pytest.raises(telegram.ChatClosed):
        asyncio.run(tg.speech(app, object()))


# --- lengths ---------------------------------------------------------------


def test_lengths_asks_by_numbers_in_chunks_and_skips_deleted():
    голосовые = [voice_note(mid, 10, mid % 50 + 1) for mid in range(1, 151)]
    app = Переписка(голосовые)
    номера = list(range(1, 151)) + [999]  # 999 удалено: Телеграм вернёт пустое место

    found = asyncio.run(tg.lengths(app, object(), номера))

    assert [len(chunk) for chunk in app.запросы] == [100, 51]
    assert len(found) == 150
    assert found[1] == 2 and found[49] == 50
    assert 999 not in found


def test_lengths_waits_out_a_short_flood_and_asks_again():
    app = Переписка([voice_note(1, 10, 7)],
                    ответы=[FloodWaitError(request=None, capture=20)])
    паузы = Паузы()

    found = asyncio.run(tg.lengths(app, object(), [1], sleep=паузы))

    assert found == {1: 7}
    assert паузы == [22]
    assert app.запросы == [[1], [1]]


def test_lengths_puts_off_the_chat_when_telegram_asks_too_long():
    app = Переписка([voice_note(1, 10, 7)],
                    ответы=[FloodWaitError(request=None, capture=telegram.FLOOD_LIMIT + 1)])

    with pytest.raises(telegram.FloodStop):
        asyncio.run(tg.lengths(app, object(), [1], sleep=Паузы()))
