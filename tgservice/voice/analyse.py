"""Разбор батчей моделью: сам разбор, переспрос по находкам, проверка вторым агентом.

Три обращения к Gemini, все по заданиям из `prompts/`. Задания там и правятся:
здесь только доставка текста и приём ответа.

Прогон одного батча целиком — `прогон`, он же `run_batch`:

    разбор  →  сверка  →  если находки: переспрос  →  сверка  →  проверка

Шаги доступны и порознь, когда надо вмешаться руками:

    python -m tgservice.voice разбор --batch <файл>     # агент читает батч
    python -m tgservice.voice verify --answer <..> --batch <..>   # скрипт сверяет
    python -m tgservice.voice переспрос --batch <файл>  # агент правит названное
    python -m tgservice.voice проверка --batch <файл>   # второй агент читает разбор

`прогон-все` гонит порядок по всем батчам, `разбор-все` — только первый шаг.
Оба идут пачкой в несколько потоков и пропускают готовое: убитый прогон
продолжается, а не начинается заново.

**Это платно.** Разбор батча стоит около сорока центов, проверка — двадцать.
"""

import concurrent.futures
import hashlib
import json
import pathlib
import re
import threading
import time
import urllib.error
import urllib.request

from .. import config
from ..gemini import DAILY_QUOTA_MARKS, REFUSAL_REASONS
from . import classify
from . import compose
from . import trial
from . import verify
from .common import (
    BATCH_INDEX,
    BATCHES,
    DECISIONS,
    VOICE,
    read_json,
    say,
    write_json,
    write_text,
)

ANSWERS = VOICE / "answers"
PROMPTS = config.PACKAGE_DIR / "voice" / "prompts"
BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MODEL = "gemini-3.1-pro-preview"
MAX_OUT = 65536

# Сколько раз пробуем при отказе сервера или обрыве сети. Повтор стоял только
# на отказах, и одна сетевая икота теряла батч: цикл ехал дальше, а в отчёте не
# хватало ответа.
TRIES = 4

# Пауза перед следующей попыткой: 20, 40, 60 секунд. Одна на все причины —
# ждём мы одного и того же, чтобы на той стороне отпустило.
BACKOFF = 20

# Сколько батчей читается одновременно. Батчи друг о друге не знают: у каждого
# свой файл ответа, к сессии Телеграма разбор не ходит. Упирается всё в квоту
# Gemini, а не в наш код, поэтому число подбирается по отказам 429, а не по
# ядрам машины. Восемьдесят два батча подряд — это три с половиной часа.
WORKERS = 16


class Empty(RuntimeError):
    """Модель промолчала. Это отказ, а не ответ, и файлом он не ложится.

    Стоит за потерянным батчем: на одном куске переписки модель вернула
    пустоту за две секунды, код записал пустой файл разбора, счёл шаг
    сделанным — и заплатил ещё за переспрос и проверку этой пустоты. Прогон
    отчитался «прошло 92 из 92», и пропажа нашлась только по нулевой полноте.

    Носит с собой расход: пустая попытка стоит полной отправки промпта, и
    не посчитать её значит разойтись со счётом Google в другую сторону.
    """

    def __init__(self, text, usage=None):
        super().__init__(text)
        self.usage = usage or {"вход": 0, "выход": 0, "размышление": 0}


def version(name):
    """Отпечаток задания: чем сделан ответ.

    На диске лежала смесь — своды от одной редакции задания, проверки от
    другой, — и разобрать это можно было только по времени файлов и по памяти.
    Отметка превращает «перегнать сделанное старым заданием» в команду, а не в
    догадку.

    Отпечаток берётся с текста, а не с имени: правка на одну строку обязана
    дать другую отметку.
    """
    тело = (PROMPTS / name).read_bytes()
    return hashlib.sha256(тело).hexdigest()[:12]


def prompt(name):
    """Задание из `prompts/`. Комментарий-шапка в модель не уходит."""
    text = (PROMPTS / name).read_text(encoding="utf-8")
    head = text.find("-->")
    return text[head + 3 :].lstrip() if text.lstrip().startswith("<!--") else text


def _key():
    values = config.load_env()
    (api_key,) = config.require(values, ["GEMINI_API_KEY"])
    return api_key


def _add(spent, usage):
    """Сложить счёт модели в наш. Токены размышления оплачиваются как выход."""
    spent["вход"] += usage.get("promptTokenCount", 0)
    spent["выход"] += usage.get("candidatesTokenCount", 0)
    spent["размышление"] += usage.get("thoughtsTokenCount", 0)
    return spent


def ask(text, temperature=0.2, timeout=1800, notify=say):
    """Один вопрос модели. Отказы сервера, обрывы сети и молчание пережидаются.

    Расход копится по всем попыткам, а не только по удачной: четыре пустых
    ответа стоят четырёх отправок промпта.

    Срок ожидания задаётся вызовом. По умолчанию он под разбор батча — там
    ответ идёт минутами. На коротком вопросе такой срок означает, что висящее
    соединение держит поток полчаса, и восемь потоков встают все разом.
    """
    spent = {"вход": 0, "выход": 0, "размышление": 0}
    url = f"{BASE}/{MODEL}:generateContent?key={_key()}"
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {"maxOutputTokens": MAX_OUT, "temperature": temperature},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    for attempt in range(TRIES):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as answer:
                payload = json.loads(answer.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            notify(f"HTTP {error.code}: {detail}")
            flat = detail.lower().replace("_", "").replace("-", "")
            if error.code == 429 and any(mark in flat for mark in DAILY_QUOTA_MARKS):
                # Суточная квота паузой не лечится. Шестнадцать потоков по
                # четыре попытки дают шестьдесят четыре бесполезных
                # обращения и стену шума вместо одной внятной строки.
                notify("суточная квота Gemini выбрана — до завтра")
                raise
            if not (error.code in (429, 500, 503) and attempt < TRIES - 1):
                if spent["вход"]:
                    # Прежние попытки этого вызова оплачены. Голое исключение
                    # теряло их расход: _paid ловит только Empty.
                    raise Empty(f"HTTP {error.code} после оплаченных попыток: {detail}", spent)
                raise
            time.sleep(BACKOFF * (attempt + 1))
            continue
        except Exception as error:
            notify(f"сеть: {type(error).__name__}: {error}")
            if attempt >= TRIES - 1:
                if spent["вход"]:
                    raise Empty(f"сеть после оплаченных попыток: {error}", spent)
                raise
            time.sleep(BACKOFF * (attempt + 1))
            continue
        _add(spent, payload.get("usageMetadata", {}) or {})
        candidate = (payload.get("candidates") or [{}])[0]
        parts = candidate.get("content", {}).get("parts", []) or []
        answered = "".join(part.get("text", "") for part in parts)
        if answered.strip():
            return {
                "text": answered,
                "reason": candidate.get("finishReason"),
                "usage": spent,
            }
        # Пустой ответ приходит мгновенно и выглядит как успех: код двести,
        # обрыва нет, просто нет слов. Причину модель называет сама.
        blocked = payload.get("promptFeedback", {}).get("blockReason")
        why = candidate.get("finishReason") or blocked or "причины не назвала"
        notify(f"модель промолчала: {why}")
        if why in REFUSAL_REASONS or blocked:
            # Внутри прогона повтор бесполезен: один батч уходил на четыре
            # полных отправки тридцатитысячного промпта и две минуты сна за
            # отказ, известный с первой попытки.
            #
            # Между прогонами — полезен. Тот же батч отказал четырежды подряд,
            # а на следующем заходе прошёл целиком. Готовым он не считается,
            # значит следующий прогон возьмёт его сам: вечен не отказ, а запись
            # «этот батч не готов».
            raise Empty(f"модель отказала, в этом прогоне повтор не поможет: {why}", spent)
        if attempt < TRIES - 1:
            time.sleep(BACKOFF * (attempt + 1))
            continue
        raise Empty(f"модель вернула пустой ответ: {why}", spent)


def _batch(name):
    return (BATCHES / name).read_text(encoding="utf-8")


def _place(name, kind):
    return ANSWERS / f"{name}.{kind}.md"


def _done(name, kind):
    """Шаг сделан, если его файл есть и в нём что-то написано.

    Пустой файл хуже отсутствующего: он выглядит готовым шагом и молча уводит
    батч из прогона. Так и потерялся кусок переписки.
    """
    place = _place(name, kind)
    return place.exists() and place.stat().st_size > 0


def spent_place(name):
    return ANSWERS / f"{name}.расход.json"


def _spend(name, kind, usage, задание=None):
    """Записать, во что обошёлся шаг. Счёт модели, а не наша оценка.

    `ask` возвращал usageMetadata с самого начала, и класть его было некуда:
    после прогона на девяносто два батча цену пришлось прикидывать по
    расценкам из документации вместо того, чтобы прочитать.

    Копится по шагам: три обращения на батч, каждое со своей ценой.

    Замка нет намеренно: файл расхода свой у каждого батча, а три его шага
    идут в одном потоке друг за другом. Появится шаг, который зовут из
    другого потока, — замок понадобится.
    """
    spent = read_json(spent_place(name), {}) or {}
    было = spent.get(kind) or {}
    # Копится, а не затирается: перегон писал расход поверх прежнего, и
    # «дельта за прогон» выходила нулём — пятнадцать сводов по двести тысяч
    # токенов отчитались тремя тысячами.
    spent[kind] = {
        key: было.get(key, 0) + usage.get(key, 0)
        for key in ("вход", "выход", "размышление")
    }
    spent[kind]["модель"] = MODEL
    spent[kind]["обращений"] = было.get("обращений", 0) + 1
    if задание:
        spent[kind]["задание"] = задание
    write_json(spent_place(name), spent)
    return spent


def _paid(name, kind, call, задание=None):
    """Обращение к модели с записью расхода — и удачного, и впустую.

    Заодно пишется отпечаток задания: по нему видно, какой редакцией сделан
    ответ, и не надо гадать по времени файла.
    """
    try:
        answer = call()
    except Empty as error:
        _spend(name, f"{kind} впустую", error.usage, задание)
        raise
    _spend(name, kind, answer["usage"], задание)
    return answer


def bill(names=None):
    """Сколько стоил прогон: вход, выход и по шагам. Считается по файлам расхода."""
    names = names if names is not None else batches()
    steps = {}
    for name in names:
        for kind, row in (read_json(spent_place(name), {}) or {}).items():
            place = steps.setdefault(
                kind, {"обращений": 0, "вход": 0, "выход": 0, "размышление": 0}
            )
            place["обращений"] += row.get("обращений", 1)
            for key in ("вход", "выход", "размышление"):
                place[key] += row.get(key, 0)
    # Итог считается из шагов, а не копится рядом с ними: две параллельные
    # суммы разойдутся молча, стоит появиться новой статье расхода.
    total = {
        key: sum(place[key] for place in steps.values())
        for key in ("вход", "выход", "размышление")
    }
    return {"итого": total, "по шагам": steps}


def review(name, notify=say):
    """Шаг 1 второй спеки: агент читает батч и описывает манеру."""
    started = time.time()
    answer = _paid(
        name, "разбор",
        lambda: ask(prompt("разбор.md") + _batch(name), notify=notify),
        version("разбор.md"),
    )
    write_text(_place(name, "разбор"), answer["text"])
    notify(
        f"{name}: разбор {len(answer['text'])} знаков за {time.time() - started:.0f} с"
        + (" — ОБРЫВ" if answer["reason"] == "MAX_TOKENS" else "")
    )
    return answer


def again(name, letter, notify=say, round=1):
    """Переспрос: агент правит названное и присылает заплатку, а не разбор заново.

    Просить разбор целиком нельзя: агент перепечатывает его и попутно теряет
    материал — однажды выбросил четыреста цитат из полутора тысяч.
    """
    text = "".join(
        [
            prompt("переспрос.md"),
            letter,
            "\n\n# Переписка\n\n",
            _batch(name),
            "\n\n# Твой разбор, который надо поправить\n\n",
            _place(name, "разбор").read_text(encoding="utf-8"),
        ]
    )
    answer = _paid(name, f"переспрос {round}", задание=version("переспрос.md"), call=lambda: ask(text, notify=notify))
    if answer["reason"] == "MAX_TOKENS":
        # Обрезанная заплатка хуже пустой: разбор пар достроил бы хвостовую
        # пару без «СТАЛО», а пара без «СТАЛО» — это удаление настоящей цитаты
        # из оплаченного разбора. И файл непустой: при возобновлении такая
        # заплатка «уже оплачена», целой её не перекупили бы никогда.
        raise Empty("заплатка оборвана на потолке вывода — файлом не ложится", answer["usage"])
    write_text(patch_place(name, round), answer["text"])
    notify(f"{name}: заплатка {len(answer['text'].splitlines())} строк")
    return answer


def audit(name, notify=say):
    """Проверка вторым агентом: утверждения о манере, которые поиском не сверить."""
    text = "".join(
        [
            prompt("проверка.md"),
            _batch(name),
            "\n\n# Разбор, который надо проверить\n\n",
            _place(name, "разбор").read_text(encoding="utf-8"),
        ]
    )
    answer = _paid(name, "проверка", задание=version("проверка.md"), call=lambda: ask(text, temperature=0.1, notify=notify))
    write_text(_place(name, "проверка"), answer["text"])
    notify(f"{name}: проверка {len(answer['text'])} знаков")
    return answer


def batches():
    """Все батчи по индексу. Порядок — как в индексе, чтобы прогон был повторяем."""
    index = read_json(BATCH_INDEX, {}) or {}
    return [row["file"] for row in index.get("batches", [])]


HOPELESS = "перегнать"


def look(name, batch, notify=say):
    """Сверка с голосом. Денег не стоит, потому гоняется на каждом заходе.

    Уехавшая отметка чинится здесь же и ложится обратно в разбор. Раньше
    `repair` применялся только руками, ключом `--fix`: прогон чинил отметку в
    памяти ради счёта полноты и выбрасывал починенное. Разбор оставался с
    отметкой, по которой цитату в выгрузке не найти, и она доезжала до файла
    стиля. Чинится только однозначное — текст, встречающийся в выгрузке один
    раз; спорное уходит на переспрос.
    """
    место = _place(name, "разбор")
    разбор = место.read_text(encoding="utf-8")
    result = verify.check(разбор, batch)
    mended, fixes = verify.repair(разбор, batch)
    if fixes:
        write_text(место, mended)
    spread = result["coverage"]
    notify(
        f"{name}: сверка — {result['verdict']},"
        f" цитат {result['quotes']},"
        f" разложено {spread['laid']} из {spread['total']}"
        f" ({100 * spread['share']:.0f}%)"
        + (f", отметок починено {len(fixes)}" if fixes else "")
    )
    return result


def patch_place(name, round=1):
    """Куда ложится заплатка круга. У каждого своя — затирать чужую нельзя.

    Заплатка первого круга уже оплачена и вклеена. Перезаписав её вторым
    кругом, теряем след того, что и почему правилось.
    """
    return _place(name, "заплатка" if round == 1 else f"заплатка-{round}")


def mend(name, batch, result=None, notify=say, round=1):
    """Переспрос по находкам и вклейка заплатки — одной дорогой для прогона и для рук.

    Раньше эта последовательность жила в двух копиях, в `run_batch` и в CLI, и
    они успели разойтись: ручной переспрос платил всегда, автоматический
    пропускал вклейку при лежащей заплатке.

    **Охраняется только платный шаг.** Вклейка бесплатна и повторяется без
    вреда: уже применённые строки «БЫЛО» просто лягут в «не найдено». Когда
    охрана стояла на всём блоке, смерть прогона между записью заплатки и
    записью починенного разбора теряла оплаченную заплатку навсегда.
    """
    answer_place = _place(name, "разбор")
    answer = answer_place.read_text(encoding="utf-8")
    result = verify.check(answer, batch) if result is None else result
    letter = verify.ask_again(result)
    if not letter:
        notify(f"{name}: к разбору вопросов нет, переспрашивать нечего")
        return result
    place = patch_place(name, round)
    if place.exists() and place.stat().st_size > 0:
        patch = place.read_text(encoding="utf-8")
        notify(f"{name}: заплатка круга {round} уже оплачена, вклеиваю")
    else:
        patch = again(name, letter, notify=notify, round=round)["text"]
    if patch.strip() and not verify.patch_pairs(patch):
        # Непустое письмо, из которого не разобралось ни одной пары, — провал
        # формата, а не успех: находки целы, а батч уехал бы в проверку и
        # готовность. Файл сносится, чтобы следующий прогон перекупил
        # заплатку, а не вклеил эту пустоту снова.
        place.unlink(missing_ok=True)
        raise Empty(f"{name}: заплатка без единой пары «БЫЛО/СТАЛО» — не по формату")
    mended, note = verify.apply_patch(answer, patch)
    write_text(answer_place, mended)
    result = verify.check(mended, batch)
    notify(f"{name}: круг {round} вклеен — {note}, стало «{result['verdict']}»")
    return result


def polish(name, notify=say, rounds=2):
    """Дочистка: круги переспроса, пока сверка находит и круги не кончились.

    Первый круг оставил 385 находок в 58 батчах — 164 обрезанных, 109 неясных
    отметок, 79 выдумок, 21 склейку. Обрезка занижает счёт частот, а частота
    лежит в основе третьего шага, поэтому одного круга мало.
    """
    batch = verify.load(name)
    result = look(name, batch, notify=notify)
    for round in range(2, rounds + 1):
        if not verify.ask_again(result):
            notify(f"{name}: чисто, круг {round} не нужен")
            break
        result = mend(name, batch, result, notify=notify, round=round)
    return result


def dirty():
    """Батчи, к которым у сверки ещё есть вопросы. Считается даром."""
    left = []
    for name in batches():
        if not _done(name, "разбор"):
            continue
        batch = verify.load(name)
        answer = _place(name, "разбор").read_text(encoding="utf-8")
        if verify.ask_again(verify.check(answer, batch)):
            left.append(name)
    return left


def all_polish(rounds=2, workers=WORKERS, notify=say):
    """Дочистка по всем батчам, где сверке ещё есть что спросить."""
    left = dirty()
    notify(f"батчей с находками: {len(left)}")
    was = bill(left)["итого"]
    fallen = _spread(left, lambda name, notify: polish(name, notify=notify, rounds=rounds), notify, workers)
    now = bill(left)["итого"]
    paid = {key: now[key] - was[key] for key in now}
    notify(
        f"дочистка окончена: прошло {len(left) - len(fallen)} из {len(left)},"
        f" вход {paid['вход']} токенов, выход {paid['выход']}"
        f", размышление {paid['размышление']}"
    )
    return fallen


def run_batch(name, notify=say):
    """Весь порядок на одном батче: разбор → сверка → переспрос → сверка → проверка.

    Каждый шаг пропускается, если его файл уже лежит и не пуст. Так убитый
    прогон продолжается с места обрыва и не платит второй раз за готовое.
    """
    if not _done(name, "разбор"):
        review(name, notify=notify)
    batch = verify.load(name)
    result = look(name, batch, notify=notify)
    if result["verdict"] == HOPELESS and not _done(name, "перегон"):
        # «Перегнать» — это приговор порогов verify: латать дороже, чем купить
        # разбор заново. Платить за переспрос и проверку такого разбора значит
        # выбросить шестьдесят центов и оставить батч готовым с мусором внутри.
        notify(f"{name}: приговор «{HOPELESS}» — разбор покупается заново")
        # Старая заплатка и проверка написаны по разбору, который сейчас
        # умрёт: заплатка вклеивала бы чужие правки в новый текст под видом
        # «уже оплачена», а лежащая проверка пропускала бы аудит. Сносятся до
        # метки: умри прогон между ними, батч останется неготовым, а не
        # готовым с чужим QC.
        for stale in [_place(name, "проверка"), *ANSWERS.glob(f"{name}.заплатка*.md")]:
            stale.unlink(missing_ok=True)
        write_text(_place(name, "перегон"), "разбор перегнан один раз\n")
        _place(name, "разбор").unlink()
        review(name, notify=notify)
        result = look(name, batch, notify=notify)
    if result["verdict"] == HOPELESS:
        # Второй перегон не покупаем: если задание даёт мусор дважды, дело в
        # задании или в батче, и деньгами это не лечится. Переспрос и проверку
        # по мусору не покупаем тем более — иначе батч навсегда числился бы
        # готовым, и «дальше руками» не наступало никогда.
        notify(f"{name}: и после перегона «{HOPELESS}» — дальше руками, переспрос и проверка не покупаются")
        return result
    result = mend(name, batch, result, notify=notify)
    if not _done(name, "проверка"):
        audit(name, notify=notify)
    return result


def _spread(names, work, notify, workers):
    """Пачка батчей в несколько потоков. Упавший называется вслух и не роняет прогон.

    Голос каждого батча копится отдельно и выходит одним куском, когда батч
    закончен. Иначе шестнадцать потоков перемешают свои строки между собой, и
    в отчёте не разобрать, чьё «разложено 62%».
    """
    fallen = []

    taken = [0]
    voice = threading.Lock()

    def one(name):
        with voice:
            taken[0] += 1
            # Батч, висящий на получасовом таймауте, иначе неотличим от
            # мёртвого прогона: до конца он молчит.
            notify(f"взят [{taken[0]}/{len(names)}] {name}")
        lines = []
        try:
            work(name, notify=lines.append)
        except Exception as error:
            # Один отказ модели не должен стоить прогона на восемьдесят два батча.
            lines.append(f"{name}: НЕ ВЫШЛО — {type(error).__name__}: {error}")
            return name, lines, False
        return name, lines, True

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        tasks = [pool.submit(one, name) for name in names]
        for number, task in enumerate(concurrent.futures.as_completed(tasks), 1):
            name, lines, good = task.result()
            if not good:
                fallen.append(name)
            notify("\n".join([f"[{number}/{len(names)}] {name}"] + lines))
    except KeyboardInterrupt:
        # Без сброса очереди Ctrl+C останавливает только главный поток, а
        # восемьдесят батчей продолжают жечь по полдоллара ещё полчаса.
        # Летящие вызовы доедут — их деньги уже потрачены.
        pool.shutdown(wait=False, cancel_futures=True)
        notify("прервано: очередь сброшена, летящие батчи доедут")
        raise
    finally:
        pool.shutdown(wait=True)
    if fallen:
        notify("не вышло: " + ", ".join(fallen))
    return fallen


def all_batches(workers=WORKERS, notify=say):
    """Весь порядок по всем батчам. Готовым считается тот, у кого есть проверка."""
    names = batches()
    left = [name for name in names if not _done(name, "проверка")]
    notify(f"батчей всего {len(names)}, готово {len(names) - len(left)}, осталось {len(left)}")
    # Расход копится за всю жизнь батча, через все прогоны. Итог этого
    # запуска — разница, иначе цена растёт с каждым возобновлением сама.
    was = bill(left)["итого"]
    fallen = _spread(left, run_batch, notify, workers)
    now = bill(left)["итого"]
    paid = {key: now[key] - was[key] for key in now}
    notify(
        f"прогон окончен: прошло {len(left) - len(fallen)} из {len(left)},"
        f" вход {paid['вход']} токенов, выход {paid['выход']}"
        f", размышление {paid['размышление']}"
    )
    return fallen


def all_reviews(workers=WORKERS, notify=say):
    """Только первый шаг по всем батчам, без сверки и проверки."""
    names = batches()
    left = [name for name in names if not _done(name, "разбор")]
    notify(f"батчей всего {len(names)}, разобрано {len(names) - len(left)}, осталось {len(left)}")
    return _spread(left, review, notify, workers)


CLASSES = VOICE / "классы.json"


def classify_all(карточки, отдельно=None, notify=say):
    """Один агент раздаёт класс отношений каждому собеседнику.

    Выделенные (`classify.apart`) в лист не идут: самый плотный собеседник
    даёт больше половины всех сообщений владельца, и класс, куда он попадёт,
    станет его портретом, а не портретом класса. Класс ему назначен решением
    владельца. Без параметра выделенные читаются из файла решений — вызов из
    процесса идёт без него.

    Ответ, где кто-то не роздан или выдуман номер, бракуется целиком. Латать
    классификацию по частям нельзя: непонятно, что агент видел, когда раздавал
    остальное.
    """
    if отдельно is None:
        отдельно = classify.apart(read_json(DECISIONS, {}), карточки)
    свои = [к for к in карточки if к["имя"] not in отдельно]
    лист = classify.sheet(свои)
    notify(f"карточек агенту: {len(свои)}, лист {len(лист):,} знаков")
    answer = _paid(
        "классификация", "классификация",
        lambda: ask(prompt("классификация.md") + лист, notify=notify),
        version("классификация.md"),
    )
    роздано = classify.assignments(answer["text"])
    типы = classify.definitions(answer["text"], классы=set(роздано.values()))
    беда = classify.integrity(роздано, свои, типы)
    if not беда["цело"]:
        # Ответ куплен — терять его текст значит перезапускать вслепую:
        # бракованный ответ ложится своим файлом до отказа.
        write_text(CLASSES.with_suffix(".ответ.брак.md"), answer["text"])
        raise Empty(
            f"классификация неполна: не роздано {len(беда['не роздано'])}"
            f", лишних номеров {len(беда['лишние'])}"
            f", без определения {len(беда['без определения'])}",
            answer["usage"],
        )
    notify(f"типов вышло: {беда['классов']}")
    по_имени = {к["имя"]: роздано[номер] for номер, к in enumerate(свои, 1)}
    в_корпусе = {к["имя"] for к in карточки}
    for имя, запись in отдельно.items():
        if имя in в_корпусе:
            по_имени[имя] = запись["класс"]
    write_text(CLASSES.with_suffix(".ответ.md"), answer["text"])
    write_json(CLASSES, по_имени)
    return по_имени


RAW = VOICE / "raw"
CHECK = VOICE / "классы.проверка.json"
VERDICTS = VOICE / "вердикты"
СУД = re.compile(r"^\s*(?P<тип>[^|\n]+?)\s*\|\s*(?P<уверен>.+?)\s*$", re.M)


def classify_check(роздано, карточки, типы, сколько=5, notify=say):
    """Проверка классификации по сырой речи: свой агент на каждого проверяемого.

    По выгрузке, а не по карточке: классификация построена на портретах, а
    портреты писал агент первого шага. Проверять портрет портретом значит
    проверять агента им же самим.

    Судье не говорят присвоенный класс — иначе он согласится, и проверка
    померит сговорчивость.
    """
    по_имени = {к["имя"]: к for к in карточки}
    номера = {к["имя"]: n for n, к in enumerate(карточки, 1)}
    выборка = classify.sample(
        {номера[имя]: класс for имя, класс in роздано.items() if имя in номера}, сколько
    )
    словарь = chr(10).join(f"{т} — {о}" for т, о in sorted(типы.items()))
    очередь, немы = [], []
    for класс, кто in sorted(выборка.items()):
        for номер in кто:
            карточка = карточки[номер - 1]
            место = RAW / f"{карточка['чат'].lstrip('-')}.json"
            if not место.exists():
                место = RAW / f"{карточка['чат']}.json"
            if место.exists():
                очередь.append((класс, карточка, место))
            else:
                немы.append(карточка["имя"])

    def один(дело):
        класс, карточка, место = дело
        готов = VERDICTS / f"{карточка['чат']}.json"
        прежний = read_json(готов, {})
        # Пустой «судья» готовым не считается: ответ не по формату кэшировался
        # навсегда, и человек вечно висел в «разошлось: не по формату».
        if прежний and прежний.get("судья"):
            return класс, карточка, прежний["судья"], прежний.get("рядом", "")
        сообщения = read_json(место, {}).get("messages", [])
        отрывок = classify.excerpt(сообщения, карточка["имя"])
        задание = prompt("классификация-проверка.md") + словарь
        задание += chr(10) * 2 + "# Отрывок переписки" + chr(10) * 2 + отрывок
        answer = _paid(
            f"проверка-класса-{карточка['чат']}", "проверка класса",
            lambda: ask(задание, temperature=0.0, timeout=180, notify=lambda текст: None),
            version("классификация-проверка.md"),
        )
        найдено = СУД.search(answer["text"])
        сказал = найдено.group("тип").strip() if найдено else ""
        хвост = найдено.group("уверен").strip() if найдено else ""
        # Ничья разрешена заданием: «тип | не отличить от: другой тип».
        рядом = хвост.split("не отличить от:", 1)[1].strip() if "не отличить от" in хвост else ""
        # Вердикт ложится сразу: прогон вставал на висящем соединении, и убить
        # его значило потерять тридцать одно оплаченное обращение. Сырой ответ
        # рядом: без него вердикт не по формату нечем ни перечитать, ни судить.
        write_json(готов, {"кто": карточка["имя"], "судья": сказал, "рядом": рядом,
                           "ответ": answer["text"]})
        return класс, карточка, сказал, рядом

    сошлось, разошлось, ничьи = 0, [], []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS // 2)
    try:
        for класс, карточка, сказал, рядом in pool.map(один, очередь):
            if сказал == класс:
                сошлось += 1
            elif рядом and класс == рядом:
                # Судья сказал «между ними выбрать нечем» и назвал присвоенный
                # класс вторым. Это не расхождение: считать ничью промахом
                # значит занижать долю именно на близких классах — а по этой
                # доле выбиралась сетка регистров.
                ничьи.append(карточка["имя"])
            else:
                разошлось.append({"кто": карточка["имя"], "присвоен": класс,
                                  "судья": сказал or "не по формату"})
    except KeyboardInterrupt:
        # Очередь платных обращений отменяется вместе с главным потоком. Без
        # `cancel_futures` прерывание останавливало только его, а восемьдесят
        # заданий в очереди дожигали до сорока восьми долларов.
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=True)
    всего = сошлось + len(разошлось) + len(ничьи)
    notify(
        f"проверено {всего}: сошлось {сошлось}, разошлось {len(разошлось)}"
        + (f", не различимо {len(ничьи)}" if ничьи else "")
    )
    решено = сошлось + len(разошлось)
    итог = {"сошлось": сошлось, "разошлось": разошлось, "не различимо": ничьи,
            "без выгрузки": немы,
            "доля": сошлось / решено if решено else 0.0}
    write_json(CHECK, итог)
    return итог


PORTIONS = VOICE / "порции"
MERGED = VOICE / "своды"


def merge_portion(имя, notify=say):
    """Свод по одной порции. Готовое второй раз не покупается.

    Пустой файл готовым не считается: на этом уже потеряли батч на 1 745
    сообщений — модель вернула пустоту, код записал файл нулевого размера и
    счёл шаг сделанным.
    """
    место = MERGED / f"{имя}.md"
    if место.exists() and место.stat().st_size > 0:
        notify(f"{имя}: свод уже есть")
        return None
    лист = (PORTIONS / f"{имя}.md").read_text(encoding="utf-8")
    начало = time.time()
    answer = _paid(
        f"свод-{имя}", "свод",
        lambda: ask(prompt("свод.md") + лист, notify=notify),
        version("свод.md"),
    )
    MERGED.mkdir(exist_ok=True)
    if answer.get("reason") == "MAX_TOKENS":
        # Обрезанный свод, легши файлом, навсегда считался бы готовым: своей
        # сверки у сводов нет, и обрыв не поймал бы никто. Оплаченный кусок
        # не выбрасывается — ложится рядом, но готовым шагом не считается.
        write_text(место.with_suffix(".обрыв.md"), answer["text"])
        raise Empty(f"{имя}: свод оборван на потолке вывода, кусок в {место.with_suffix('.обрыв.md').name}", answer["usage"])
    write_text(место, answer["text"])
    notify(f"{имя}: свод {len(answer['text']):,} знаков за {time.time() - начало:.0f} с")
    return answer


def merge_all(имена, workers=6, notify=say):
    """Своды по всем порциям пачкой. Упавшая не роняет остальные."""
    павшие = []

    def один(имя):
        try:
            merge_portion(имя, notify=notify)
        except Exception as беда:
            notify(f"{имя}: НЕ ВЫШЛО — {type(беда).__name__}: {беда}")
            return имя
        return None

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        павшие = [имя for имя in pool.map(один, имена) if имя]
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=True)
    notify(f"сводов: прошло {len(имена) - len(павшие)} из {len(имена)}")
    if павшие:
        notify("не вышли: " + ", ".join(павшие))
    return павшие


def impersonate(файл, ищем=(), notify=say):
    """Обезличить цитаты файла: оборот остаётся, обстоятельства уходят.

    Цитата подаётся целиком, с продолжениями. Отбор по строкам с отметкой
    времени показывал агенту только первую строку — телефон стоял второй и в
    файле остался. Латать это образцом нельзя: образец неполон по устройству,
    ловит «+7 701…» и пропускает написанное иначе.

    Правка идёт заплаткой «БЫЛО/СТАЛО», как переспрос. Файл целиком у агента
    просить нельзя: он его перепечатает и потеряет.

    Счёт — по факту замены. Прежний считал вклеенным всё, чьё «БЫЛО» после
    правки в файле не находится; правка, не легшая вовсе, тоже не находится.
    """
    путь = pathlib.Path(файл)
    текст = путь.read_text(encoding="utf-8")
    цитаты = compose.quotes(текст)
    notify(f"{путь.name}: цитат {len(цитаты)}")
    # Образцом чистить нельзя — он неполон по устройству. Но найти то, что
    # агент пропустил, он умеет: после первого прохода в файле остались номер
    # карты и три юзернейма. Найденное показывается агенту, решает агент.
    подозрительные = []
    for образец in ищем:
        for одна in цитаты:
            if re.search(образец, одна) and одна not in подозрительные:
                подозрительные.append(одна)
    # Задание само кончается заголовком «# Цитаты» — подозрительное встаёт
    # перед ним, иначе оно читается как часть общего списка.
    голова, _, хвост = prompt("обезличивание.md").partition("# Цитаты")
    задание = голова
    if подозрительные:
        notify(f"{путь.name}: образец нашёл подозрительное в {len(подозрительные)} цитатах")
        задание += ("# Здесь образец нашёл личные данные — проверь особо" + chr(10) * 2
                    + chr(10).join(подозрительные) + chr(10) * 2)
    задание += "# Цитаты" + хвост.rstrip() + chr(10) * 2 + chr(10).join(цитаты)
    answer = _paid(
        f"обезличивание-{путь.stem}", "обезличивание",
        lambda: ask(задание, notify=notify),
        version("обезличивание.md"),
    )
    заплатка = путь.with_suffix(".заплатка.md")
    write_text(заплатка, answer["text"])
    правки = compose.replacements(answer["text"])
    обезличено, легло = compose.impersonal(текст, правки, счёт=True)
    write_text(путь, обезличено)
    notify(f"{путь.name}: вклеено {len(легло)} из {len(правки)}")
    мимо = [было for было in правки if было not in легло]
    if мимо:
        notify("не нашлись: " + "; ".join(f"«{о.splitlines()[0][:44]}»" for о in мимо[:6]))
    return легло




def meaning(обмен, notify=say):
    """Плоский пересказ настоящего ответа владельца — вход отвечающему агенту.

    Агент не может знать, что владелец хочет занять налички или что собеседник
    ему незнаком. Первый замер этим и подкосился: расхождение по смыслу тянуло
    за собой расхождение по признакам.
    """
    ответ = chr(10).join(
        m["text"] for m in обмен["reply"] if (m.get("text") or "").strip()
    )
    answer = _paid(
        f"смысл-{обмен['chat_id']}", "смысл",
        lambda: ask(prompt("смысл.md") + ответ, temperature=0.0, timeout=300, notify=notify),
        version("смысл.md"),
    )
    return " ".join(answer["text"].split())


def reply_as_owner(обмен, смысл, файл, notify=say):
    """Агент пишет ответ вместо владельца по файлу стиля.

    Регистр агент определяет сам: в работе его никто не подскажет, и узнавание
    — часть того, что меряется.

    Температура выше рабочей: манера у владельца сама по себе разбросана — по
    длине он сходится сам с собой в 30% случаев. Отвечать одним и тем же на всё
    значит показать не манеру, а среднее.
    """
    задание = prompt("ответ.md") + pathlib.Path(файл).read_text(encoding="utf-8")
    задание += chr(10) * 2 + "# Кому и на что отвечаем" + chr(10) * 2
    задание += trial.case(обмен, смысл)
    answer = _paid(
        f"ответ-{обмен['chat_id']}", "ответ",
        lambda: ask(задание, temperature=0.7, timeout=300, notify=notify),
        version("ответ.md"),
    )
    строки = [one.strip() for one in answer["text"].splitlines() if one.strip()]
    notify(f"{обмен['chat']}: сообщений в ответе {len(строки)}")
    return строки
