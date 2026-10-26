"""Отправка наружу и её память: чёрный список приманок и журнал отправок.

Подменный клиент Телеграма: сеть не дёргаем, аккаунт не трогаем. Чёрный
список и журнал каждого теста лежат в tmp — до боевых файлов тесты не
дотягиваются, кроме контрольного образца, который их только читает.
"""
import json
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError, PeerFloodError, UnauthorizedError

from tgservice import config, outbox, telegram
from tgservice.files import read_lines


@pytest.fixture(autouse=True)
def память_круга(tmp_path, monkeypatch):
    """Чёрный список и журнал отправок — в tmp: `say` читает первый и пишет
    во второй, и ни один тест не должен дотянуться до боевых файлов."""
    monkeypatch.setattr(config, "BLACKLIST", tmp_path / "blacklist.json")
    monkeypatch.setattr(config, "OUTREACH", tmp_path / "outreach.jsonl")
    return tmp_path


@pytest.fixture
def чёрный_список(память_круга):
    return память_круга / "blacklist.json"


@pytest.fixture
def журнал(память_круга):
    return память_круга / "outreach.jsonl"


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

    async def send_file(self, кому, файл, caption=""):
        if self.беда:
            raise self.беда
        self.ушло.append((кому, caption, str(файл)))
        return SimpleNamespace(id=778)


@pytest.mark.asyncio
async def test_a_message_goes_to_the_username_not_to_a_number():
    # Числовые идентификаторы едут после каждой выгрузки, и подстановка
    # вчерашних однажды увела шесть чужих чатов в стороннюю модель.
    app = Отправщик()

    await outbox.say(app, "user_one", "Брат че делаешь")

    assert app.ушло == [("user_one", "Брат че делаешь")]


@pytest.mark.asyncio
async def test_a_file_goes_with_its_caption_through_the_same_door(tmp_path):
    # Файл уходит той же дверью, что и текст: подпись — это текст сообщения,
    # и сверка с чёрным списком и журнал для него те же.
    app = Отправщик()
    файл = tmp_path / "транскрипт.txt"
    файл.write_text("Айгерим: добрый день", encoding="utf-8")

    await outbox.say(app, "user_one", "Транскрипт звонка", файл=файл)

    assert app.ушло == [("user_one", "Транскрипт звонка", str(файл))]


@pytest.mark.asyncio
async def test_a_file_may_go_without_a_caption(tmp_path):
    # Пустой текст — ошибка только когда отправлять больше нечего. Файл сам
    # по себе сообщение, подпись к нему необязательна.
    app = Отправщик()
    файл = tmp_path / "программа.pdf"
    файл.write_bytes(b"%PDF-1.4")

    await outbox.say(app, "user_one", "", файл=файл)

    assert app.ушло == [("user_one", "", str(файл))]


@pytest.mark.asyncio
async def test_a_missing_file_is_refused_before_the_network(tmp_path):
    app = Отправщик()

    with pytest.raises(FileNotFoundError):
        await outbox.say(app, "user_one", "Транскрипт", файл=tmp_path / "нет.txt")

    assert app.ушло == []


@pytest.mark.asyncio
async def test_an_empty_message_is_refused_before_the_network():
    # Пустое сообщение — это ошибка вызова, а не повод дёргать Телеграм.
    app = Отправщик()

    with pytest.raises(ValueError):
        await outbox.say(app, "user_one", "   ")

    assert app.ушло == []


@pytest.mark.asyncio
async def test_a_flood_wait_is_named_not_swallowed():
    app = Отправщик(беда=FloodWaitError(request=None))

    with pytest.raises(telegram.FloodStop):
        await outbox.say(app, "user_one", "привет")


@pytest.mark.asyncio
async def test_a_spam_block_is_named_too():
    app = Отправщик(беда=PeerFloodError(request=None))

    with pytest.raises(telegram.SpamRestricted):
        await outbox.say(app, "user_one", "привет")


@pytest.mark.asyncio
async def test_a_numeric_addressee_is_refused_before_the_network():
    # Правило «адресат — юзернейм, а не число» жило только в докстринге: число
    # принималось молча, а подстановка вчерашних id однажды увела шесть чужих
    # чатов с личной перепиской в стороннюю модель. Отправку отменить нельзя.
    app = Отправщик()

    with pytest.raises(ValueError, match="юзернейм"):
        await outbox.say(app, "-1001000000001", "привет")
    with pytest.raises(ValueError, match="юзернейм"):
        await outbox.say(app, 123456789, "привет")

    assert app.ушло == []


@pytest.mark.asyncio
async def test_a_revoked_session_in_say_is_named_not_dumped():
    # Весь модуль обещает «трассировки Telethon до команд не доходят» — say
    # отдавал сырой стек вместо SessionGone с подсказкой про login.
    app = Отправщик(беда=UnauthorizedError(request=None, message="сессия отозвана"))

    with pytest.raises(telegram.SessionGone):
        await outbox.say(app, "user_one", "привет")


# --- чёрный список у двери -------------------------------------------------
#
# 08.09.2026 два пинга ушли ботам, лежавшим в чёрном списке с 02.09: проверку
# перед рассылкой делал разовый скрипт, и он искал не тот ключ. Правило
# «перед любым исходящим сверяться со списком» жило в памяти агента. Теперь
# оно живёт в единственной двери наружу: `say` сверяется сам.


@pytest.mark.asyncio
async def test_a_known_bot_is_refused_before_the_network(чёрный_список):
    чёрный_список.write_text('{"боты": [{"username": "baIt_bot", "подтверждён": "2026-09-02"}]}',
                             encoding="utf-8")
    app = Отправщик()

    with pytest.raises(outbox.KnownBot, match="baIt_bot"):
        await outbox.say(app, "@BAIT_BOT", "привет")

    assert app.ушло == []


@pytest.mark.asyncio
async def test_a_blacklist_of_another_shape_blocks_sending(чёрный_список):
    # Сломанная проверка хуже отсутствующей: пока список не читается, не идёт никто.
    чёрный_список.write_text('{"contacts": [{"contact": "baIt_bot"}]}', encoding="utf-8")
    app = Отправщик()

    with pytest.raises(LookupError):
        await outbox.say(app, "user_two", "привет")

    assert app.ушло == []


@pytest.mark.asyncio
async def test_no_blacklist_yet_does_not_block_sending(чёрный_список):
    app = Отправщик()

    await outbox.say(app, "user_two", "привет")

    assert app.ушло == [("user_two", "привет")]


# --- журнал отправок пишет сама дверь --------------------------------------
#
# Строка журнала ставилась руками после каждой отправки. Это маркер намерения,
# а не следа: забытая строка означает второе письмо тому же человеку.


@pytest.mark.asyncio
async def test_a_sent_message_is_journaled_by_say_itself(журнал):
    app = Отправщик()

    await outbox.say(app, "@user_two", "привет", про="сайт для салона")

    assert outbox.писали_из() == ["user_two"]
    (строка,) = read_lines(журнал)
    assert строка["text"] == "привет"
    assert строка["about"] == "сайт для салона"
    assert строка["message_id"] == "777"
    assert строка["when"].endswith("+00:00")


@pytest.mark.asyncio
async def test_a_refused_message_leaves_no_journal_line(журнал):
    app = Отправщик(беда=PeerFloodError(request=None))

    with pytest.raises(telegram.SpamRestricted):
        await outbox.say(app, "user_two", "привет")

    assert not журнал.exists()


# --- адресат без юзернейма: чат, найденный по названию -----------------


@pytest.mark.asyncio
async def test_say_sends_to_a_chat_found_by_title_and_journals_it(журнал):
    группа = SimpleNamespace(username=None)
    чат = telegram.Чат(id=-5000000000001, title="Sample Studio KZ | Proj", entity=группа)
    app = Отправщик()

    await outbox.say(app, чат, "Марат, привет!", про="Proj: инструкция и договор")

    assert app.ушло == [(группа, "Марат, привет!")]
    (строка,) = read_lines(журнал)
    assert строка["to"] == "чат:Sample Studio KZ | Proj"
    assert строка["about"] == "Proj: инструкция и договор"


@pytest.mark.asyncio
async def test_a_chat_found_by_title_is_still_checked_against_the_blacklist(чёрный_список):
    чёрный_список.write_text('{"боты": [{"username": "bait_bot", "подтверждён": "2026-09-02"}]}',
                             encoding="utf-8")
    чат = telegram.Чат(id=77, title="Помощник по заказам",
                       entity=SimpleNamespace(username="bait_bot"))
    app = Отправщик()

    with pytest.raises(outbox.KnownBot):
        await outbox.say(app, чат, "привет")

    assert app.ушло == []


# --- чёрный список: одна проверка на всех ---------------------------------
#
# 08.09.2026 два пинга ушли ботам, которые лежали в чёрном списке с 02.09:
# разовый скрипт парсил файл руками и искал ключ «contacts» с полем «contact»,
# а в файле «боты» и «username». Проверка вернула «никого нет», и ей поверили.
# Сломанная проверка хуже отсутствующей, поэтому парсить руками больше нечем:
# ниже единственный вход, и он падает, когда файл не той формы.


def test_a_known_bot_is_recognised_whatever_the_case(tmp_path):
    # Ник в Телеграме регистронезависим: Bait_One и bait_one — один аккаунт.
    файл = tmp_path / "blacklist.json"
    файл.write_text(json.dumps({"боты": [{"username": "Bait_One"}]}, ensure_ascii=False), encoding="utf-8")

    assert outbox.бот("bait_one", путь=файл) is True
    assert outbox.бот("@BAIT_ONE", путь=файл) is True
    assert outbox.бот("sample_manager", путь=файл) is False


def test_a_blacklist_of_another_shape_is_a_breakage_not_an_empty_list(tmp_path):
    # Ключ переименовали или подсунули не тот файл — это поломка. Пустой
    # список здесь означал бы «ботов нет», и письмо ушло бы приманке.
    файл = tmp_path / "blacklist.json"
    файл.write_text(json.dumps({"contacts": [{"contact": "Bait_One"}]}, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(LookupError, match="боты"):
        outbox.боты_из(файл)


def test_a_missing_blacklist_is_empty_and_not_a_breakage(tmp_path):
    # Файла ещё нет — это начало работы, а не сломанный формат.
    assert outbox.боты_из(tmp_path / "нет.json") == []
    assert outbox.бот("кто-то", путь=tmp_path / "нет.json") is False


def test_a_confirmed_bot_is_written_into_the_list(tmp_path):
    # Бот опознаётся по ответу, и пометка должна ложиться сразу же —
    # иначе следующий круг напишет ему снова.
    файл = tmp_path / "blacklist.json"
    файл.write_text(json.dumps({"боты": [{"username": "Bait_One"}]}, ensure_ascii=False), encoding="utf-8")

    outbox.пометить_бота("Bait_Four", "автоответ рекламой @sample_leads_bot", "2026-09-08", путь=файл)

    assert outbox.бот("bait_four", путь=файл) is True
    записи = json.loads(файл.read_text(encoding="utf-8"))["боты"]
    свежая = [b for b in записи if b["username"] == "Bait_Four"][0]
    assert свежая["подтверждён"] == "2026-09-08"
    assert "sample_leads_bot" in свежая["чем"]
    assert len(записи) == 2  # прежняя запись цела


def test_a_bot_is_not_written_twice(tmp_path):
    файл = tmp_path / "blacklist.json"
    файл.write_text(json.dumps({"боты": [{"username": "Bait_One"}]}, ensure_ascii=False), encoding="utf-8")

    outbox.пометить_бота("bait_one", "тот же бот другим регистром", "2026-09-08", путь=файл)

    assert len(json.loads(файл.read_text(encoding="utf-8"))["боты"]) == 1


def test_a_record_without_username_is_a_breakage_not_an_empty_list(tmp_path):
    # Переименовали поле внутри записи — ключ «боты» на месте, ников ноль.
    # «Никого» здесь означало бы «ботов нет», и письмо ушло бы приманке.
    файл = tmp_path / "blacklist.json"
    файл.write_text(json.dumps({"боты": [{"ник": "Bait_One"}]}, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(LookupError, match="username"):
        outbox.боты_из(файл)


def test_marking_into_a_fresh_file_does_not_consult_the_live_list(tmp_path, monkeypatch):
    # Файла по указанному пути ещё нет — проверка «уже есть?» должна идти по
    # нему же, а не по боевому списку: иначе бот, известный боевому, в новый
    # файл не запишется никогда.
    боевой = tmp_path / "боевой.json"
    боевой.write_text(json.dumps({"боты": [{"username": "Bait_One"}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "BLACKLIST", боевой)
    новый = tmp_path / "новый.json"

    outbox.пометить_бота("Bait_One", "автоответ", "2026-09-08", путь=новый)

    assert [b["username"] for b in json.loads(новый.read_text(encoding="utf-8"))["боты"]] == ["Bait_One"]


# --- журнал отправок: форма не та — поломка ------------------------------


def test_an_outreach_journal_of_another_shape_is_a_breakage(tmp_path):
    файл = tmp_path / "outreach.jsonl"
    файл.write_text('{"кому": "athyse", "when": "2026-09-03"}\n', encoding="utf-8")

    with pytest.raises(LookupError, match="to"):
        outbox.писали_из(файл)


def test_a_missing_outreach_journal_is_empty(tmp_path):
    assert outbox.писали_из(tmp_path / "нет.jsonl") == []


def test_a_send_is_appended_to_the_journal_in_the_shape_the_reader_expects(tmp_path):
    файл = tmp_path / "outreach.jsonl"
    файл.write_text('{"when": "2026-09-02T11:19:15+00:00", "to": "Bait_One", "about": "", "text": "x", "message_id": "391963"}\n',
                    encoding="utf-8")

    outbox.записать_отправку("sample_lead", "привет", про="сайт", message_id=394894,
                             когда="2026-09-08T02:43:28+00:00", путь=файл)

    assert outbox.писали_из(файл) == ["Bait_One", "sample_lead"]


# --- отправка по адресу telegram.say ---------------------------------------
#
# Скрипты и инструкции зовут отправку как `telegram.say`.


def test_say_and_known_bot_are_reached_through_telegram_too():
    assert telegram.say is outbox.say
    assert telegram.KnownBot is outbox.KnownBot
