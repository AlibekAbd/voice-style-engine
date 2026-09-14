from tgservice.voice import verify

BATCH = """# Переписка: Санжар Брат (@sanzhar_b)

Собеседник: Санжар Брат (@sanzhar_b), id 123
Часть 1 из 1

## 2025-09-24

10:00 я: Салем брат
10:01 Санжар Брат: Уа алейкум ассалам
10:02 я: Вань, привет !
    Как твои дела?
    Пишу по делу.
10:05 я: ок

## 2025-10-01

09:00 я: Салем брат
09:30 я: Брат че не спишь
"""


def batch():
    return verify.Batch(BATCH)


# --- разбор выгрузки -----------------------------------------------------


def test_multiline_message_is_indexed_whole():
    # Абзацы после первой строки — часть того же сообщения, а не мусор.
    spots = batch().owner_said("Пишу по делу.")

    assert spots == [("2025-09-24", "10:02")]


def test_trailing_spaces_do_not_break_the_match():
    assert verify.Batch("## 2025-09-24\n10:00 я: привет  \n").owner_said("привет")


# --- разбор ответа агента ------------------------------------------------


def test_quotes_are_read_in_both_formats():
    answer = "\n".join([
        "2025-09-24 10:00 я: Салем брат",
        "- «ок» (2025-09-24 10:05)",
        "2025-09-24 я: Салем брат",
    ])

    assert len(verify.quotes(answer)) == 3


# --- приговор цитате -----------------------------------------------------


def test_exact_quote_passes():
    assert verify.judge(("2025-09-24", "10:00", "я", "Салем брат"), batch()) is None


def test_quote_without_time_passes_by_day():
    assert verify.judge(("2025-09-24", None, "я", "Салем брат"), batch()) is None


def test_wrong_day_with_a_single_source_is_repaired():
    note = verify.judge(("2025-12-31", None, "я", "ок"), batch())

    assert note["verdict"] == verify.FIXED
    assert note["should"] == ("2025-09-24", "10:05")


def test_wrong_day_with_several_sources_goes_to_the_agent():
    note = verify.judge(("2025-12-31", None, "я", "Салем брат"), batch())

    assert note["verdict"] == verify.ASK
    assert len(note["seen"]) == 2


def test_words_of_the_other_side_are_caught():
    note = verify.judge(("2025-09-24", None, "я", "Уа алейкум ассалам"), batch())

    assert note["verdict"] == verify.FOREIGN


def test_invented_quote_is_caught():
    note = verify.judge(("2025-09-24", None, "я", "Такого владелец не писал"), batch())

    assert note["verdict"] == verify.INVENTED


def test_only_the_case_differs_is_its_own_verdict():
    # Заглавная буква — не выдумка и не дословность: своя пометка.
    note = verify.judge(("2025-09-24", None, "я", "САЛЕМ БРАТ"), batch())

    assert note["verdict"] == verify.CASED


def test_a_single_changed_letter_is_caught():
    # «в Астану» → «в Астана»: ровно так модель и подменяет цитату.
    note = verify.judge(("2025-09-24", None, "я", "Салем брад"), batch())

    assert note["verdict"] == verify.INVENTED


def test_a_truncated_quote_is_an_error():
    # «Брат че не спишь» процитировано как «че не спишь»: слова настоящие, но
    # сообщение длиннее. Формат батча к обрезке не подталкивает — значит ошибка.
    note = verify.judge(("2025-10-01", None, "я", "че не спишь", "3. Раскладка"), batch())

    assert note["verdict"] == verify.CUT


def test_a_fragment_under_a_block_is_a_turn_of_phrase_not_a_cut():
    # Под блоком приводится оборот, а не сообщение целиком: кусок там уместен.
    assert verify.judge(("2025-10-01", None, "я", "че не спишь", "2. Блоки"), batch()) is None


def test_a_truncated_quote_on_a_wrong_day_is_still_placed():
    assert verify.judge(("2025-09-24", None, "я", "че не спишь"), batch()) is not None


# --- полнота -------------------------------------------------------------


def test_coverage_names_what_was_dropped():
    answer = "2025-09-24 10:00 я: Салем брат"

    spread = verify.coverage(answer, batch())

    assert spread["total"] == 5
    assert spread["laid"] == 1
    assert any("че не спишь" in row[2] for row in spread["missing"])


def test_full_layout_gives_full_coverage():
    answer = "\n".join([
        "2025-09-24 10:00 я: Салем брат",
        "2025-09-24 10:02 я: Вань, привет !",
        "    Как твои дела?",
        "    Пишу по делу.",
        "2025-09-24 10:05 я: ок",
        "2025-10-01 09:00 я: Салем брат",
        "2025-10-01 09:30 я: Брат че не спишь",
    ])

    assert verify.coverage(answer, batch())["share"] == 1.0


# --- решение по ответу целиком -------------------------------------------


def test_clean_answer_is_accepted():
    answer = "\n".join([
        "2025-09-24 10:00 я: Салем брат",
        "2025-09-24 10:02 я: Вань, привет !",
        "    Как твои дела?",
        "    Пишу по делу.",
        "2025-09-24 10:05 я: ок",
        "2025-10-01 09:00 я: Салем брат",
        "2025-10-01 09:30 я: Брат че не спишь",
    ])

    assert verify.check(answer, batch())["verdict"] == "принято"


def test_invented_quotes_send_the_answer_back():
    answer = "2025-09-24 10:00 я: Ничего такого не было\n" * 3

    assert verify.check(answer, batch())["verdict"] == "перегнать"


def test_repair_fixes_only_what_is_unambiguous():
    answer = "2025-12-31 я: ок\n2025-12-31 я: Салем брат"

    mended, fixes = verify.repair(answer, batch())

    assert "2025-09-24 я: ок" in mended
    assert "2025-12-31 я: Салем брат" in mended  # два вхождения — не угадываем
    assert len(fixes) == 1


def test_patch_replaces_only_named_lines():
    answer = "\n".join([
        "2025-09-24 10:00 я: Салем брат",
        "2025-09-24 10:01 я: Уа алейкум ассалам",
        "2025-09-24 10:05 я: ок",
    ])
    patch = "\n".join([
        "БЫЛО: 2025-09-24 10:01 я: Уа алейкум ассалам",
        "СТАЛО: 2025-09-24 10:01 Санжар Брат: Уа алейкум ассалам",
    ])

    mended, note = verify.apply_patch(answer, patch)

    assert "10:01 Санжар Брат: Уа алейкум ассалам" in mended
    assert "10:00 я: Салем брат" in mended  # не названное не тронуто
    assert "10:05 я: ок" in mended
    assert note["внесено"] == 1


def test_patch_can_delete_a_line():
    answer = "2025-09-24 10:00 я: Салем брат\n2025-09-24 10:00 я: выдумка"
    patch = "БЫЛО: 2025-09-24 10:00 я: выдумка\nСТАЛО: —"

    mended, _ = verify.apply_patch(answer, patch)

    assert "выдумка" not in mended
    assert "Салем брат" in mended


def test_patch_can_split_one_line_into_several():
    answer = "2025-09-24 10:00 я: БРАТЫМ\nАВТОМАТЫМ"
    patch = "\n".join([
        "БЫЛО: 2025-09-24 10:00 я: БРАТЫМ",
        "СТАЛО: 2025-09-24 10:00 я: БРАТЫМ",
        "СТАЛО: 2025-09-24 10:00 я: АВТОМАТЫМ",
    ])

    mended, _ = verify.apply_patch(answer, patch)

    assert mended.count("10:00 я:") == 2


def test_patch_reports_what_it_could_not_place():
    mended, note = verify.apply_patch("строка", "БЫЛО: такой строки нет\nСТАЛО: неважно")

    assert note["не найдено"] == ["такой строки нет"]
    assert mended == "строка"


def test_letter_names_the_missing_messages():
    answer = "2025-09-24 10:00 я: Салем брат"

    letter = verify.ask_again(verify.check(answer, batch()))

    assert "не попали" in letter
    assert "че не спишь" in letter


# --- склейка через чужую реплику -----------------------------------------
#
# Задание требует цитировать многострочное сообщение целиком. Надавив на это
# правило, получили обратное: агент начал склеивать соседние сообщения, в том
# числе чужие. На пробном батче так вышло одиннадцать цитат, и все они
# назывались «выдумано» — переспрос просил найти текст, которого и не было.


def test_glued_own_messages_are_called_glued():
    quote = ("2025-10-01", None, "я", "Салем брат\nБрат че не спишь")

    assert verify.judge(quote, batch())["verdict"] == verify.MERGED


def test_a_quote_glued_through_someone_elses_line_is_not_an_invention():
    # Слова в выгрузке есть — все до одного. Ошибка в том, что они склеены
    # через реплику собеседника и выданы за речь владельца.
    quote = ("2025-09-24", None, "я", "Салем брат\nУа алейкум ассалам")

    assert verify.judge(quote, batch())["verdict"] == verify.GLUED


def test_the_foreign_part_of_a_glued_quote_is_named():
    # Переспросу надо сказать, какая именно строка чужая, иначе агент не
    # поймёт, что править.
    quote = ("2025-09-24", None, "я", "Салем брат\nУа алейкум ассалам")

    found = verify.judge(quote, batch())

    assert "Уа алейкум ассалам" in found["чужое"]


def test_a_quote_of_words_nobody_said_stays_an_invention():
    quote = ("2025-09-24", None, "я", "Салем брат\nэтого никто не писал")

    assert verify.judge(quote, batch())["verdict"] == verify.INVENTED


def test_a_glue_is_recognised_even_when_a_part_sits_inside_a_longer_message():
    # Часть склейки может быть строкой многострочного сообщения, а не отдельным
    # сообщением целиком. Двенадцать таких на пробном батче звались выдумкой.
    quote = ("2025-09-24", None, "я", "Как твои дела?\nок")

    assert verify.judge(quote, batch())["verdict"] == verify.MERGED


def test_a_glue_of_a_line_inside_a_message_with_someone_elses_words():
    quote = ("2025-09-24", None, "я", "Пишу по делу.\nУа алейкум ассалам")

    assert verify.judge(quote, batch())["verdict"] == verify.GLUED


def test_the_dump_keeps_the_order_of_who_said_what():
    # Порядок нужен для дробления и задержки: без него не отличить серию
    # сообщений владельца от ответа на входящее.
    rows = batch().rows

    assert [who for _, _, who, _ in rows][:3] == ["я", "Санжар Брат", "я"]
    assert rows[0][:2] == ("2025-09-24", "10:00")


# --- вёрстка сборного батча ----------------------------------------------

СБОРНЫЙ = """# Переписка: 2 собеседника

Собеседников в файле: 2. Сообщений всего: 3.

---

## Собеседник 1 из 2: Алмас (@almas_t)

id 100000001, сообщений 2 (мои 2, его 0), период 2025-12-15 — 2025-12-15

### 2025-12-15

05:49 я: Аа понял понял
05:49 я: Ок

---

## Собеседник 2 из 2: Светлана (@svetlana_r)

id 100000002, сообщений 1 (мои 1, его 0), период 2026-05-25 — 2026-05-25

### 2026-05-25

05:37 я: Пон
"""


def test_section_headers_are_not_glued_to_the_last_message():
    # Продолжение сообщения отбито отступом — так его пишет сам рендер батча.
    # Всё, что стоит от левого края, это вёрстка: разделитель, заголовок
    # собеседника, строка с id. Приклеенная к сообщению, она делает его
    # ненаходимым, и верная цитата агента объявляется выдумкой.
    said = verify.Batch(СБОРНЫЙ).owner_said("Ок")

    assert said == [("2025-12-15", "05:49")]


def test_a_correct_quote_of_a_section_tail_is_not_an_invention():
    quote = ("2025-12-15", "05:49", "я", "Ок")

    assert verify.judge(quote, verify.Batch(СБОРНЫЙ)) is None


def test_the_last_message_of_a_section_keeps_its_own_text():
    rows = verify.Batch(СБОРНЫЙ).rows

    assert [text for _, _, _, text in rows] == ["Аа понял понял", "Ок", "Пон"]


def test_a_slid_stamp_is_repaired_on_a_multiline_quote():
    # `quotes` склеивает продолжение с отступом, а `repair` разбирал ответ
    # своими руками и судил только первую строку. Многострочная цитата с
    # уехавшей отметкой оставалась непочиненной: по первой строке текст
    # встречается во многих местах, и приговор выходил «переспросить».
    answer = "2025-09-23 10:02 я: Вань, привет !\n    Как твои дела?\n    Пишу по делу.\n"

    mended, fixes = verify.repair(answer, batch())

    assert "2025-09-24 10:02 я: Вань, привет !" in mended
    assert [one["text"] for one in fixes] == ["Вань, привет !\nКак твои дела?\nПишу по делу."]


# --- сверка выше батча ----------------------------------------------------


def test_the_corpus_is_one_dump_of_all_the_batches(tmp_path, monkeypatch):
    # Цитаты свода проверять по одному батчу нельзя: свод сведён из многих, и
    # своего батча у него нет. Выше батча цитаты не проверял никто, и уехавшая
    # отметка доезжала до файла стиля.
    monkeypatch.setattr(verify, "BATCHES", tmp_path)
    (tmp_path / "один.md").write_text("## 2025-09-24\n10:00 я: салем\n", encoding="utf-8")
    (tmp_path / "два.md").write_text("## 2025-10-01\n11:00 я: газ брат\n", encoding="utf-8")

    целое = verify.corpus()

    assert celo(целое) == [("2025-09-24", "10:00"), ("2025-10-01", "11:00")]


def celo(целое):
    return целое.owner_said("салем") + целое.owner_said("газ брат")


def test_a_quote_of_a_summary_is_repaired_against_the_whole_corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "BATCHES", tmp_path)
    (tmp_path / "один.md").write_text("## 2025-09-24\n10:00 я: салем\n", encoding="utf-8")
    (tmp_path / "два.md").write_text("## 2025-10-01\n11:00 я: газ брат\n", encoding="utf-8")
    свод = "Примеры:\nбатч-02.md 2025-10-02 11:00 я: газ брат\n"

    mended, fixes = verify.repair(verify.unsourced(свод), verify.corpus())

    assert "2025-10-01 11:00 я: газ брат" in mended
    assert len(fixes) == 1


# --- находки ревью 07.09.2026 ---------------------------------------------


def test_a_patch_line_in_bold_or_backticks_is_still_a_patch_line():
    # Агент оформляет пометки как хочет: жирным, в бэктиках. STAMPED понимает
    # все виды цитат, а PATCH не понимал — непустое письмо давало «внесено 0,
    # не найдено 0», и это считалось успехом.
    answer = "2025-09-24 10:00 я: Салем брат"
    patch = ("**БЫЛО:** 2025-09-24 10:00 я: Салем брат\n"
             "**СТАЛО:** 2025-09-24 10:00 я: Салам брат\n")

    mended, note = verify.apply_patch(answer, patch)

    assert note["внесено"] == 1
    assert "Салам брат" in mended


def test_the_pairs_of_a_patch_are_countable():
    # «Ни одной пары из непустого письма» — провал, а не успех: mend должен
    # уметь это отличить до вклейки.
    assert verify.patch_pairs("БЫЛО: а\nСТАЛО: б\n") == 1
    assert verify.patch_pairs("поправил всё, смотри выше\n") == 0


def test_a_multiline_replacement_keeps_the_continuation_indented():
    # Вклейка ровняла все строки замены по левому краю — продолжение
    # переставало быть продолжением, и сверка снова видела «обрезано»,
    # покупая переспрос по кругу.
    answer = "# Раскладка\n\n2025-09-24 10:02 я: Вань, привет !"
    patch = ("БЫЛО: 2025-09-24 10:02 я: Вань, привет !\n"
             "СТАЛО: 2025-09-24 10:02 я: Вань, привет !\n"
             "    Как твои дела?\n"
             "    Пишу по делу.\n")

    mended, note = verify.apply_patch(answer, patch)

    assert note["внесено"] == 1
    assert mended.splitlines()[-1].startswith((" ", "\t"))
    assert "обрезано" not in verify.check(mended, batch())["counts"]


def test_a_split_into_separate_messages_is_not_indented():
    # Разнесённая склейка — несколько самостоятельных сообщений, каждое со
    # своей отметкой: их отступать нельзя, иначе они станут продолжениями.
    answer = "# Раскладка\n\n2025-10-01 09:00 я: Салем брат Брат че не спишь"
    patch = ("БЫЛО: 2025-10-01 09:00 я: Салем брат Брат че не спишь\n"
             "СТАЛО: 2025-10-01 09:00 я: Салем брат\n"
             "СТАЛО: 2025-10-01 09:30 я: Брат че не спишь\n")

    mended, note = verify.apply_patch(answer, patch)

    assert "\n2025-10-01 09:30 я: Брат че не спишь" in mended


def test_a_deleted_multiline_quote_takes_its_tail_along():
    # Удаление сносило только первую строку; отступные хвосты приклеивались к
    # предыдущей цитате и рождали ложные «склеено».
    answer = ("# Раскладка\n\n"
              "2025-09-24 10:00 я: Салем брат\n"
              "2025-09-24 10:02 я: Вань, привет !\n"
              "    Как твои дела?\n"
              "    Пишу по делу.\n")
    patch = "БЫЛО: 2025-09-24 10:02 я: Вань, привет !\nСТАЛО: —\n"

    mended, note = verify.apply_patch(answer, patch)

    assert "Как твои дела?" not in mended
    assert "Пишу по делу." not in mended


def test_a_multiline_was_is_matched_as_a_whole():
    # «БЫЛО» бывает многострочным — продолжения без пометки терялись, и пара
    # не находила свою строку.
    answer = ("2025-09-24 10:02 я: Вань, привет !\n"
              "    Как твои дела?\n")
    patch = ("БЫЛО: 2025-09-24 10:02 я: Вань, привет !\n"
             "    Как твои дела?\n"
             "СТАЛО: 2025-09-24 10:02 я: Вань, привет !\n")

    mended, note = verify.apply_patch(answer, patch)

    assert note["внесено"] == 1
    assert "Как твои дела?" not in mended


def test_a_stimulus_line_is_not_glued_to_the_previous_quote():
    # Строка раздражителя «←» скану была неизвестна: её отступное продолжение
    # приклеивалось к предыдущей цитате владельца, и рождался ложный
    # «склеено с чужой» — с платным переспросом по несуществующей ошибке.
    дамп = verify.Batch("## 2025-09-24\n"
                        "10:00 я: Салем брат\n"
                        "10:01 Санжар Брат: Уа алейкум ассалам\n"
                        "    как сам брат\n")
    answer = ("# Раскладка\n\n"
              "2025-09-24 10:00 я: Салем брат\n"
              "← 2025-09-24 10:01 Санжар Брат: Уа алейкум ассалам\n"
              "    как сам брат\n")

    result = verify.check(answer, дамп)

    assert result["counts"] == {}


def test_prose_between_quote_and_indented_list_does_not_extend_the_quote():
    # Отступная строка после прозы — не продолжение цитаты: вложенный
    # markdown-список превращал цитату в многострочную и рождал фантомные
    # находки с платным переспросом.
    answer = ("# Раскладка\n\n"
              "2025-09-24 10:00 я: Салем брат\n"
              "Дальше владелец переходит к делу:\n"
              "    - пример вложенного списка\n")

    result = verify.check(answer, batch())

    assert result["counts"] == {}


def test_a_shortened_name_of_the_other_side_is_not_an_invention():
    # «Санжар» вместо «Санжар Брат» — орфография имени, а не выдумка. Пять таких
    # на батче — и доля брака дотолкает до «перегнать», до второй покупки
    # разбора за сокращённое имя.
    вердикт = verify.judge(("2025-09-24", "10:01", "Санжар", "Уа алейкум ассалам"), batch())

    assert вердикт["verdict"] == verify.ASK


def test_an_example_under_a_block_does_not_count_as_layout_coverage():
    # Оборот под блоком зачитывался раскладке — завышенная полнота гасила
    # нужный переспрос, и пропуски оставались молча.
    answer = ("# Блоки\n\n"
              "### Приветствие\n\n"
              "2025-09-24 10:00 я: Салем брат\n")

    spread = verify.coverage(answer, batch())

    assert spread["laid"] == 0
