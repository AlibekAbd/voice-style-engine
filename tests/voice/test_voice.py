from tgservice.voice import batch, collect, common, select


def msg(mid, day, minute, out, text, **extra):
    return {
        "mid": mid,
        "date": f"2026-08-{day:02d}T{minute}",
        "out": out,
        "text": text,
        **extra,
    }


CHAT = {"id": "-100500", "name": "Санжар Брат", "username": "sanzhar_b", "kind": "dm", "msgs": 6}
WHO = {"name": "Санжар Брат", "username": "sanzhar_b"}


# --- отсев мусора --------------------------------------------------------


def test_short_reply_survives_the_filter():
    assert select.junk(msg(1, 8, "10:00", True, "ок")) is None
    assert select.junk(msg(2, 8, "10:00", True, "гоу")) is None


def test_service_forward_and_bare_link_are_junk():
    assert select.junk(msg(1, 8, "10:00", False, "", service=True)) == "служебное"
    assert select.junk(msg(2, 8, "10:00", False, "смотри", fwd=True)) == "пересланное"
    assert select.junk(msg(3, 8, "10:00", True, "https://t.me/x")) == "ссылка без текста"


def test_link_with_own_words_survives():
    assert select.junk(msg(1, 8, "10:00", True, "глянь https://t.me/x")) is None


def test_own_voice_note_is_dropped_but_incoming_one_stays():
    # Своё голосовое — манера говорить, а не писать: в разбор не идёт.
    assert select.junk(msg(1, 8, "10:00", True, "", media="голосовое")) == "своё без текста"
    # Входящее остаётся пометкой: по нему видно, на что владелец отвечает.
    assert select.junk(msg(2, 8, "10:00", False, "", media="голосовое")) is None
    assert select.junk(msg(3, 8, "10:00", True, "вот смета", media="файл")) is None


def test_incoming_placeholder_is_visible_in_the_batch():
    rows = [msg(1, 8, "10:00", False, "", media="голосовое")]

    assert "10:00 Санжар Брат: [голосовое]" in batch._render(CHAT, rows, 1, 1)


def test_voice_note_length_is_shown_when_it_is_known():
    assert batch._mark(msg(1, 8, "10:00", False, "", media="голосовое", secs=107)) == "[голосовое 1:47]"
    assert batch._mark(msg(2, 8, "10:00", False, "", media="голосовое", secs=2)) == "[голосовое 0:02]"
    assert batch._mark(msg(3, 8, "10:00", False, "", media="голосовое")) == "[голосовое]"


def test_length_is_asked_once_and_not_asked_again():
    rows = [
        msg(1, 8, "10:00", False, "", media="голосовое"),
        msg(2, 8, "10:01", False, "", media="голосовое", secs=12),
        msg(3, 8, "10:02", True, "", media="голосовое"),
        msg(4, 8, "10:03", False, "", media="фото"),
    ]

    assert collect._voiced(rows) == [1]


# --- отбор живых ---------------------------------------------------------


def test_one_sided_chat_is_not_alive():
    only_mine = [msg(i, 8, "10:00", True, "привет") for i in range(5)]

    assert select.alive(WHO,only_mine) == "собеседник молчит"


def test_three_messages_is_not_enough():
    few = [msg(1, 8, "10:00", True, "привет"), msg(2, 8, "10:01", False, "здарова")]

    assert select.alive(WHO,few) == "мало сообщений"


def test_short_and_odd_names_are_not_a_reason_to_drop_a_chat():
    rows = [
        msg(1, 8, "10:00", True, "салам"),
        msg(2, 8, "10:01", False, "оу"),
        msg(3, 8, "10:02", True, "как сам"),
        msg(4, 8, "10:03", False, "норм"),
    ]

    assert select.alive({"name": "x5", "username": None}, rows) is None
    assert select.alive({"name": ".", "username": "nobody_x"}, rows) is None
    assert select.alive({"name": "Санжар Брат", "username": None}, rows) is None


def test_interlocutor_without_name_and_username_is_not_a_person():
    rows = [
        msg(1, 8, "10:00", True, "/start"),
        msg(2, 8, "10:00", False, "access not configured"),
        msg(3, 8, "10:02", True, "работаешь?"),
        msg(4, 8, "10:03", False, "Работаю!"),
    ]

    assert select.alive({"name": "", "username": None}, rows) is not None


def test_two_sided_chat_with_four_messages_is_alive():
    rows = [
        msg(1, 8, "10:00", True, "привет"),
        msg(2, 8, "10:01", False, "здарова"),
        msg(3, 8, "10:02", True, "как сам"),
        msg(4, 8, "10:03", False, "норм"),
    ]

    assert select.alive(WHO,rows) is None


# --- нарезка -------------------------------------------------------------


def test_batch_boundary_never_cuts_a_day_in_half():
    rows = [msg(i, day, "10:00", i % 2 == 0, "текст " * 200) for day in (8, 9, 10) for i in range(20)]

    parts = batch._split(CHAT, rows, limit=2000)

    assert len(parts) > 1
    for part in parts:
        days = {row["date"][:10] for row in part}
        assert days
    starts = [part[0]["date"][:10] for part in parts]
    assert len(set(starts)) == len(starts)


def test_empty_chat_gives_no_batches_instead_of_crashing():
    assert batch._split(CHAT, []) == []


def test_small_chat_stays_one_batch():
    rows = [msg(1, 8, "10:00", True, "привет"), msg(2, 8, "10:01", False, "здарова")]

    assert len(batch._split(CHAT, rows)) == 1


def test_rendered_batch_keeps_the_text_word_for_word():
    rows = [msg(1, 8, "10:00", True, "Салам, кара, сколько по срокам?")]

    text = batch._render(CHAT, rows, 1, 1)

    assert "10:00 я: Салам, кара, сколько по срокам?" in text
    assert "2026-08-08" not in text.split("## 2026-08-08")[1]
    assert "## 2026-08-08" in text


def test_incoming_message_is_signed_with_the_name():
    rows = [msg(1, 8, "10:00", False, "перезвони")]

    assert "10:00 Санжар Брат: перезвони" in batch._render(CHAT, rows, 1, 1)


def test_short_chats_ride_together_and_long_ones_alone():
    short = [
        ({"id": str(n), "name": f"Собеседник {n}", "username": None, "kind": "dm", "msgs": 2},
         [msg(1, 8, "10:00", True, "привет"), msg(2, 8, "10:01", False, "здарова")],
         200)
        for n in range(5)
    ]

    packs = batch._pack(short)

    assert len(packs) == 1
    assert len(packs[0]) == 5


def test_a_pack_does_not_hold_more_people_than_allowed():
    many = [
        ({"id": str(n), "name": f"Собеседник {n}", "username": None, "kind": "dm", "msgs": 2},
         [msg(1, 8, "10:00", True, "привет")], 10)
        for n in range(batch.TAIL_PEOPLE * 2 + 3)
    ]

    packs = batch._pack(many)

    assert len(packs) == 3
    assert all(len(pack) <= batch.TAIL_PEOPLE for pack in packs)


def test_mixed_batch_warns_that_these_are_different_people():
    pack = [
        ({"id": "1", "name": "Жанна", "username": "zhanna_m", "kind": "dm", "msgs": 2},
         [msg(1, 8, "10:00", False, "привет"), msg(2, 8, "10:01", True, "здарова")]),
        ({"id": "2", "name": "Ринат", "username": None, "kind": "dm", "msgs": 2},
         [msg(3, 9, "11:00", False, "салам"), msg(4, 9, "11:01", True, "уа алейкум")]),
    ]

    text = batch._mixed(pack)

    assert "несколько РАЗНЫХ собеседников" in text
    assert "по каждому человеку отдельно" in text
    assert "## Собеседник 1 из 2: Жанна (@zhanna_m)" in text
    assert "## Собеседник 2 из 2: Ринат" in text
    assert "10:00 Жанна: привет" in text
    assert "11:00 Ринат: салам" in text


def test_finished_chats_are_recovered_from_the_dump(tmp_path, monkeypatch):
    # Потерянное состояние не повод перечитывать год: правда лежит в выгрузке.
    monkeypatch.setattr(collect, "RAW", tmp_path)
    common.write_json(
        tmp_path / "77.json",
        {"meta": {"id": "77", "name": "Санжар Брат", "msgs": 12, "since": "2025-08-27T00:00"},
         "messages": []},
    )
    common.write_json(
        tmp_path / "88.json",
        {"meta": {"id": "88", "name": "Из другого окна", "msgs": 5, "since": "2025-01-01T00:00"},
         "messages": []},
    )

    state = collect._recover({"since": "2025-08-27T00:00", "chats": {}})

    assert state["chats"]["77"]["status"] == "done"
    assert "88" not in state["chats"]


def test_window_is_taken_from_the_dump_not_from_today(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "RAW", tmp_path)
    for number in range(3):
        common.write_json(
            tmp_path / f"{number}.json",
            {"meta": {"id": str(number), "name": "кто-то", "msgs": 1, "since": "2025-08-27T00:00"},
             "messages": []},
        )
    common.write_json(
        tmp_path / "odd.json",
        {"meta": {"id": "9", "name": "перечитанный", "msgs": 1, "since": "2025-08-29T00:00"},
         "messages": []},
    )

    assert collect.dump_window() == "2025-08-27T00:00"


def test_quarter_marks_the_place_on_the_year():
    assert batch._quarter("2025-09-24T14:28") == "2025-Q3"
    assert batch._quarter("2026-01-02T09:00") == "2026-Q1"
    assert batch._quarter("2026-12-31T23:59") == "2026-Q4"


# --- отложенная выборка --------------------------------------------------


LIVE = [
    {
        "id": str(number),
        "name": f"Собеседник {number}",
        "username": None,
        "kind": "dm",
        "msgs": 4,
        "messages": [
            msg(1, 8, "10:00", True, "привет"),
            msg(2, 8, "10:01", False, "здарова, по проекту что решили?"),
            msg(3, 8, "10:02", True, "завтра скину смету"),
            msg(4, 8, "10:03", False, "принял"),
        ],
    }
    for number in range(9)
]


def test_holdout_takes_one_exchange_per_person():
    picked = select.holdout(LIVE, size=5)

    assert len(picked) == 5
    assert len({row["chat_id"] for row in picked}) == 5


def test_holdout_carries_the_incoming_and_the_reply():
    item = select.holdout(LIVE, size=1)[0]

    assert item["incoming"][0]["text"] == "здарова, по проекту что решили?"
    assert item["reply"][0]["text"] == "завтра скину смету"
    assert set(item["mids"]) == {2, 3}


def test_holdout_messages_do_not_reach_the_batches(tmp_path, monkeypatch):
    # Номера сообщений глобальны только у личек: у каждой супергруппы своя
    # нумерация с единицы. Плоское множество mid вычёркивало из чужих чатов
    # их собственные сообщения молча — счётчики батча считались уже по
    # урезанному списку и расхождения не показывали.
    monkeypatch.setattr(batch, "BATCHES", tmp_path)
    picked = select.holdout(LIVE, size=3)
    держаное = {(item["chat_id"], mid) for item in picked for mid in item["mids"]}
    выбранные = {item["chat_id"] for item in picked}

    made = batch.batches(LIVE, держаное)

    по_чату = {card["id"]: card["msgs"] for row in made for card in row["chats"]}
    for chat in LIVE:
        if chat["id"] in выбранные:
            assert по_чату[chat["id"]] == 2   # обмен вынут из своего чата
        else:
            assert по_чату[chat["id"]] == 4   # чужой холдаут этот чат не трогает


def test_holdout_is_the_same_on_a_repeat_run():
    assert select.holdout(LIVE, size=6) == select.holdout(LIVE, size=6)


def test_series_of_messages_in_a_row_is_taken_whole():
    rows = [
        msg(1, 8, "10:00", False, "слушай, тут такое дело"),
        msg(2, 8, "10:00", False, "надо лендинг переделать"),
        msg(3, 8, "10:01", True, "понял"),
        msg(4, 8, "10:01", True, "сколько времени есть?"),
    ]

    found = select._exchanges(rows)

    assert len(found) == 1
    assert [row["mid"] for row in found[0]["incoming"]] == [1, 2]
    assert [row["mid"] for row in found[0]["reply"]] == [3, 4]


def test_a_pause_inside_the_incoming_block_does_not_break_it():
    # Написали в 09:27, дописали в 10:39, владелец открыл мессенджер вечером и
    # прочитал всё разом. Для него это один раздражитель.
    rows = [
        msg(1, 8, "09:27", False, "ок"),
        msg(2, 8, "10:39", False, "слушай, тут такое дело"),
        msg(3, 8, "19:40", True, "давай"),
    ]

    found = select._exchanges(rows)

    assert len(found) == 1
    assert [row["mid"] for row in found[0]["incoming"]] == [1, 2]


def test_an_answer_ten_hours_later_is_still_an_answer():
    rows = [
        msg(1, 8, "09:00", False, "брат, сможешь завтра подъехать?"),
        msg(2, 8, "19:30", True, "прости, только увидел"),
    ]

    found = select._exchanges(rows)

    assert len(found) == 1
    assert found[0]["answered_in_minutes"] == 630


def test_owner_writing_again_hours_later_is_a_new_turn():
    rows = [
        msg(1, 8, "10:00", False, "как там смета?"),
        msg(2, 8, "10:01", True, "щас гляну"),
        msg(3, 8, "14:20", True, "кстати, по договору вопрос"),
    ]

    found = select._exchanges(rows)

    assert [row["mid"] for row in found[0]["reply"]] == [2]


def test_answer_a_week_later_is_not_an_exchange():
    rows = [
        msg(1, 8, "10:00", False, "привет, как там смета?"),
        msg(2, 20, "10:00", True, "прости, замотался"),
    ]

    assert select._exchanges(rows) == []


# --- оценка длины --------------------------------------------------------


def test_cyrillic_costs_more_tokens_than_latin():
    assert common.tokens("привет" * 100) > common.tokens("privet" * 100)


def test_a_digit_costs_a_whole_token():
    # Замер: штамп «2025-09-24 14:28 » — семнадцать токенов, слова «Вы как» — три.
    assert common.tokens("2025-09-24 14:28 ") > common.tokens("Вы как")


def test_token_estimate_grows_with_the_text():
    assert common.tokens("а" * 1000) > common.tokens("а" * 100)


# --- выгрузка: окно, перезапуск, сироты ----------------------------------


import asyncio
import json

import pytest

from tgservice.voice import assemble
from tgservice import telegram as tg


@pytest.fixture
def выгрузка(tmp_path, monkeypatch):
    """Пути выгрузки уводятся в свою папку, сеть подменяется заглушкой."""
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(collect, "RAW", raw)
    monkeypatch.setattr(collect, "VOICE", tmp_path)
    monkeypatch.setattr(collect, "FETCH_STATE", tmp_path / "fetch_state.json")
    monkeypatch.setattr(collect, "RAW_INDEX", tmp_path / "raw_index.json")

    class Приложение:
        async def disconnect(self):
            pass

    async def queued(notify=None):
        return Приложение()

    async def targets(app):
        return []

    async def обход(app, targets, state, since, notify=collect.say):
        pass

    monkeypatch.setattr(tg, "queued", queued)
    monkeypatch.setattr(collect, "_targets", targets)
    monkeypatch.setattr(collect, "_collect", обход)
    return tmp_path


def _дамп(tmp_path, chat_id, since):
    (tmp_path / "raw" / f"{chat_id}.json").write_text(json.dumps({
        "meta": {"id": chat_id, "name": f"Чат {chat_id}", "username": None,
                 "kind": "dm", "msgs": 4, "mine": 2, "theirs": 2,
                 "first": "2026-08-08T10:00", "last": "2026-08-08T10:03",
                 "since": since},
        "messages": [msg(1, 8, "10:00", True, "привет"),
                     msg(2, 8, "10:01", False, "здарова, по проекту что решили?"),
                     msg(3, 8, "10:02", True, "завтра скину смету"),
                     msg(4, 8, "10:03", False, "принял")],
    }, ensure_ascii=False), encoding="utf-8")


def test_restart_actually_refetches_even_on_the_same_day(выгрузка):
    # --restart в день исходной выгрузки был холостым: окно совпадало до
    # минуты, _recover помечал все файлы «восстановлено по выгрузке», и в
    # Телеграм не уходило ни одного запроса — при справке «выгрузить всё заново».
    since = collect.since_of(collect.DAYS).isoformat()[:16]
    _дамп(выгрузка, "111", since)

    asyncio.run(collect.fetch(restart=True))

    state = json.loads((выгрузка / "fetch_state.json").read_text(encoding="utf-8"))
    assert state["chats"] == {}


def test_without_restart_the_dump_is_recovered(выгрузка):
    # Обратная сторона: обычный запуск восстанавливает готовое по выгрузке,
    # потерянный файл состояния не повод перечитывать год.
    since = collect.since_of(collect.DAYS).isoformat()[:16]
    _дамп(выгрузка, "111", since)

    asyncio.run(collect.fetch())

    state = json.loads((выгрузка / "fetch_state.json").read_text(encoding="utf-8"))
    assert state["chats"]["111"]["at"] == "восстановлено по выгрузке"


def test_orphan_dumps_are_named_and_quarantined(выгрузка):
    # Владелец убрал группу из groups_taken.json — а её старый raw-файл лежал
    # и штатно уезжал в батчи: решение об исключении молча не исполнялось.
    since = collect.since_of(collect.DAYS).isoformat()[:16]
    _дамп(выгрузка, "111", since)
    _дамп(выгрузка, "222", since)

    asyncio.run(collect.fetch())  # восстановит оба
    (выгрузка / "fetch_state.json").write_text(json.dumps(
        {"since": since, "days": collect.DAYS,
         "chats": {"111": {"title": "Чат 111", "status": "done", "msgs": 4, "at": "x"}}},
        ensure_ascii=False), encoding="utf-8")
    состояние = json.loads((выгрузка / "fetch_state.json").read_text(encoding="utf-8"))

    лишние = collect.orphans(состояние)

    assert [path.name for path in лишние] == ["222.json"]


def test_a_voice_only_incoming_still_reaches_the_holdout():
    # Входящее из одного голосового имело ноль знаков и никогда не проходило
    # порог: экзамен не проверял манеру ответа на голосовое, хотя весь
    # конвейер бережёт голосовые-заглушки как раздражители.
    rows = [
        msg(1, 8, "10:00", False, "", media="голосовое", secs=107),
        msg(2, 8, "10:01", True, "понял, наберу вечером"),
    ]

    assert len(select._exchanges(rows)) == 1


def test_a_deleted_voice_message_is_not_asked_again():
    # Удалённое сообщение длительность не отдаст никогда. Без пометки его
    # переспрашивали каждым прогоном, и «голосовых без длительности» не
    # доходило до нуля.
    rows = [msg(1, 8, "10:00", False, "", media="голосовое")]

    collect._lay(rows, ids=[1], found={})

    assert collect._voiced(rows) == []
    assert rows[0]["secs"] is None


# --- сборка: выборка не тасуется, негабарит виден -------------------------


@pytest.fixture
def сборка(tmp_path, monkeypatch):
    """Все пути сборки — в свою папку."""
    raw = tmp_path / "raw"
    batches = tmp_path / "batches"
    raw.mkdir()
    batches.mkdir()
    monkeypatch.setattr(select, "RAW", raw)
    monkeypatch.setattr(assemble, "RAW", raw)
    monkeypatch.setattr(batch, "BATCHES", batches)
    monkeypatch.setattr(assemble, "HOLDOUT", tmp_path / "holdout.json")
    monkeypatch.setattr(assemble, "HOLDOUT_TEXT", tmp_path / "holdout.md")
    monkeypatch.setattr(assemble, "LIVE_INDEX", tmp_path / "live_index.json")
    monkeypatch.setattr(assemble, "BATCH_INDEX", tmp_path / "batches_index.json")
    monkeypatch.setattr(assemble, "FETCH_STATE", tmp_path / "fetch_state.json")
    monkeypatch.setattr(assemble, "MEASURE", tmp_path / "measure.json")
    monkeypatch.setattr(assemble, "MEASURE_TEXT", tmp_path / "measure.md")
    return tmp_path


def test_a_rebuild_does_not_reshuffle_the_holdout(сборка):
    # Повторный build после довыгрузки выбирал другие обмены, а старые уже
    # видели агенты в прежних батчах — заголовок «модель их не видела»
    # становился ложью, и проверка по третьей спеке ничего не доказывала.
    _дамп(сборка, "111", "2025-08-27T00:00")

    assemble.build()
    первый = json.loads((сборка / "holdout.json").read_text(encoding="utf-8"))
    _дамп(сборка, "222", "2025-08-27T00:00")
    assemble.build()
    второй = json.loads((сборка / "holdout.json").read_text(encoding="utf-8"))

    assert второй["items"] == первый["items"]


def test_an_oversized_batch_is_named_by_the_measure(сборка):
    # День тяжелее потолка молча давал негабаритную часть, а негабарит — это
    # обрезанный вывод модели на разборе, причину которого искали бы руками.
    (сборка / "live_index.json").write_text(json.dumps({
        "live": [{"id": "1", "name": "X", "msgs": 4, "mine": 2, "theirs": 2,
                  "raw": 4, "first": "2026-08-08T10:00", "last": "2026-08-08T10:03"}],
        "skipped": [], "junk": {}}, ensure_ascii=False), encoding="utf-8")
    (сборка / "batches_index.json").write_text(json.dumps({
        "batches": [
            {"name": "x-01.md", "tokens": 70_000, "kind": "solo",
             "chats": [{"id": "1", "name": "X", "msgs": 4}]},
            {"name": "x-02.md", "tokens": 20_000, "kind": "solo",
             "chats": [{"id": "1", "name": "X", "msgs": 4}]},
        ]}, ensure_ascii=False), encoding="utf-8")
    (сборка / "holdout.json").write_text(json.dumps(
        {"count": 0, "items": []}, ensure_ascii=False), encoding="utf-8")
    (сборка / "fetch_state.json").write_text(json.dumps(
        {"since": "2025-08-27T00:00", "chats": {}}, ensure_ascii=False), encoding="utf-8")

    numbers = assemble.measure()

    assert numbers["oversized"] == ["x-01.md"]
    assert "x-01.md" in (сборка / "measure.md").read_text(encoding="utf-8")


def test_a_whole_chat_carries_no_part_disclaimer():
    # «Часть 1 из 1» в шапке включала в разборе дисклеймер «судить об
    # отношениях целиком нельзя» — ложный для цельной переписки.
    rows = [msg(1, 8, "10:00", True, "привет")]

    assert "Часть 1 из 1" not in batch._render(CHAT, rows, 1, 1)
    assert "Часть 2 из 3" in batch._render(CHAT, rows, 2, 3)
