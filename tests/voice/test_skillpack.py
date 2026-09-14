"""Раскладка собранного файла стиля по файлам скилла.

Скилл живёт двумя руками. Файлы регистров порождаются из свода и переписываются
целиком при каждой пересборке. SKILL.md, правила и журнал замечаний пишутся
руками и скриптом не трогаются никогда — иначе первая же пересборка сотрёт
решения владельца, а измеренное «как есть» перемешается с решением «как надо».
"""

import pytest

from tgservice.voice import skillpack

ФАЙЛ = """# Как владелец переписки пишет сообщения

## Что верно во всех регистрах

- медиана длины реплики: 3 слова

## Четыре регистра и как их узнать

| регистр | кого включает | людей | «вы» | вопросом | эмодзи |
|---|---|---|---|---|---|
| свои | семья, близкие друзья | 55 | 24% | 11% | 3% |
| формальное | исполнители, старшие | 141 | 67% | 20% | 3% |

---

### Регистр: свои

## 1. Кто эти собеседники

Самый близкий круг.

## 2. Блоки речевых действий

### Согласие и подтверждение

Встречается у 40 собеседников из 55.

Определение: подтверждает коротко.

Примеры:
2025-08-27 12:43 я: Ок завтра давайте

---

### Регистр: формальное

## 1. Кто эти собеседники

Исполнители и официальные лица.

## 2. Блоки речевых действий

### Отказ и несогласие

Встречается у 2 собеседников из 141.

Определение: отклоняет предложения.

Примеры:
2026-06-30 06:48 я: Нет, это нам не подходит

---

## Чего в этом файле нет

**Нурлана.** Его манера описана отдельным файлом.
"""

ОТДЕЛЬНЫЕ = {"нурлан.md": """# Как владелец пишет Нурлану

## 1. Кто эти собеседники

Ближайший друг.
"""}


def test_every_register_becomes_its_own_file():
    разделы = skillpack.split(ФАЙЛ)

    assert list(разделы) == ["свои", "формальное"]


def test_the_register_becomes_the_title_of_its_file():
    # В своде регистр стоит третьим уровнем под общей шапкой. Отдельным файлом
    # он сам себе заголовок, иначе главы внутри окажутся выше него.
    разделы = skillpack.split(ФАЙЛ)

    assert разделы["свои"].startswith("# Регистр: свои\n\n## 1.")
    assert "### Регистр:" not in разделы["свои"]


def test_the_shared_head_does_not_leak_into_a_register():
    # Таблица узнавания и общие числа живут в SKILL.md. Попав в каждый регистр,
    # они поедут вчетверо и разойдутся между собой при первой же правке.
    разделы = skillpack.split(ФАЙЛ)

    assert "Что верно во всех регистрах" not in разделы["свои"]
    assert "Чего в этом файле нет" not in разделы["формальное"]


def test_the_quotes_survive_the_split():
    # Цитаты — единственное, чего не восстановить: настоящие слова владельца,
    # сверенные с выгрузкой. Всё прочее пересказывается, они нет.
    разделы = skillpack.split(ФАЙЛ)

    assert "2025-08-27 12:43 я: Ок завтра давайте" in разделы["свои"]
    assert "Встречается у 2 собеседников из 141" in разделы["формальное"]


def test_the_owners_decisions_do_not_leak_into_registers():
    # Раздел решений стоит до первого регистра, и раскладка его отбрасывает:
    # в порождаемых файлах живёт измеренное «как есть», решения — в правила.md.
    с_решениями = ФАЙЛ.replace(
        "---\n\n### Регистр: свои",
        "## Решения владельца\n\nМысль одним сообщением.\n\n---\n\n### Регистр: свои",
        1,
    )

    разделы = skillpack.split(с_решениями)

    assert "Мысль одним сообщением" not in разделы["свои"]
    assert "Мысль одним сообщением" not in разделы["формальное"]


def test_a_decision_missing_from_the_source_is_named_aloud():
    # Пересборка, забывшая подать решения, выглядит случившейся: файл собран,
    # регистры на месте. Расхождение называется вслух — как у stale.
    решения = {"формальное": {"правка": "Мысль одним сообщением."}}

    assert skillpack.undecided(ФАЙЛ, решения) == ["формальное"]
    assert skillpack.undecided(ФАЙЛ + "\nМысль одним сообщением.\n", решения) == []


@pytest.fixture
def скилл(tmp_path):
    """Папка скилла: рукописные файлы на месте, порождаемых ещё нет."""
    (tmp_path / "references").mkdir()
    (tmp_path / "SKILL.md").write_text("рука", encoding="utf-8")
    (tmp_path / "FEEDBACK.md").write_text("рука", encoding="utf-8")
    (tmp_path / "references" / "правила.md").write_text("рука", encoding="utf-8")
    return tmp_path


def test_the_written_files_land_in_references(скилл):
    написано = skillpack.write(скилл, ФАЙЛ, ОТДЕЛЬНЫЕ)

    имена = sorted(путь.name for путь in написано)
    assert имена == ["нурлан.md", "свои.md", "формальное.md"]
    assert all(путь.parent.name == "references" for путь in написано)


def test_the_hand_written_files_are_never_touched(скилл):
    # На этом держится весь развод: пересборка свода не должна стирать правила,
    # выведенные из замечаний владельца.
    skillpack.write(скилл, ФАЙЛ, ОТДЕЛЬНЫЕ)

    assert (скилл / "SKILL.md").read_text(encoding="utf-8") == "рука"
    assert (скилл / "FEEDBACK.md").read_text(encoding="utf-8") == "рука"
    assert (скилл / "references" / "правила.md").read_text(encoding="utf-8") == "рука"


def test_a_register_that_left_the_source_leaves_the_skill(скилл):
    # Иначе файл выбывшего регистра останется лежать и будет читаться как
    # действующий. Надгробий не держим.
    (скилл / "references" / "приятельское.md").write_text("старьё", encoding="utf-8")

    skillpack.write(скилл, ФАЙЛ, ОТДЕЛЬНЫЕ)

    assert not (скилл / "references" / "приятельское.md").exists()


def test_the_recognition_table_is_read_from_the_source():
    числа = skillpack.numbers(ФАЙЛ)

    assert числа["свои"] == ["55", "24%", "11%", "3%"]
    assert числа["формальное"] == ["141", "67%", "20%", "3%"]


def test_a_number_gone_stale_in_the_skill_is_named_aloud():
    # SKILL.md пишется руками, а числа в нём взяты из свода. Пересборка их не
    # правит — значит расхождение надо называть, а не молча оставлять цифру,
    # которой нельзя верить.
    устарел = ФАЙЛ.replace("| свои | семья, близкие друзья | 55 |",
                           "| свои | семья, близкие друзья | 49 |")

    assert skillpack.stale(устарел, ФАЙЛ) == ["свои"]
    assert skillpack.stale(ФАЙЛ, ФАЙЛ) == []


def test_a_register_missing_from_the_skill_counts_as_stale():
    без = ФАЙЛ.replace("| формальное | исполнители, старшие | 141 | 67% | 20% | 3% |\n", "")

    assert skillpack.stale(без, ФАЙЛ) == ["формальное"]


def test_a_folder_without_a_skill_file_is_refused(tmp_path):
    # Раскладка удаляет из references всё лишнее. Промахнуться папкой значит
    # стереть чужое, поэтому цель опознаётся по SKILL.md до первой записи.
    with pytest.raises(ValueError, match="SKILL.md"):
        skillpack.write(tmp_path, ФАЙЛ, ОТДЕЛЬНЫЕ)


def test_an_empty_split_refuses_to_wipe_the_skill(скилл):
    # split(), вернувший пусто, — это съехавший формат, а не пустой свод.
    # Раскладка снесла бы все файлы регистров молча и вернула «написано:
    # нурлан.md» без единой ошибки.
    (скилл / "references" / "свои.md").write_text("порождённый", encoding="utf-8")

    with pytest.raises(ValueError, match="регистр"):
        skillpack.write(скилл, "# Файл, в котором формат съехал\n", ОТДЕЛЬНЫЕ)

    assert (скилл / "references" / "свои.md").exists()


def test_a_set_apart_file_missing_on_disk_is_kept_in_the_skill(скилл):
    # Файла на диске нет — это не «человек выбыл»: промах файлом стирал бы
    # порождённую половину скилла. Кого беречь, говорят решения владельца.
    (скилл / "references" / "нурлан.md").write_text("порождённый", encoding="utf-8")

    skillpack.write(скилл, ФАЙЛ, отдельные={}, беречь={"нурлан.md"})

    assert (скилл / "references" / "нурлан.md").exists()


def test_a_set_apart_file_nobody_protects_leaves_with_the_rest(скилл):
    # Выделенного больше нет в решениях — его файл не отличим от выбывшего
    # регистра. Надгробий не держим.
    (скилл / "references" / "нурлан.md").write_text("старьё", encoding="utf-8")

    skillpack.write(скилл, ФАЙЛ, отдельные={})

    assert not (скилл / "references" / "нурлан.md").exists()


def test_pack_collects_the_set_apart_files_by_the_decisions(скилл, tmp_path, monkeypatch):
    # Раскладка сама читает, кого сводили отдельно, и берёт их файлы из
    # out/voice. Файл, названный в решениях, но не лежащий на диске, в
    # references не пишется и не стирается.
    from tgservice.voice import classify
    from tgservice.voice.common import write_json
    voice = tmp_path / "voice"
    voice.mkdir()
    (voice / "стиль.md").write_text(ФАЙЛ, encoding="utf-8")
    (voice / "нурлан.md").write_text(ОТДЕЛЬНЫЕ["нурлан.md"], encoding="utf-8")
    write_json(voice / "решения-по-классам.json", {classify.ОТДЕЛЬНО: {
        "Нурлан Брат": {"класс": "отдельно", "файл": "нурлан.md"},
        "Тимур": {"класс": "отдельно", "файл": "тимур.md"},
    }})
    (скилл / "references" / "тимур.md").write_text("порождённый", encoding="utf-8")
    monkeypatch.setattr(skillpack, "VOICE", voice)
    monkeypatch.setattr(skillpack, "STYLE", voice / "стиль.md")
    monkeypatch.setattr(skillpack, "DECISIONS", voice / "решения-по-классам.json")

    написано, устарело, без_правок = skillpack.pack(скилл)

    имена = sorted(путь.name for путь in написано)
    assert имена == ["нурлан.md", "свои.md", "формальное.md"]
    assert (скилл / "references" / "тимур.md").exists()


def test_pack_applies_the_threshold_by_the_cards_on_disk(скилл, tmp_path, monkeypatch):
    # Порог считается по карточкам — они лежат рядом с решениями, и раскладка
    # читает их сама, чтобы выделить тех же людей, что и классификация.
    from tgservice.voice import classify
    from tgservice.voice.common import write_json
    voice = tmp_path / "voice"
    voice.mkdir()
    (voice / "стиль.md").write_text(ФАЙЛ, encoding="utf-8")
    (voice / "нурлан-брат.md").write_text("# Как владелец пишет Нурлану\n", encoding="utf-8")
    write_json(voice / "решения-по-классам.json", {classify.ПОРОГ: 0.5})
    write_json(voice / "карточки.json", [{"имя": "Нурлан Брат", "сообщений": 600},
                                          {"имя": "Санжар", "сообщений": 400}])
    monkeypatch.setattr(skillpack, "VOICE", voice)
    monkeypatch.setattr(skillpack, "STYLE", voice / "стиль.md")
    monkeypatch.setattr(skillpack, "DECISIONS", voice / "решения-по-классам.json")
    monkeypatch.setattr(skillpack, "CARDS", voice / "карточки.json")

    написано, _, _ = skillpack.pack(скилл)

    assert "нурлан-брат.md" in {путь.name for путь in написано}


def test_a_skill_row_for_a_register_gone_from_the_source_is_stale():
    # Обратная сторона сверки: регистр выбыл из свода, а его строка в таблице
    # SKILL.md жила вечно — надгробие с числами, которым нельзя верить.
    без_формального = ФАЙЛ.replace(
        "| формальное | исполнители, старшие | 141 | 67% | 20% | 3% |\n", ""
    ).replace("### Регистр: формальное", "### Регистр: другое")

    assert skillpack.stale(ФАЙЛ, без_формального) == ["формальное"]


def test_fractional_numbers_are_compared_too():
    # «0,4%» не считалось числом — дробная ячейка выпадала из сверки молча.
    дробный = ФАЙЛ.replace("| 24% | 11% |", "| 24% | 0,4% |")

    assert skillpack.stale(ФАЙЛ, дробный) == ["свои"]
