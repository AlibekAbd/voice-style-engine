"""Замер файла стиля по отложенной выборке — третья спека.

Агент получает входящее, сведения о собеседнике и файл стиля. Пишет ответ.
Написанное сравнивается с настоящим ответом владельца по признакам, которые
поддаются измерению. Сравнение по смыслу сюда не входит: агент не знает
обстоятельств и ответит по существу иначе. Проверяется звучание.
"""

from tgservice.voice import trial

ОБМЕН = {
    "chat_id": "123",
    "chat": "Санжар Брат",
    "username": "sanzhar_b",
    "kind": "dm",
    "chat_msgs": 4200,
    "context": [{"date": "2026-04-08T06:00", "out": True, "text": "Брат ты где"}],
    "incoming": [
        {"date": "2026-04-08T06:04", "out": False, "text": "В офисе"},
        {"date": "2026-04-08T06:04", "out": False, "text": "Ты подъедешь?"},
    ],
    "reply": [
        {"date": "2026-04-08T06:05", "out": True, "text": "Через полчаса буду"},
        {"date": "2026-04-08T06:05", "out": True, "text": "Жди"},
    ],
    "answered_in_minutes": 1,
}


# --- задание агенту -------------------------------------------------------


def test_the_task_carries_the_incoming_message_and_the_context():
    задание = trial.case(ОБМЕН, "договаривается о времени")

    assert "В офисе" in задание
    assert "Ты подъедешь?" in задание
    assert "Брат ты где" in задание


def test_the_task_carries_the_meaning_but_not_the_relation():
    # Смысл даётся готовым: агент не может знать, что владелец хочет занять
    # налички. Тип отношений НЕ даётся: в работе его никто не подскажет, и
    # узнавание регистра — часть проверяемого.
    задание = trial.case(ОБМЕН, "договаривается о времени")

    assert "договаривается о времени" in задание
    assert "Тип отношений" not in задание


def test_the_real_reply_never_reaches_the_task():
    # Показать ответ владельца — значит отдать ответ на экзамене.
    задание = trial.case(ОБМЕН, "договаривается о времени")

    assert "Через полчаса буду" not in задание
    assert "Жди" not in задание


def test_the_task_says_how_long_they_have_been_talking():
    # «Четыре тысячи сообщений» и «первое письмо» — разная дистанция.
    assert "4200" in trial.case(ОБМЕН, "договаривается о времени")


# --- признаки, которые считает скрипт -------------------------------------


def test_the_number_of_messages_is_counted():
    признаки = trial.measure(["Через полчаса буду", "Жди"])

    assert признаки["сообщений"] == 2


def test_the_words_are_counted_across_all_the_messages():
    признаки = trial.measure(["Через полчаса буду", "Жди"])

    assert признаки["слов"] == 4


def test_an_emoji_is_seen():
    assert trial.measure(["Готово 🔥"])["эмодзи"] is True
    assert trial.measure(["Готово"])["эмодзи"] is False


def test_a_question_at_the_end_is_seen():
    # Хвостовые эмодзи и пробелы отбрасываются перед проверкой — так же, как в
    # `traits`, иначе «Ты где? 🙂» перестанет быть вопросом.
    assert trial.measure(["Ты где? 🙂"])["вопрос в конце"] is True
    assert trial.measure(["Буду через час"])["вопрос в конце"] is False


def test_a_question_inside_does_not_count_as_a_question_at_the_end():
    assert trial.measure(["Ты где? Я выехал"])["вопрос в конце"] is False


def test_the_latin_only_message_is_seen():
    # Механика, а не суждение: ни одной кириллической буквы.
    assert trial.measure(["What about 12 pm?"])["латиницей"] is True
    assert trial.measure(["Буду в 12"])["латиницей"] is False


# --- сравнение с настоящим ответом ----------------------------------------


def test_a_matching_reply_hits_every_counted_feature():
    свои = trial.measure(["Через полчаса буду", "Жди"])

    сошлось = trial.compare(свои, свои)

    assert all(сошлось.values())


def test_the_number_of_messages_is_compared_as_split_or_not():
    # Спека мерит «разбит ли ответ на несколько сообщений подряд», а не точное
    # их число: два против трёх — это одна и та же манера дробить.
    два = trial.measure(["раз", "два"])
    три = trial.measure(["раз", "два", "три"])
    одно = trial.measure(["раз два три"])

    assert trial.compare(два, три)["дробление"] is True
    assert trial.compare(два, одно)["дробление"] is False


def test_the_length_is_compared_by_basket_not_by_exact_count():
    # Медиана длины владельца — три слова, и разница между тремя и четырьмя
    # словами не значит ничего. Корзины те же, что у `traits`.
    три = trial.measure(["раз два три"])
    четыре = trial.measure(["раз два три четыре"])
    длинное = trial.measure([" ".join(["слово"] * 20)])

    assert trial.compare(три, четыре)["длина"] is True
    assert trial.compare(три, длинное)["длина"] is False


# --- проверка на подлог ---------------------------------------------------


def test_a_quote_lifted_from_the_style_file_is_named():
    # Спека: подстановка готового оборота не к месту — провал, даже когда все
    # измеримые признаки сошлись.
    банк = {"Пон Пон", "Все принял брат"}

    assert trial.lifted(["Все принял брат"], банк) == ["Все принял брат"]
    assert trial.lifted(["Через полчаса буду"], банк) == []


def test_the_bank_of_turns_is_read_from_the_style_file():
    файл = "Примеры:\n2025-09-06 09:26 я: Пон Пон\n2025-09-08 04:51 я: Все принял брат\n"

    assert trial.bank(файл) == {"Пон Пон", "Все принял брат"}


# --- естественный предел точности -----------------------------------------


def test_the_owner_is_compared_against_himself_to_get_the_noise_floor():
    # Спека: разброс между двумя настоящими ответами владельца задаёт предел.
    # Точнее самого себя агент быть не может, и без этого числа «сошлось 60%»
    # ничего не значит.
    ответы = [["Через полчаса буду", "Жди"], ["Ща приеду", "Стой там"], ["Ок"]]

    предел = trial.floor(ответы)

    # Два дроблёных ответа из трёх: пара «дроблёный + одиночный» даёт промах.
    assert предел["дробление"] == 1 / 3
    assert предел["эмодзи"] == 1.0  # эмодзи нет нигде — сходится всегда


def test_the_noise_floor_needs_at_least_two_replies():
    assert trial.floor([["Ок"]]) == {}


# --- суд вслепую ----------------------------------------------------------


def test_the_judge_is_not_told_which_reply_is_real():
    # Знай судья автора — засчитал бы настоящему то, чего не засчитал бы
    # второму, и замер померил бы вежливость судьи.
    случай = trial.blind(ОБМЕН, ["Ща приеду"], ["Через полчаса буду", "Жди"])

    assert "владелец" not in случай["текст"].lower()
    assert "агент" not in случай["текст"].lower()
    assert "А" in случай["текст"] and "Б" in случай["текст"]


def test_the_order_of_the_two_replies_is_shuffled_but_remembered():
    случай = trial.blind(ОБМЕН, ["ответ агента"], ["ответ владельца"])

    чей = случай["чей"]
    assert set(чей) == {"А", "Б"}
    assert {чей["А"], чей["Б"]} == {"агент", "владелец"}
    # Тасовка одна и та же при одном и том же чате: замер должен повторяться.
    assert trial.blind(ОБМЕН, ["ответ агента"], ["ответ владельца"])["чей"] == чей


def test_the_verdict_of_the_judge_is_read_back():
    ответ = (
        "А | приветствие: есть | язык: смесь | обращение: есть\n"
        "Б | приветствие: нет | язык: русский | обращение: нет\n"
    )

    разобрано = trial.verdicts(ответ)

    assert разобрано["А"] == {"приветствие": True, "язык": "смесь", "обращение": True}
    assert разобрано["Б"] == {"приветствие": False, "язык": "русский", "обращение": False}


def test_a_style_quote_without_time_is_seen_by_the_bank():
    # Банк оборотов для проверки подлога не видел цитат без времени: дословная
    # подстановка «ДАЙ ДОСТУП» агентом замера не ловилась.
    assert trial.QUOTE.match("2025-11-07 я: Есть в долг 100к")
