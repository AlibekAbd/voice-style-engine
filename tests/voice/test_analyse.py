import pathlib
"""Порядок работ на батче и прогон по всем батчам сразу.

Здесь проверяется только распорядок: кого зовут, в каком порядке, что
пропускают и что делают с упавшим. Сами обращения к модели заглушены — их
проверять нечем, они стоят денег и ходят в сеть.
"""

import io
import json
import threading

import pytest

from tgservice.voice import analyse
from tgservice.voice import classify
from tgservice.voice import compose
from tgservice.voice import verify

BATCH = """# Переписка: Санжар Брат (@sanzhar_b)

Собеседник: Санжар Брат (@sanzhar_b), id 123
Часть 1 из 1

## 2025-09-24

10:00 я: Салем брат
10:01 Санжар Брат: Уа алейкум ассалам
10:02 я: давай завтра созвонимся
10:05 я: ок принял
"""

# Каждое сообщение владельца на месте, отметки верные — сверке не к чему
# придраться, и переспрашивать не о чем.
CLEAN = """# Раскладка

2025-09-24 10:00 я: Салем брат
2025-09-24 10:02 я: давай завтра созвонимся
2025-09-24 10:05 я: ок принял
"""

# Последняя строка — выдумка: такого в выгрузке нет.
DIRTY = CLEAN + "2025-09-24 10:07 я: перезвоню через полчаса\n"


@pytest.fixture
def place(tmp_path, monkeypatch):
    """Батчи и ответы уводятся в свою папку, чтобы не трогать настоящие."""
    batches = tmp_path / "batches"
    answers = tmp_path / "answers"
    batches.mkdir()
    answers.mkdir()
    monkeypatch.setattr(analyse, "BATCHES", batches)
    monkeypatch.setattr(analyse, "ANSWERS", answers)
    monkeypatch.setattr(analyse, "VOICE", tmp_path)
    # Путь к индексу берётся из common — патчим его там, где он живёт, иначе
    # тест награждает дублирование литерала в analyse.
    monkeypatch.setattr(analyse, "BATCH_INDEX", tmp_path / "batches_index.json")
    # Пути, посчитанные при импорте, подменой VOICE не меняются: тест
    # уже написал классификацию в настоящую папку данных.
    monkeypatch.setattr(analyse, "CLASSES", tmp_path / "классы.json")
    monkeypatch.setattr(analyse, "CHECK", tmp_path / "классы.проверка.json")
    monkeypatch.setattr(analyse, "RAW", tmp_path / "raw")
    monkeypatch.setattr(analyse, "VERDICTS", tmp_path / "вердикты")
    monkeypatch.setattr(analyse, "PORTIONS", tmp_path / "порции")
    monkeypatch.setattr(analyse, "MERGED", tmp_path / "своды")
    monkeypatch.setattr(analyse, "DECISIONS", tmp_path / "решения-по-классам.json")
    monkeypatch.setattr(verify, "BATCHES", batches)
    return tmp_path


def put(place, name, batch=BATCH):
    (place / "batches" / name).write_text(batch, encoding="utf-8")
    return name


def answer_of(place, name, kind):
    return place / "answers" / f"{name}.{kind}.md"


def steps(monkeypatch, place, answer=CLEAN, patch="", fall=()):
    """Заглушки трёх обращений к модели. Возвращают список того, кого позвали."""
    called = []

    def review(name, notify=analyse.say):
        called.append(("разбор", name))
        if "разбор" in fall:
            raise RuntimeError("модель отказала")
        answer_of(place, name, "разбор").write_text(answer, encoding="utf-8")

    def again(name, letter, notify=analyse.say, round=1):
        called.append(("переспрос", name))
        analyse.patch_place(name, round).write_text(patch, encoding="utf-8")
        return {"text": patch}  # настоящий `again` отдаёт ответ модели

    def audit(name, notify=analyse.say):
        called.append(("проверка", name))
        answer_of(place, name, "проверка").write_text("проверено", encoding="utf-8")

    monkeypatch.setattr(analyse, "review", review)
    monkeypatch.setattr(analyse, "again", again)
    monkeypatch.setattr(analyse, "audit", audit)
    return called


# --- порядок на одном батче ----------------------------------------------


def test_a_dirty_answer_goes_through_all_four_steps(place, monkeypatch):
    # Разбор → сверка нашла выдумку → переспрос → проверка вторым агентом.
    name = put(place, "sanzhar-123-01.md")
    called = steps(monkeypatch, place, answer=DIRTY)

    analyse.run_batch(name, notify=lambda text: None)

    assert [step for step, _ in called] == ["разбор", "переспрос", "проверка"]


def test_a_clean_answer_is_not_asked_again(place, monkeypatch):
    # Переспрос стоит денег. Спрашивать не о чем — не спрашиваем.
    name = put(place, "sanzhar-123-01.md")
    called = steps(monkeypatch, place, answer=CLEAN)

    analyse.run_batch(name, notify=lambda text: None)

    assert [step for step, _ in called] == ["разбор", "проверка"]


def test_a_finished_batch_is_not_paid_for_twice(place, monkeypatch):
    # Убитый прогон продолжается, а не начинается заново: у батча с разбором и
    # проверкой брать нечего, и второй раз за него платить незачем.
    name = put(place, "sanzhar-123-01.md")
    answer_of(place, name, "разбор").write_text(CLEAN, encoding="utf-8")
    answer_of(place, name, "проверка").write_text("проверено", encoding="utf-8")
    called = steps(monkeypatch, place)

    analyse.run_batch(name, notify=lambda text: None)

    assert called == []


def test_a_run_broken_after_the_review_picks_up_at_the_check(place, monkeypatch):
    # Разбор уже оплачен — переразбирать его нельзя, а проверку доделать надо.
    name = put(place, "sanzhar-123-01.md")
    answer_of(place, name, "разбор").write_text(CLEAN, encoding="utf-8")
    called = steps(monkeypatch, place)

    analyse.run_batch(name, notify=lambda text: None)

    assert [step for step, _ in called] == ["проверка"]


def test_the_patch_is_glued_in_and_the_review_is_not_reprinted(place, monkeypatch):
    # У агента просят заплатку, а не разбор заново: перепечатывая, он теряет
    # материал — однажды выбросил четыреста цитат из полутора тысяч.
    name = put(place, "sanzhar-123-01.md")
    patch = "БЫЛО: 2025-09-24 10:07 я: перезвоню через полчаса\nСТАЛО: 2025-09-24 10:05 я: ок принял\n"
    steps(monkeypatch, place, answer=DIRTY, patch=patch)

    analyse.run_batch(name, notify=lambda text: None)

    mended = answer_of(place, name, "разбор").read_text(encoding="utf-8")
    assert "перезвоню через полчаса" not in mended
    assert "давай завтра созвонимся" in mended


# --- пустой ответ модели -------------------------------------------------
#
# Ловится за настоящей потерей: на одном батче модель вернула пустоту за две
# секунды, код записал пустой файл разбора, счёл шаг сделанным и заплатил ещё
# за переспрос и проверку этой пустоты. Прогон отчитался «прошло 92 из 92».


class Answered:
    """Ответ Gemini вместо сети."""

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def network(monkeypatch):
    """Сеть заменена, ключ не спрашивается, паузы между попытками не выжидаются."""
    monkeypatch.setattr(analyse, "_key", lambda: "ключ")
    monkeypatch.setattr(analyse.time, "sleep", lambda seconds: None)

    def answer(payload):
        monkeypatch.setattr(
            analyse.urllib.request, "urlopen", lambda *a, **k: Answered(payload)
        )

    return answer


def test_an_empty_answer_is_a_refusal_and_not_an_answer(network):
    # Молчание модели надо назвать вслух, а не разложить по файлам как ответ.
    network({"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]})

    with pytest.raises(analyse.Empty):
        analyse.ask("вопрос", notify=lambda text: None)


def test_a_refusal_names_the_reason_the_model_gave(network):
    network({"candidates": [{"content": {"parts": []}, "finishReason": "RECITATION"}]})

    with pytest.raises(analyse.Empty, match="RECITATION"):
        analyse.ask("вопрос", notify=lambda text: None)


def test_an_empty_answer_leaves_no_file_behind(place, network, monkeypatch):
    # Пустой файл хуже отсутствующего: он выглядит готовым шагом.
    name = put(place, "sanzhar-123-01.md")
    network({"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]})

    with pytest.raises(analyse.Empty):
        analyse.review(name, notify=lambda text: None)

    assert not answer_of(place, name, "разбор").exists()


def test_an_empty_file_left_by_an_older_run_is_bought_again(place, monkeypatch):
    # Пустой разбор уже лежит на диске с прошлого прогона — готовым он не считается.
    name = put(place, "sanzhar-123-01.md")
    answer_of(place, name, "разбор").write_text("", encoding="utf-8")
    called = steps(monkeypatch, place)

    analyse.run_batch(name, notify=lambda text: None)

    assert [step for step, _ in called] == ["разбор", "проверка"]


def test_an_answer_that_did_arrive_is_taken(network):
    network({"candidates": [{"content": {"parts": [{"text": "разбор"}]}, "finishReason": "STOP"}]})

    assert analyse.ask("вопрос", notify=lambda text: None)["text"] == "разбор"


# --- пути возобновления и прерывания -------------------------------------
#
# Всё ниже — по код-ревью от 02.09.2026. Общий рисунок находок: основной путь
# выверен прогонами, а запасные — возобновление, прерывание, отказ — в бою не
# выполнялись ни разу.


def test_a_review_that_fell_leaves_no_half_made_batch(place, monkeypatch):
    # Разбор упал — ни заплатки, ни проверки за ним покупать нечего.
    name = put(place, "sanzhar-123-01.md")
    called = steps(monkeypatch, place, fall=("разбор",))

    with pytest.raises(RuntimeError):
        analyse.run_batch(name, notify=lambda text: None)

    assert [step for step, _ in called] == ["разбор"]
    assert not answer_of(place, name, "разбор").exists()
    assert not answer_of(place, name, "проверка").exists()


def test_a_spent_daily_quota_is_not_waited_out(place, monkeypatch):
    # Суточная квота паузой не лечится: шестнадцать потоков по четыре попытки
    # дают шестьдесят четыре бесполезных обращения и стену шума.
    slept = []
    monkeypatch.setattr(analyse, "_key", lambda: "ключ")
    monkeypatch.setattr(analyse.time, "sleep", lambda s: slept.append(s))

    def refuse(*a, **k):
        raise analyse.urllib.error.HTTPError(
            "url", 429, "Too Many Requests", {},
            io.BytesIO(b'{"error":{"message":"Quota exceeded: GenerateRequestsPerDayPerProject"}}'),
        )

    monkeypatch.setattr(analyse.urllib.request, "urlopen", refuse)

    with pytest.raises(analyse.urllib.error.HTTPError):
        analyse.ask("вопрос", notify=lambda text: None)

    assert slept == []


def test_a_paid_patch_is_glued_even_when_the_run_died_before_gluing(place, monkeypatch):
    # Заплатка записана, разбор починить не успели — при возобновлении её надо
    # вклеить, а не пропустить блок целиком: она уже оплачена.
    name = put(place, "sanzhar-123-01.md")
    answer_of(place, name, "разбор").write_text(DIRTY, encoding="utf-8")
    answer_of(place, name, "заплатка").write_text(
        "БЫЛО: 2025-09-24 10:07 я: перезвоню через полчаса\nСТАЛО: —\n", encoding="utf-8"
    )
    called = steps(monkeypatch, place)

    analyse.run_batch(name, notify=lambda text: None)

    assert [step for step, _ in called] == ["проверка"]  # переспрос второй раз не покупаем
    mended = answer_of(place, name, "разбор").read_text(encoding="utf-8")
    assert "перезвоню через полчаса" not in mended


def test_a_hopeless_answer_is_run_again_instead_of_patched(place, monkeypatch):
    # Приговор «перегнать» означает, что латать дороже. Платить за переспрос и
    # проверку такого разбора — выбросить шестьдесят центов.
    name = put(place, "sanzhar-123-01.md")
    hopeless = "# Раскладка\n\n2025-09-24 10:00 я: этого он не писал\n"
    called = []

    def review(name, notify=analyse.say):
        called.append(("разбор", name))
        # Второй разбор выходит годным — так перегон и должен работать.
        text = hopeless if len(called) == 1 else CLEAN
        answer_of(place, name, "разбор").write_text(text, encoding="utf-8")

    monkeypatch.setattr(analyse, "review", review)
    monkeypatch.setattr(
        analyse, "again",
        lambda *a, **k: (called.append(("переспрос", name)), {"text": ""})[1],
    )
    monkeypatch.setattr(analyse, "audit", lambda name, notify=None: (
        called.append(("проверка", name)),
        answer_of(place, name, "проверка").write_text("ok", encoding="utf-8"),
    ))

    analyse.run_batch(name, notify=lambda text: None)

    assert [step for step, _ in called] == ["разбор", "разбор", "проверка"]


def test_a_batch_is_run_again_only_once(place, monkeypatch):
    # Перегон, который тоже вышел безнадёжным, второй раз не покупается.
    name = put(place, "sanzhar-123-01.md")
    hopeless = "# Раскладка\n\n2025-09-24 10:00 я: этого он не писал\n"
    called = []

    def review(name, notify=analyse.say):
        called.append("разбор")
        answer_of(place, name, "разбор").write_text(hopeless, encoding="utf-8")

    monkeypatch.setattr(analyse, "review", review)
    monkeypatch.setattr(
        analyse, "again",
        lambda *a, **k: (called.append("переспрос"), {"text": ""})[1],
    )
    monkeypatch.setattr(analyse, "audit", lambda name, notify=None: called.append("проверка"))

    analyse.run_batch(name, notify=lambda text: None)

    assert called.count("разбор") == 2


def test_a_block_by_content_is_not_retried(place, network, monkeypatch):
    # PROHIBITED_CONTENT на повторах тот же. Четыре отправки промпта по 30 тысяч
    # токенов и две минуты сна — за известный заранее отказ.
    tries = []
    monkeypatch.setattr(analyse.time, "sleep", lambda s: tries.append("сон"))
    network({"candidates": [{"content": {"parts": []}, "finishReason": "PROHIBITED_CONTENT"}]})

    with pytest.raises(analyse.Empty):
        analyse.ask("вопрос", notify=lambda text: None)

    assert tries == []


def test_an_empty_answer_without_a_named_reason_is_still_retried(place, network, monkeypatch):
    # Пустота без причины может быть икотой — её пережидаем, как раньше.
    slept = []
    monkeypatch.setattr(analyse.time, "sleep", lambda s: slept.append(s))
    network({"candidates": [{"content": {"parts": []}}]})

    with pytest.raises(analyse.Empty):
        analyse.ask("вопрос", notify=lambda text: None)

    assert len(slept) == analyse.TRIES - 1


def test_an_interrupted_run_drops_the_queue(place, monkeypatch):
    # Прерывание должно останавливать очередь, а не только главный поток:
    # восемьдесят батчей в очереди — это полчаса и пятьдесят долларов.
    names = [put(place, f"батч-{n}.md") for n in range(6)]
    index(place, names)
    taken = []

    def run_batch(name, notify=analyse.say):
        taken.append(name)
        raise KeyboardInterrupt

    monkeypatch.setattr(analyse, "run_batch", run_batch)

    with pytest.raises(KeyboardInterrupt):
        analyse.all_batches(workers=1, notify=lambda text: None)

    assert len(taken) < len(names)


# --- расход токенов ------------------------------------------------------
#
# Стоит за прогоном на девяносто два батча, после которого цену пришлось
# оценивать по расценкам из документации: `ask` возвращал usageMetadata
# каждым вызовом, а класть его было некуда, и он терялся.


def test_what_the_model_counted_is_written_down(place, network):
    name = put(place, "sanzhar-123-01.md")
    network({
        "candidates": [{"content": {"parts": [{"text": "разбор"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 31000, "candidatesTokenCount": 14000},
    })

    analyse.review(name, notify=lambda text: None)

    spent = json.loads(analyse.spent_place(name).read_text(encoding="utf-8"))
    assert spent["разбор"]["вход"] == 31000
    assert spent["разбор"]["выход"] == 14000


def test_every_step_writes_its_own_line(place, network):
    # Три обращения на батч, и каждое стоит своих денег.
    name = put(place, "sanzhar-123-01.md")
    answer_of(place, name, "разбор").write_text(CLEAN, encoding="utf-8")
    network({
        "candidates": [{"content": {"parts": [{"text": "проверено"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 40000, "candidatesTokenCount": 2000},
    })

    analyse.audit(name, notify=lambda text: None)

    spent = json.loads(analyse.spent_place(name).read_text(encoding="utf-8"))
    assert list(spent) == ["проверка"]
    assert spent["проверка"]["вход"] == 40000


def test_a_repeated_step_does_not_lose_what_was_written_before(place, network):
    # Расход копится по шагам, а не переписывается последним.
    name = put(place, "sanzhar-123-01.md")
    network({
        "candidates": [{"content": {"parts": [{"text": "разбор"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50},
    })
    analyse.review(name, notify=lambda text: None)
    analyse.audit(name, notify=lambda text: None)

    spent = json.loads(analyse.spent_place(name).read_text(encoding="utf-8"))
    assert sorted(spent) == ["проверка", "разбор"]


def test_the_run_says_the_whole_bill_out_loud(place, monkeypatch):
    # Итог прогона должен называть расход, иначе его снова придётся оценивать.
    names = [put(place, "первый.md"), put(place, "второй.md")]
    index(place, names)
    said = []

    def run_batch(name, notify=None):
        analyse.spent_place(name).write_text(
            json.dumps({"разбор": {"вход": 1000, "выход": 500}}), encoding="utf-8"
        )

    monkeypatch.setattr(analyse, "run_batch", run_batch)

    analyse.all_batches(workers=2, notify=said.append)

    assert any("2000" in line and "1000" in line for line in said)


def test_the_run_counts_only_what_this_run_spent(place, monkeypatch):
    # Расход копится за всю жизнь батча. Итог прогона, сложивший и прошлые
    # заходы, завышает цену — и завышение растёт с каждым возобновлением.
    names = [put(place, "первый.md"), put(place, "второй.md")]
    index(place, names)
    analyse.spent_place("первый.md").write_text(
        json.dumps({"разбор": {"вход": 99000, "выход": 99000}}), encoding="utf-8"
    )
    said = []

    def run_batch(name, notify=None):
        spent = json.loads(analyse.spent_place(name).read_text(encoding="utf-8")) if analyse.spent_place(name).exists() else {}
        spent["проверка"] = {"вход": 10, "выход": 5}
        analyse.spent_place(name).write_text(json.dumps(spent), encoding="utf-8")

    monkeypatch.setattr(analyse, "run_batch", run_batch)

    analyse.all_batches(workers=2, notify=said.append)

    итог = [line for line in said if "прогон окончен" in line][0]
    assert "вход 20 токенов" in итог and "выход 10" in итог


def test_thinking_tokens_are_paid_for_and_counted(place, network):
    # Модель рассуждающая, и токены размышления оплачиваются как выход.
    name = put(place, "sanzhar-123-01.md")
    network({
        "candidates": [{"content": {"parts": [{"text": "разбор"}]}, "finishReason": "STOP"}],
        "usageMetadata": {
            "promptTokenCount": 100, "candidatesTokenCount": 50, "thoughtsTokenCount": 700,
        },
    })

    analyse.review(name, notify=lambda text: None)

    spent = json.loads(analyse.spent_place(name).read_text(encoding="utf-8"))
    assert spent["разбор"]["размышление"] == 700


def test_attempts_that_came_back_empty_are_counted_too(place, network, monkeypatch):
    # Пустая попытка стоит полной отправки промпта. Не посчитать её — значит
    # разойтись со счётом Google в другую сторону.
    name = put(place, "sanzhar-123-01.md")
    monkeypatch.setattr(analyse.time, "sleep", lambda s: None)
    network({
        "candidates": [{"content": {"parts": []}}],
        "usageMetadata": {"promptTokenCount": 30000, "candidatesTokenCount": 0},
    })

    with pytest.raises(analyse.Empty):
        analyse.review(name, notify=lambda text: None)

    spent = json.loads(analyse.spent_place(name).read_text(encoding="utf-8"))
    assert spent["разбор впустую"]["вход"] == 30000 * analyse.TRIES


# --- прогон по всем батчам -----------------------------------------------


def index(place, names):
    (place / "batches_index.json").write_text(
        json.dumps({"batches": [{"file": name} for name in names]}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_batches_are_read_at_the_same_time_not_one_after_another(place, monkeypatch):
    # Ради этого всё и затевалось: восемьдесят два батча подряд — это часы.
    # Барьер сойдётся только если два батча в работе одновременно.
    names = [put(place, "первый.md"), put(place, "второй.md")]
    index(place, names)
    barrier = threading.Barrier(2)
    met = []

    def run_batch(name, notify=analyse.say):
        barrier.wait(timeout=5)  # в одиночку не дождётся и уронит тест
        met.append(name)

    monkeypatch.setattr(analyse, "run_batch", run_batch)

    analyse.all_batches(workers=2, notify=lambda text: None)

    assert sorted(met) == ["второй.md", "первый.md"]


def test_a_fallen_batch_does_not_take_the_others_down(place, monkeypatch):
    # Один отказ модели не должен стоить прогона на восемьдесят два батча.
    names = [put(place, "целый.md"), put(place, "битый.md")]
    index(place, names)
    done = []

    def run_batch(name, notify=analyse.say):
        if name == "битый.md":
            raise RuntimeError("модель отказала")
        done.append(name)

    monkeypatch.setattr(analyse, "run_batch", run_batch)

    analyse.all_batches(workers=2, notify=lambda text: None)

    assert done == ["целый.md"]


def test_a_fallen_batch_is_named_aloud(place, monkeypatch):
    # Молча пропущенный батч не найдётся потом ничем.
    names = [put(place, "битый.md")]
    index(place, names)
    said = []

    def run_batch(name, notify=analyse.say):
        raise RuntimeError("модель отказала")

    monkeypatch.setattr(analyse, "run_batch", run_batch)

    analyse.all_batches(workers=2, notify=said.append)

    assert any("битый.md" in line for line in said)


def test_a_finished_batch_is_not_taken_into_the_run(place, monkeypatch):
    # Готовым считается тот, у кого есть проверка: на нём порядок доигран.
    names = [put(place, "готовый.md"), put(place, "новый.md")]
    index(place, names)
    answer_of(place, "готовый.md", "разбор").write_text(CLEAN, encoding="utf-8")
    answer_of(place, "готовый.md", "проверка").write_text("проверено", encoding="utf-8")
    taken = []

    monkeypatch.setattr(analyse, "run_batch", lambda name, notify=None: taken.append(name))

    analyse.all_batches(workers=2, notify=lambda text: None)

    assert taken == ["новый.md"]


# --- второй круг переспроса ----------------------------------------------
#
# Первый круг оставил 385 находок в 58 батчах: 164 обрезанных, 109 неясных
# отметок, 79 выдумок, 21 склейку. Обрезка занижает счёт частот, а частота —
# основа третьего шага, поэтому один круг мало.


def test_a_second_round_writes_its_own_patch_and_keeps_the_first(place, monkeypatch):
    # Заплатка первого круга уже вклеена и оплачена. Затирать её вторым кругом
    # значит терять след того, что и почему правилось.
    name = put(place, "sanzhar-123-01.md")
    answer_of(place, name, "разбор").write_text(DIRTY, encoding="utf-8")
    answer_of(place, name, "заплатка").write_text("первый круг\n", encoding="utf-8")
    steps(monkeypatch, place, patch="БЫЛО: нет такой строки\nСТАЛО: —\n")

    analyse.polish(name, notify=lambda text: None, rounds=2)

    assert answer_of(place, name, "заплатка").read_text(encoding="utf-8") == "первый круг\n"
    assert analyse.patch_place(name, 2).exists()


def test_a_clean_answer_does_not_buy_a_second_round(place, monkeypatch):
    name = put(place, "sanzhar-123-01.md")
    answer_of(place, name, "разбор").write_text(CLEAN, encoding="utf-8")
    called = steps(monkeypatch, place)

    analyse.polish(name, notify=lambda text: None, rounds=2)

    assert called == []


def test_rounds_stop_as_soon_as_the_check_comes_back_clean(place, monkeypatch):
    # Круги идут, пока есть что спросить. Вычистив всё на втором, третий не
    # покупаем.
    name = put(place, "sanzhar-123-01.md")
    answer_of(place, name, "разбор").write_text(DIRTY, encoding="utf-8")
    called = steps(
        monkeypatch, place,
        patch="БЫЛО: 2025-09-24 10:07 я: перезвоню через полчаса\nСТАЛО: —\n",
    )

    analyse.polish(name, notify=lambda text: None, rounds=4)

    assert [step for step, _ in called] == ["переспрос"]


def test_a_round_already_paid_for_is_not_bought_again(place, monkeypatch):
    # Убитая дочистка продолжается: заплатка второго круга лежит — вклеиваем,
    # а не покупаем заново.
    name = put(place, "sanzhar-123-01.md")
    answer_of(place, name, "разбор").write_text(DIRTY, encoding="utf-8")
    analyse.patch_place(name, 2).write_text(
        "БЫЛО: 2025-09-24 10:07 я: перезвоню через полчаса\nСТАЛО: —\n", encoding="utf-8"
    )
    called = steps(monkeypatch, place)

    analyse.polish(name, notify=lambda text: None, rounds=2)

    assert called == []
    assert "перезвоню" not in answer_of(place, name, "разбор").read_text(encoding="utf-8")


def test_only_batches_with_findings_are_taken_into_the_polish(place, monkeypatch):
    names = [put(place, "грязный.md"), put(place, "чистый.md")]
    index(place, names)
    answer_of(place, "грязный.md", "разбор").write_text(DIRTY, encoding="utf-8")
    answer_of(place, "чистый.md", "разбор").write_text(CLEAN, encoding="utf-8")
    taken = []
    monkeypatch.setattr(analyse, "polish", lambda name, notify=None, rounds=2: taken.append(name))

    analyse.all_polish(workers=2, notify=lambda text: None)

    assert taken == ["грязный.md"]


# --- шаг 3: свод описаний ------------------------------------------------
#
# Первая попытка выдавала «файл паттернов» — перечень находок. Свести такие
# файлы дальше нечем: выход устроен иначе, чем вход, и уровень выше пришлось
# бы строить с нуля. Свод отдаёт описание того же вида, что получил.


def test_a_repeated_step_adds_to_the_bill_instead_of_replacing_it(place, network):
    # Перегон записывал расход поверх прежнего, и «дельта за прогон» выходила
    # нулём: пятнадцать сводов по двести тысяч токенов отчитались тремя
    # тысячами. Расход шага копится попытками, а не затирается последней.
    name = put(place, "sanzhar-123-01.md")
    network({
        "candidates": [{"content": {"parts": [{"text": "разбор"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50},
    })

    analyse.review(name, notify=lambda text: None)
    analyse._place(name, "разбор").unlink()
    analyse.review(name, notify=lambda text: None)

    assert analyse.bill([name])["итого"]["вход"] == 200


def test_an_answer_records_which_task_version_made_it(place, network):
    # На диске лежала смесь: разборы от одной редакции задания, проверки от
    # другой, и разобрать это можно было только по времени файлов и памяти.
    # Отметка версии превращает «перегнать сделанное старым» в команду.
    put(place, "батч-01.md")
    network({
        "candidates": [{"content": {"parts": [{"text": "разбор"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    })

    analyse.review("батч-01.md", notify=lambda text: None)

    отметка = json.loads(analyse.spent_place("батч-01.md").read_text(encoding="utf-8"))
    assert отметка["разбор"]["задание"] == analyse.version("разбор.md")


def test_the_version_changes_when_the_task_changes(place, tmp_path, monkeypatch):
    # Версия — отпечаток самого текста задания, а не его имени: правка на одну
    # строку обязана дать другую отметку.
    monkeypatch.setattr(analyse, "PROMPTS", tmp_path)
    (tmp_path / "проба.md").write_text("правило первое", encoding="utf-8")
    было = analyse.version("проба.md")
    (tmp_path / "проба.md").write_text("правило первое и второе", encoding="utf-8")

    assert analyse.version("проба.md") != было


def test_a_stamp_that_slid_is_repaired_in_the_stored_review(place):
    # «Уехавшую отметку скрипт чинит молча» было написано в коде, но `repair`
    # применялся только вручную, ключом `--fix`: прогон чинил её в памяти ради
    # счёта полноты и выбрасывал. В файле стиля так и остались цитаты, которых
    # по их же отметке в выгрузке не найти.
    put(place, "батч-01.md")
    (place / "answers" / "батч-01.md.разбор.md").write_text(
        "# Раскладка\n\n2025-09-23 10:00 я: Салем брат\n", encoding="utf-8"
    )

    analyse.look("батч-01.md", verify.load("батч-01.md"), notify=lambda text: None)

    разбор = (place / "answers" / "батч-01.md.разбор.md").read_text(encoding="utf-8")
    assert "2025-09-24 10:00 я: Салем брат" in разбор


def test_the_review_is_left_alone_when_there_is_nothing_to_repair(place):
    # Переписывать файл без нужды нельзя: у прогона шестнадцать потоков.
    put(place, "батч-01.md")
    (place / "answers" / "батч-01.md.разбор.md").write_text(CLEAN, encoding="utf-8")
    было = (place / "answers" / "батч-01.md.разбор.md").stat().st_mtime_ns

    analyse.look("батч-01.md", verify.load("батч-01.md"), notify=lambda text: None)

    assert (place / "answers" / "батч-01.md.разбор.md").stat().st_mtime_ns == было


def test_the_classifier_gets_every_card_but_the_set_apart(place, monkeypatch):
    # Самый плотный собеседник даёт больше половины сообщений владельца.
    # Класс, куда он попадёт, станет его портретом, а не портретом класса, —
    # поэтому категория ему назначена решением владельца, и в лист агенту он
    # не идёт.
    карточки = [
        {"чат": "1", "имя": "Нурлан Брат", "сообщений": 40000, "портрет": "брат",
         "блоки": [], "батчи": [], "разошлось": None},
        {"чат": "2", "имя": "Санжар", "сообщений": 400, "портрет": "друг",
         "блоки": [], "батчи": [], "разошлось": None},
    ]
    отдельно = {"Нурлан Брат": {"класс": "Нурлан — отдельная категория", "файл": "нурлан.md"}}
    спрошено = []
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: спрошено.append(текст) or {
        "text": "1 | Санжар | друг\n\n# Типы\n\nдруг — общение без дистанции\n", "usage": {}
    })

    роздано = analyse.classify_all(карточки, отдельно=отдельно, notify=lambda text: None)

    assert "Нурлан Брат" not in спрошено[0]
    assert "Санжар" in спрошено[0]
    assert роздано["Нурлан Брат"] == "Нурлан — отдельная категория"
    assert роздано["Санжар"] == "друг"


def test_the_set_apart_are_read_from_the_decisions_file_by_default(place, monkeypatch):
    # Вызов из процесса идёт без параметра: решения лежат файлом, и функция
    # находит их сама. Иначе пересборка, забывшая подать выделенных, отдала бы
    # самого плотного собеседника агенту молча.
    from tgservice.voice.common import write_json
    write_json(analyse.DECISIONS, {classify.ОТДЕЛЬНО: {
        "Нурлан Брат": {"класс": "отдельно", "файл": "нурлан.md"}}})
    карточки = [
        {"чат": "1", "имя": "Нурлан Брат", "сообщений": 40000, "портрет": "брат",
         "блоки": [], "батчи": [], "разошлось": None},
        {"чат": "2", "имя": "Санжар", "сообщений": 400, "портрет": "друг",
         "блоки": [], "батчи": [], "разошлось": None},
    ]
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: {
        "text": "1 | Санжар | друг\n\n# Типы\n\nдруг — общение без дистанции\n", "usage": {}})

    роздано = analyse.classify_all(карточки, notify=lambda text: None)

    assert роздано["Нурлан Брат"] == "отдельно"


def test_the_threshold_from_the_decisions_file_sets_people_apart(place, monkeypatch):
    # Порог лежит в том же файле решений; без параметра классификатор
    # считает доли по поданным карточкам и выделяет сам.
    from tgservice.voice.common import write_json
    write_json(analyse.DECISIONS, {classify.ПОРОГ: 0.5})
    карточки = [
        {"чат": "1", "имя": "Нурлан Брат", "сообщений": 40000, "портрет": "брат",
         "блоки": [], "батчи": [], "разошлось": None},
        {"чат": "2", "имя": "Санжар", "сообщений": 400, "портрет": "друг",
         "блоки": [], "батчи": [], "разошлось": None},
    ]
    спрошено = []
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: спрошено.append(текст) or {
        "text": "1 | Санжар | друг\n\n# Типы\n\nдруг — общение без дистанции\n", "usage": {}})

    роздано = analyse.classify_all(карточки, notify=lambda text: None)

    assert "Нурлан Брат" not in спрошено[0]
    assert роздано["Нурлан Брат"] == "Нурлан Брат — отдельная категория"


def test_a_set_apart_person_absent_from_the_cards_is_not_invented(place, monkeypatch):
    # Файл решений одного владельца, карточки другого: выделенного в корпусе
    # нет, и в ответе его быть не должно.
    карточки = [{"чат": "2", "имя": "Санжар", "сообщений": 400, "портрет": "друг",
                 "блоки": [], "батчи": [], "разошлось": None}]
    отдельно = {"Нурлан Брат": {"класс": "отдельно", "файл": "нурлан.md"}}
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: {
        "text": "1 | Санжар | друг\n\n# Типы\n\nдруг — общение без дистанции\n", "usage": {}})

    роздано = analyse.classify_all(карточки, отдельно=отдельно, notify=lambda text: None)

    assert "Нурлан Брат" not in роздано


def test_a_classification_that_lost_people_is_refused(place, monkeypatch):
    # Не сошлось — ответ бракуется целиком. Латать по частям нельзя: непонятно,
    # что агент видел, когда раздавал остальное.
    карточки = [
        {"чат": "2", "имя": "Санжар", "сообщений": 400, "портрет": "друг",
         "блоки": [], "батчи": [], "разошлось": None},
        {"чат": "3", "имя": "Тимур", "сообщений": 20, "портрет": "подрядчик",
         "блоки": [], "батчи": [], "разошлось": None},
    ]
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: {
        "text": "1 | Санжар | друг\n", "usage": {}
    })

    with pytest.raises(analyse.Empty, match="не роздано"):
        analyse.classify_all(карточки, notify=lambda text: None)


def test_no_path_of_the_module_points_outside_the_test_place(place):
    # Пути, посчитанные при импорте, подменой VOICE не меняются. На этом уже
    # обожглись: тест написал классификацию в настоящую папку данных, потому
    # что `CLASSES` считался от `VOICE` в момент импорта.
    #
    # Проверяются все разом: заводя новый путь, легко забыть про фикстуру, а
    # цена забывчивости — порча настоящих данных молча.
    свои = [имя for имя in dir(analyse)
            if имя.isupper() and isinstance(getattr(analyse, имя), pathlib.Path)]

    чужие = [имя for имя in свои if place not in getattr(analyse, имя).parents
             and getattr(analyse, имя) != place]

    assert чужие == ["PROMPTS"]  # задания лежат в пакете, а не в данных


def test_the_check_never_tells_the_judge_the_class_it_is_checking(place, monkeypatch):
    # Скажешь судье присвоенный класс — он согласится, и проверка померит
    # сговорчивость, а не классификацию.
    (place / "raw").mkdir()
    (place / "raw" / "77.json").write_text(json.dumps({"meta": {"name": "Санжар"}, "messages": [
        {"date": "2026-04-08T06:00", "out": False, "text": "Салем брат"},
        {"date": "2026-04-08T06:01", "out": True, "text": "Уа алейкум"},
    ]}, ensure_ascii=False), encoding="utf-8")
    карточки = [{"чат": "77", "имя": "Санжар", "сообщений": 400, "портрет": "друг",
                 "блоки": [], "батчи": [], "разошлось": None}]
    спрошено = []
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: спрошено.append(текст) or {
        "text": "близкий друг | уверенно", "usage": {}
    })

    итог = analyse.classify_check(
        {"Санжар": "близкий друг"}, карточки,
        {"близкий друг": "без дистанции", "старший": "почтительно"},
        сколько=5, notify=lambda text: None,
    )

    assert "Салем брат" in спрошено[0]          # сырая речь, а не портрет
    # Присвоенный класс в задании не назван. Ищем до перечня типов: сам
    # перечень даётся судье намеренно, это условие задачи, а не подсказка.
    assert "близкий друг" not in спрошено[0].split("# Типы")[0]
    assert итог["сошлось"] == 1
    assert итог["разошлось"] == []


def test_a_verdict_already_bought_is_not_bought_again(place, monkeypatch):
    # Прогон судьи вставал на висящем соединении, и убить его значило потерять
    # тридцать одно оплаченное обращение: вердикты никуда не ложились.
    (place / "raw").mkdir()
    (place / "raw" / "77.json").write_text(json.dumps({"meta": {"name": "Санжар"}, "messages": [
        {"date": "2026-04-08T06:00", "out": True, "text": "Салем брат"},
    ]}, ensure_ascii=False), encoding="utf-8")
    карточки = [{"чат": "77", "имя": "Санжар", "сообщений": 400, "портрет": "друг",
                 "блоки": [], "батчи": [], "разошлось": None}]
    обращений = []
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: обращений.append(1) or {
        "text": "близкий друг | уверенно", "usage": {}
    })
    доводы = ({"Санжар": "близкий друг"}, карточки, {"близкий друг": "без дистанции"})

    analyse.classify_check(*доводы, сколько=5, notify=lambda text: None)
    analyse.classify_check(*доводы, сколько=5, notify=lambda text: None)

    assert len(обращений) == 1


def test_the_small_call_does_not_wait_half_an_hour(place, monkeypatch):
    # Таймаут в 1800 секунд поставлен под разбор батча. На коротком вопросе
    # висящее соединение держит поток полчаса, и восемь потоков встают все.
    сроки = []

    class Ответ:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "да"}]}, "finishReason": "STOP"}]}
        ).encode()

    monkeypatch.setattr(analyse, "_key", lambda: "ключ")
    monkeypatch.setattr(analyse.urllib.request, "urlopen",
                        lambda *a, **k: сроки.append(k.get("timeout")) or Ответ())

    analyse.ask("вопрос", timeout=180, notify=lambda text: None)

    assert сроки == [180]


def test_a_merge_already_bought_is_not_bought_again(place, monkeypatch):
    # Восемнадцать платных вызовов по доллару. Убитый прогон обязан
    # продолжаться, а не начинаться заново.
    (place / "порции").mkdir()
    (place / "порции" / "свои-1.md").write_text("описания", encoding="utf-8")
    (place / "своды").mkdir()
    (place / "своды" / "свои-1.md").write_text("## 2. Блоки\n\n### Привет\n", encoding="utf-8")
    обращений = []
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: обращений.append(1) or {
        "text": "## 2. Блоки", "usage": {}})

    analyse.merge_portion("свои-1", notify=lambda text: None)

    assert обращений == []


def test_an_empty_merge_on_disk_is_not_counted_as_done(place, monkeypatch):
    # Пустой файл от старого прогона готовым шагом не считается: на этом уже
    # потеряли батч на 1745 сообщений.
    (place / "порции").mkdir()
    (place / "порции" / "свои-1.md").write_text("описания", encoding="utf-8")
    (place / "своды").mkdir()
    (place / "своды" / "свои-1.md").write_text("", encoding="utf-8")
    обращений = []
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: обращений.append(1) or {
        "text": "## 2. Блоки", "usage": {}})

    analyse.merge_portion("свои-1", notify=lambda text: None)

    assert обращений == [1]


def test_the_impersonator_is_shown_the_whole_quote(place, monkeypatch):
    # Телефон стоял второй строкой цитаты. Отбирая только строки с отметкой
    # времени, агенту его не показали — и залатали регуляркой свою же дыру.
    файл = place / "стиль.md"
    файл.write_text(
        "Примеры:\n2026-02-06 02:58 я: Бахыт - стоматолог\n     +7 701 000 00 00\n",
        encoding="utf-8",
    )
    спрошено = []
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: спрошено.append(текст) or {
        "text": "БЫЛО: нет\nСТАЛО: нет\n", "usage": {}})

    analyse.impersonate(файл, notify=lambda text: None)

    assert "+7 701 000 00 00" in спрошено[0]


def test_the_glued_patches_are_counted_by_what_changed(place, monkeypatch):
    # Считали по тому, находится ли «БЫЛО» в файле после правки. Правка, не
    # легшая вовсе, тоже не находится — и шла в отчёт как вклеенная.
    файл = place / "стиль.md"
    файл.write_text("2025-11-17 05:11 я: Айгерим, добрый день !\n", encoding="utf-8")
    сказано = []
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: {
        "text": "БЫЛО: чего в файле нет\nСТАЛО: неважно\n", "usage": {}})

    analyse.impersonate(файл, notify=сказано.append)

    assert any("0 из 1" in строка for строка in сказано)


def test_what_the_pattern_found_is_shown_to_the_agent_not_replaced_by_script(place, monkeypatch):
    # Образец неполон по устройству и чистить им нельзя — на этом обожглись.
    # Но найти то, что агент пропустил, он умеет: номер карты и три юзернейма
    # остались в файле после первого прохода. Найденное показывается агенту,
    # а решает по-прежнему агент.
    файл = place / "стиль.md"
    файл.write_text("2025-10-02 18:55 я: 4400430286055170\n", encoding="utf-8")
    спрошено = []
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: спрошено.append(текст) or {
        "text": "БЫЛО: 4400430286055170\nСТАЛО: <номер карты>\n", "usage": {}})

    analyse.impersonate(файл, ищем=[r"(?<!\d)\d{16}(?!\d)"], notify=lambda text: None)

    # Найденное образцом стоит отдельной главой, до общего списка цитат.
    assert "4400430286055170" in спрошено[0].split("# Цитаты")[0]
    assert "проверь особо" in спрошено[0]
    assert "4400430286055170" not in файл.read_text(encoding="utf-8")


def test_the_answering_agent_gets_the_style_file_and_the_meaning(place, monkeypatch):
    # Замер третьей спеки: агент получает файл стиля, переписку и смысл.
    # Регистр он определяет сам — в работе его никто не подскажет.
    (place / "стиль.md").write_text("### Регистр: свои\n\nСвои.\n", encoding="utf-8")
    обмен = {"chat_id": "123", "chat": "Санжар", "username": None, "kind": "dm",
             "chat_msgs": 4200, "context": [],
             "incoming": [{"date": "2026-04-08T06:04", "out": False, "text": "Подъедешь?"}],
             "reply": [{"date": "2026-04-08T06:05", "out": True, "text": "Через полчаса"}],
             "answered_in_minutes": 1}
    спрошено = []
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: спрошено.append(текст) or {
        "text": "Через час буду\nЖди", "usage": {}})

    ответ = analyse.reply_as_owner(обмен, "сообщает, когда будет", place / "стиль.md",
                                   notify=lambda text: None)

    assert ответ == ["Через час буду", "Жди"]
    assert "Подъедешь?" in спрошено[0]
    assert "сообщает, когда будет" in спрошено[0]
    assert "Регистр: свои" in спрошено[0]
    assert "Через полчаса" not in спрошено[0]   # ответ владельца не показан


# --- находки ревью 07.09.2026: перегон, обрывы, заплатки, судья -----------


def test_a_second_hopeless_verdict_does_not_buy_the_check(place, monkeypatch):
    # После второго «перегнать» переспрос и проверка не покупаются: батч
    # остаётся неготовым и виден в «осталось». Раньше код падал сквозь,
    # покупал QC по мусору, и батч навсегда считался готовым.
    name = put(place, "sanzhar-123-01.md")
    hopeless = "# Раскладка\n\n2025-09-24 10:00 я: этого он не писал\n"
    called = []

    def review(name, notify=analyse.say):
        called.append("разбор")
        answer_of(place, name, "разбор").write_text(hopeless, encoding="utf-8")

    monkeypatch.setattr(analyse, "review", review)
    monkeypatch.setattr(
        analyse, "again",
        lambda *a, **k: (called.append("переспрос"), {"text": ""})[1],
    )
    monkeypatch.setattr(analyse, "audit", lambda name, notify=None: (
        called.append("проверка"),
        answer_of(place, name, "проверка").write_text("ok", encoding="utf-8"),
    ))

    analyse.run_batch(name, notify=lambda text: None)

    assert "переспрос" not in called
    assert "проверка" not in called
    assert not analyse._done(name, "проверка")


def test_a_rerun_buries_the_stale_patch_and_check(place, monkeypatch):
    # Старая заплатка вклеивалась в новый разбор («уже оплачена»), а лежащая
    # проверка пропускала аудит текста, которого второй агент не видел.
    name = put(place, "sanzhar-123-01.md")
    hopeless = "# Раскладка\n\n2025-09-24 10:00 я: этого он не писал\n"
    analyse.patch_place(name).write_text("БЫЛО: старое\nСТАЛО: чужое\n", encoding="utf-8")
    answer_of(place, name, "проверка").write_text("по старому разбору", encoding="utf-8")
    called = []

    def review(name, notify=analyse.say):
        called.append("разбор")
        text = hopeless if called.count("разбор") == 1 else CLEAN
        answer_of(place, name, "разбор").write_text(text, encoding="utf-8")

    monkeypatch.setattr(analyse, "review", review)
    monkeypatch.setattr(analyse, "again", lambda *a, **k: {"text": ""})
    monkeypatch.setattr(analyse, "audit", lambda name, notify=None: (
        called.append("проверка"),
        answer_of(place, name, "проверка").write_text("по новому", encoding="utf-8"),
    ))

    analyse.run_batch(name, notify=lambda text: None)

    assert not analyse.patch_place(name).exists()
    assert "проверка" in called


def test_a_truncated_patch_is_a_refusal_and_not_a_patch(place, monkeypatch):
    # Обрыв на потолке вывода достраивал пару без «СТАЛО», а пара без
    # «СТАЛО» — это удаление настоящей цитаты из оплаченного разбора. Хуже:
    # файл непустой, и при возобновлении заплатка «уже оплачена» — целой её
    # не перекупят никогда.
    name = put(place, "sanzhar-123-01.md")
    answer_of(place, name, "разбор").write_text(DIRTY, encoding="utf-8")
    monkeypatch.setattr(analyse, "ask", lambda *a, **k: {
        "text": "БЫЛО: 2025-09-24 10:05 я: ок принял", "reason": "MAX_TOKENS",
        "usage": {"вход": 1, "выход": 1, "размышление": 0}})

    with pytest.raises(analyse.Empty, match="потолк"):
        analyse.again(name, "письмо", notify=lambda text: None)

    assert not analyse.patch_place(name).exists()


def test_a_truncated_merge_is_not_written_as_a_merge(place, monkeypatch):
    # Обрезанный свод ложился файлом и навсегда считался готовым: своей
    # сверки у сводов нет, и обрыв не поймал бы никто.
    (place / "порции").mkdir()
    (place / "порции" / "свои-1.md").write_text("лист", encoding="utf-8")
    monkeypatch.setattr(analyse, "ask", lambda *a, **k: {
        "text": "полсвода", "reason": "MAX_TOKENS",
        "usage": {"вход": 1, "выход": 1, "размышление": 0}})

    with pytest.raises(analyse.Empty, match="потолк"):
        analyse.merge_portion("свои-1", notify=lambda text: None)

    assert not (place / "своды" / "свои-1.md").exists()
    assert (place / "своды" / "свои-1.обрыв.md").exists()  # оплаченное не выброшено


def test_a_patch_that_parsed_into_nothing_does_not_pass_the_batch(place, monkeypatch):
    # «Внесено 0, не найдено 0» из непустого письма — заплатка не по формату:
    # находки целы, а батч уехал бы в проверку и готовность. Файл заплатки
    # удаляется, чтобы следующий прогон перекупил её, а не вклеил пустоту.
    name = put(place, "sanzhar-123-01.md")
    called = steps(monkeypatch, place, answer=DIRTY, patch="поправил всё, смотри выше\n")

    with pytest.raises(analyse.Empty, match="пар"):
        analyse.run_batch(name, notify=lambda text: None)

    assert not analyse.patch_place(name).exists()
    assert "проверка" not in [step for step, _ in called]


def test_a_broken_classification_answer_is_still_written_to_disk(place, monkeypatch):
    # Ответ куплен. Потерять его текст значит перезапускать вслепую: нечего
    # прочитать глазами и нечего разобрать иначе.
    карточки = [
        {"чат": "2", "имя": "Санжар", "сообщений": 400, "портрет": "друг",
         "блоки": [], "батчи": [], "разошлось": None},
        {"чат": "3", "имя": "Тимур", "сообщений": 20, "портрет": "подрядчик",
         "блоки": [], "батчи": [], "разошлось": None},
    ]
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: {
        "text": "1 | Санжар | друг\n", "usage": {}
    })

    with pytest.raises(analyse.Empty, match="не роздано"):
        analyse.classify_all(карточки, notify=lambda text: None)

    assert "Санжар" in (place / "классы.ответ.брак.md").read_text(encoding="utf-8")


def test_a_type_without_a_definition_fails_the_classification(place, monkeypatch):
    # Тип без определения ломает проверку судьёй: судья не может его назвать,
    # и все люди типа уезжают в «разошлось» за деньги.
    карточки = [
        {"чат": "2", "имя": "Санжар", "сообщений": 400, "портрет": "друг",
         "блоки": [], "батчи": [], "разошлось": None},
    ]
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: {
        "text": "1 | Санжар | друг\n", "usage": {}
    })

    with pytest.raises(analyse.Empty, match="определени"):
        analyse.classify_all(карточки, notify=lambda text: None)


def test_an_unparsed_verdict_is_not_cached_as_bought(place, monkeypatch):
    # Ответ не по формату кэшировался пустым вердиктом навсегда: человек
    # вечно висел в «разошлось: не по формату», и перекупить его было нечем.
    (place / "raw").mkdir()
    (place / "raw" / "77.json").write_text(json.dumps({"meta": {"name": "Санжар"}, "messages": [
        {"date": "2026-04-08T06:00", "out": True, "text": "Салем брат"},
    ]}, ensure_ascii=False), encoding="utf-8")
    карточки = [{"чат": "77", "имя": "Санжар", "сообщений": 400, "портрет": "друг",
                 "блоки": [], "батчи": [], "разошлось": None}]
    ответы = iter(["мнение агента без разделителя", "близкий друг | уверенно"])
    обращений = []
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: обращений.append(1) or {
        "text": next(ответы), "usage": {}
    })
    доводы = ({"Санжар": "близкий друг"}, карточки, {"близкий друг": "без дистанции"})

    analyse.classify_check(*доводы, сколько=5, notify=lambda text: None)
    второй = analyse.classify_check(*доводы, сколько=5, notify=lambda text: None)

    assert len(обращений) == 2
    assert второй["сошлось"] == 1


def test_a_tie_naming_the_assigned_class_is_not_a_mismatch(place, monkeypatch):
    # Задание разрешает судье ничью «тип | не отличить от: другой». Считать её
    # расхождением значит занижать долю именно на близких классах — а по этой
    # доле выбиралась сетка регистров.
    (place / "raw").mkdir()
    (place / "raw" / "77.json").write_text(json.dumps({"meta": {"name": "Санжар"}, "messages": [
        {"date": "2026-04-08T06:00", "out": True, "text": "Салем брат"},
    ]}, ensure_ascii=False), encoding="utf-8")
    карточки = [{"чат": "77", "имя": "Санжар", "сообщений": 400, "портрет": "друг",
                 "блоки": [], "батчи": [], "разошлось": None}]
    monkeypatch.setattr(analyse, "ask", lambda текст, **kw: {
        "text": "старший | не отличить от: близкий друг", "usage": {}
    })

    итог = analyse.classify_check(
        {"Санжар": "близкий друг"}, карточки, {"близкий друг": "б", "старший": "с"},
        сколько=5, notify=lambda text: None,
    )

    assert итог["разошлось"] == []
    assert итог["не различимо"] == ["Санжар"]


def test_paid_attempts_are_not_lost_on_a_later_network_failure(place, monkeypatch):
    # Первая попытка оплачена (пустая, но с расходом), вторая падает сетью
    # навсегда. Голое исключение теряло накопленный расход — против правила
    # «не посчитать значит разойтись со счётом Google».
    monkeypatch.setattr(analyse, "_key", lambda: "ключ")
    monkeypatch.setattr(analyse.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def urlopen(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return Answered({"usageMetadata": {"promptTokenCount": 100},
                             "candidates": [{"content": {"parts": []}}]})
        raise analyse.urllib.error.URLError("сеть упала")

    monkeypatch.setattr(analyse.urllib.request, "urlopen", urlopen)

    with pytest.raises(analyse.Empty) as err:
        analyse.ask("вопрос", notify=lambda text: None)

    assert err.value.usage["вход"] == 100
