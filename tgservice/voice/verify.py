"""Проверка ответа агента по выгрузке: цитаты и полнота раскладки.

Модель склонна выдавать пересказ за прямую речь и молча пропускать половину
работы. Проверено на трёх пробных прогонах: из 45 оборотов семь были приписаны
к соседнему дню, один выдуман целиком, а из 1537 сообщений владельца в
раскладку попали 1386 — про остальные 151 агент не сказал ничего.

Ни то, ни другое само себя не покажет: ответ выглядит уверенно в любом случае.

К модели этот модуль не обращается: сверка идёт по выгрузке на диске.
"""

import re
from pathlib import Path

from .common import BATCHES

# Как агенты пишут цитаты. Формат в задании задан один, но на практике он
# гуляет: то со временем, то без, то ёлочками. Проверка обязана понимать все
# виды — иначе она отчитается «цитат нет» вместо «цитаты неверны», и это будет
# хуже, чем никакой проверки.
STAMPED = re.compile(
    r"^\s*(?:[-*]\s+)?`?(?P<day>\d{4}-\d{2}-\d{2})"
    r"(?:\s+(?P<time>\d{2}:\d{2}))?\s+(?P<who>[^:\n]{1,40}?):\s?(?P<text>.*?)`?\s*$"
)
ANGLED = re.compile(
    r"«(?P<text>[^»]+)»\s*\((?P<day>\d{4}-\d{2}-\d{2})(?:\s+(?P<time>\d{2}:\d{2}))?\)"
)
DAY_HEAD = re.compile(r"^#{0,4}\s*\**(?P<day>\d{4}-\d{2}-\d{2})\**\s*$")
BATCH_LINE = re.compile(r"^(?P<time>\d{2}:\d{2})\s+(?P<who>.+?):\s?(?P<text>.*)$")

OWNER = "я"


def flatten(text):
    """Текст без невидимых различий: краевые пробелы каждой строки убраны.

    В выгрузке сообщения лежат дословно, и владелец нередко заканчивает строку
    пробелом. Воспроизвести невидимый пробел агент не может, и требовать этого
    значит забраковать верную цитату как выдумку.
    """
    return "\n".join(line.strip() for line in (text or "").strip().splitlines())

# Что делать с находкой. Названия — это и есть решение, принятое заранее, чтобы
# не решать заново над каждой цитатой.
FIXED = "починено"  # отметка мимо, но текст в выгрузке один: правим молча
ASK = "переспросить"  # текст встречается несколько раз: угадывать нельзя
FOREIGN = "чужая речь"  # слова собеседника в банке владельца
INVENTED = "выдумано"  # такого текста в выгрузке нет вовсе
# Заглавная буква вместо строчной — не выдумка и не дословность. Своя пометка
# нужна потому, что без неё выбор был между двумя одинаково неверными: считать
# это подменой слова или не замечать вовсе. Обожглись на обоих.
CASED = "регистр"
# Цитата — кусок настоящего сообщения. Раньше засчитывалась как верная: слова
# ведь не подменены. Но «с утра созвонимся план сделаем» вместо «…с меня
# контент, с тебя код» меняет смысл на противоположный по сути — кто что делает.
# Формат батча к обрезке больше не подталкивает: продолжение отбито отступом,
# и в задании про это сказано. Значит обрезка — уже решение агента, а не
# следствие вёрстки, и считается ошибкой.
CUT = "обрезано"
# Несколько отдельных сообщений сведены агентом в одну цитату. Слова настоящие,
# все до одного, но сообщений было три, а не одно. Раньше такая цитата не
# находилась целиком и записывалась в выдумки — 291 раз на одном батче.
MERGED = "склеено"
# Склейка, в которую затесалась чужая реплика. Отдельно от «склеено»,
# потому что чинится иначе: там разнести по строкам, здесь ещё и выкинуть
# чужое. И отдельно от «выдумано»: слова в выгрузке есть, все до одного.
GLUED = "склеено с чужой"

# Пороги, после которых латать дороже, чем перегнать заново.
# Со скольких знаков цитата, совпавшая куском с настоящим сообщением,
# считается усечённой, а не случайно похожей. Ниже этого судить не о чем: «ок»
# найдётся в сотне мест и ничего этим не докажет.
PREFIX_MIN = 4

COVERAGE_OK = 0.95
COVERAGE_HOPELESS = 0.70
JUNK_HOPELESS = 0.10
# Доля без нижней границы врёт на мелком батче: одна выдумка из девяти цитат —
# это 11% и приговор «перегнать» из-за единственной строки. Перегон оправдан,
# когда брака много и по счёту, и по доле.
JUNK_LEAST = 5


class Batch:
    """Выгрузка одного батча, разложенная для поиска.

    Ключ поиска — текст сообщения: агент помнит слова точнее, чем отметку.
    Именно поэтому починка идёт от текста к дате, а не наоборот.
    """

    def __init__(self, text):
        self.at = {}  # (день, время) -> [(кто, текст)]
        self.by_text = {}  # текст без краевых пробелов -> [(день, время, кто)]
        self.by_lower = {}  # то же, но без учёта регистра и краевых пробелов в строках
        self.owner = []  # (день, время, текст) владельца, по порядку
        day = None
        rows = []
        for line in text.splitlines():
            head = DAY_HEAD.match(line.strip())
            if head:
                day = head.group("day")
                continue
            row = BATCH_LINE.match(line)
            if row and day:
                rows.append([day, row.group("time"), row.group("who").strip(), row.group("text")])
            elif rows and day and line.strip() and line.startswith((" ", "\t")):
                # Сообщение в несколько абзацев: продолжение идёт строкой без
                # отметки времени. Индексировать только первую строку значит
                # объявить выдумкой всё, что процитировано из остальных.
                #
                # Отступ — признак продолжения, и только он: его ставит сам
                # рендер батча (`batch.WRAP`), а вёрстка идёт от левого края.
                # Без этой проверки заголовок следующего собеседника в сборном
                # батче приклеивался к последнему сообщению предыдущего, и
                # верная цитата этого сообщения объявлялась выдумкой. Так
                # оболгано 294 сообщения в 25 сборных батчах.
                rows[-1][3] += "\n" + line.lstrip()
        # Порядок сообщений с обеими сторонами: без него не отличить серию
        # реплик владельца от ответа на входящее, а на этом стоят и дробление,
        # и задержка ответа.
        self.rows = [(day, time, who, body) for day, time, who, body in rows]
        for day, time, who, body in rows:
            self.at.setdefault((day, time), []).append((who, body))
            # Ключ без краевых пробелов: в выгрузке текст лежит дословно, а
            # владелец нередко заканчивает сообщение пробелом. Требовать от
            # агента воспроизвести невидимый пробел — значит забраковать
            # верную цитату.
            self.by_text.setdefault(flatten(body), []).append((day, time, who))
            # Отдельный указатель без регистра. Без него «Ихуани муслимин»,
            # процитированное строчными, объявляется выдумкой — а сказано оно
            # владельцем. На этом уже обожглись: разбор насчитал выдумок там,
            # где вся вина была в заглавной букве.
            self.by_lower.setdefault(flatten(body).lower(), []).append((day, time, who))
            if who == OWNER:
                self.owner.append((day, time, body))

    def _found(self, text, only_owner):
        text = flatten(text)
        exact = [
            (day, time, who)
            for day, time, who in self.by_text.get(text, [])
            if not only_owner or who == OWNER
        ]
        if exact or len(text) < PREFIX_MIN:
            return exact, False
        cased = [
            (day, time, who)
            for day, time, who in self.by_lower.get(text.lower(), [])
            if not only_owner or who == OWNER
        ]
        if cased:
            return cased, True
        return self._cut(text, only_owner)

    def _cut(self, text, only_owner):
        # Длинную реплику агент приводит куском: то началом, то серединой
        # («Брат че не спишь» → «че не спишь»). Обрезанная цитата — не выдумка,
        # и валить её в одну кучу с сочинённой значит топить проверку в ложных
        # тревогах. Порог длины бережёт от случайных совпадений.
        needle = text.lower()
        cut = [
            (day, time, who)
            for body, spots in self.by_lower.items()
            if needle in body
            for day, time, who in spots
            if not only_owner or who == OWNER
        ]
        return cut, bool(cut)

    def owner_said(self, text):
        """Где владелец говорил это. Пусто — не говорил."""
        spots, _ = self._found(text, True)
        return [(day, time) for day, time, _ in spots]

    def owner_said_cut(self, text):
        """Совпало только куском: сообщение длиннее того, что процитировано."""
        text = flatten(text)
        if self.by_lower.get(text.lower()):
            return False
        spots, cut = self._found(text, True)
        return bool(spots) and cut

    def only_case_differs(self, text):
        """Сообщение целиком то же, разошёлся только регистр.

        Цитата куском сюда не попадает: у неё несовпадение не в регистре, а в
        длине, и мерить её той же меркой значит объявить ошибкой обрезку.
        """
        return bool(self.by_lower.get(flatten(text).lower())) and not self.by_text.get(
            flatten(text)
        )

    def anyone_said(self, text):
        spots, _ = self._found(text, False)
        return spots

    def owner_said_in_parts(self, text):
        """Цитата — несколько подряд идущих сообщений, сведённых в одно.

        Серия реплик в одну минуту читается как одна мысль, и агент нередко
        сводит её в одну цитату. Слова при этом не подменены: каждая строка
        сказана владельцем, просто сообщений было несколько.

        Возвращает места всех частей, если каждая нашлась и все они на одном
        дне. Пусто — значит цитата не склейка, а что-то другое.
        """
        parts = [line for line in flatten(text).split("\n") if line]
        if len(parts) < 2:
            return []
        spots = []
        for part in parts:
            said = self.by_text.get(part) or self.by_lower.get(part.lower())
            mine = [(day, time) for day, time, who in (said or []) if who == OWNER]
            if not mine:
                return []
            spots.append(mine)
        days = {day for first in spots for day, _ in first}
        if len(days) > len(parts):  # разбросано по всему году — это не серия
            return []
        return [group[0] for group in spots]

    def parts_speakers(self, text):
        """Кто сказал каждую строку склейки. `None` — хотя бы одной нет в выгрузке.

        Задание требует цитировать многострочное сообщение целиком, и, надавив
        на это правило, получили обратное: агент стал склеивать соседние
        сообщения, в том числе чужие. На пробном батче так вышло двенадцать
        цитат, и все звались «выдумано» — переспрос просил найти текст,
        которого и не было, вместо «выкинь чужую строку».

        Часть ищется как строка, а не как сообщение целиком: склеить можно и
        строку из середины длинной реплики. Первая версия искала по точному
        тексту сообщения и потому не узнала ни одной такой склейки.
        """
        parts = [line for line in flatten(text).split("\n") if line]
        if len(parts) < 2:
            return None
        told = []
        for part in parts:
            said = self.anyone_said(part)
            if not said:
                return None  # части нет в выгрузке вовсе — это уже выдумка
            told.append((part, any(who == OWNER for _, _, who in said)))
        return told


def load(path):
    """Батч с диска. Имя файла годится вместо полного пути."""
    place = Path(path)
    if not place.exists():
        place = BATCHES / path
    return Batch(place.read_text(encoding="utf-8"))


def corpus():
    """Все батчи одной выгрузкой: по ней судятся цитаты свода.

    У свода своего батча нет — он сведён из многих, а на верхних уровнях и из
    других сводов. Поэтому выше батча цитаты не проверял никто, и уехавшая
    отметка доезжала до файла стиля нетронутой.

    Сборка идёт секунду на сотне тысячах сообщений: держать её в памяти дешевле,
    чем читать батчи заново на каждую цитату.
    """
    return Batch("\n".join(
        place.read_text(encoding="utf-8") for place in sorted(BATCHES.glob("*.md"))
    ))


SOURCED = re.compile(r"^(\s*)\S+\.md\s+(?=\d{4}-\d{2}-\d{2}\s)")


def unsourced(text):
    """Свод без имени батча перед цитатой: иначе строка не читается как цитата.

    В описании цитата начинается с отметки, а в своде перед ней стоит имя
    батча — оно нужно, чтобы вернуться к источнику. Для суда над цитатой это
    помеха: `STAMPED` ждёт дату в начале строки.
    """
    return "\n".join(SOURCED.sub(r"\1", line) for line in text.splitlines())


def quotes(answer):
    """Цитаты из ответа агента: (день, время, кто, текст) в порядке появления."""
    return [quote for _, quote in _scan(answer)]


def _scan(answer):
    """То же, что `quotes`, но с номером строки, на которой стоит отметка.

    Один разбор на всех, кто читает ответ агента. Пока их было два, `repair`
    судил только первую строку многострочной цитаты: по ней текст встречается
    во многих местах, приговор выходил «переспросить», и уехавшая отметка
    оставалась непочиненной.

    Многострочное сообщение агент выводит так же, как оно стоит в батче:
    первая строка с отметкой, продолжение с отступом. Не склеив их, проверка
    объявила бы обрезанным каждое такое сообщение — а обрезки в нём нет.
    """
    found = []
    where = ""
    open_ = False
    for number, line in enumerate(answer.splitlines()):
        if line.lstrip().startswith("#"):
            where = line.strip("# ").strip()
            open_ = False
        # Строка раздражителя помечена «←» — это цитата собеседника. Без
        # разбора метки её отступное продолжение приклеивалось к предыдущей
        # цитате владельца и рождало ложное «склеено с чужой».
        core = line.lstrip()[1:].lstrip() if line.lstrip().startswith("←") else line
        angled = ANGLED.search(core)
        if angled:
            found.append(
                [number, [angled.group("day"), angled.group("time"), None,
                          angled.group("text").strip(), where]]
            )
            open_ = True
            continue
        stamped = STAMPED.match(core)
        if stamped and stamped.group("text").strip():
            found.append(
                [
                    number,
                    [
                        stamped.group("day"),
                        stamped.group("time"),
                        stamped.group("who").strip(),
                        stamped.group("text").strip(),
                        where,
                    ],
                ]
            )
            open_ = True
            continue
        # Отступ — признак продолжения, и только он: проза в ответе идёт от
        # левого края. Продолжение идёт сразу за цитатой: строка от левого
        # края закрывает её, и отступный список после прозы — не продолжение.
        if found and open_ and line.startswith((" ", "\t")) and line.strip():
            found[-1][1][3] += "\n" + line.strip()
        elif line.strip():
            open_ = False
    return [(number, tuple(row)) for number, row in found]


LAYOUT = "асклад"  # «Раскладка сообщений по блокам» — глава, где место цитате целиком


def judge(quote, batch):
    """Что не так с цитатой и что с этим делать. `None` — всё в порядке.

    Обрезка считается ошибкой только в раскладке: там сообщение обязано стоять
    целиком. Под блоком приводится **оборот**, и короткий кусок длинной реплики
    там уместен — это и есть оборот, а не обрезанное сообщение. Раньше мерились
    одной меркой, и из 38 «обрезок» на сборном батче 28 были законными.
    """
    day, time, who, text = quote[:4]
    where = quote[4] if len(quote) > 4 else ""
    if who is not None and who != OWNER:
        # Речь собеседника агент приводит как контекст — это разрешено, и
        # проверять её надо на дословность, но не на принадлежность владельцу.
        seen = batch.anyone_said(text)
        named = [(d, t) for d, t, w in seen if w == who]
        if any(d == day and (time is None or t == time) for d, t in named):
            return None
        if not seen:
            return {"verdict": INVENTED, "text": text, "said": day, "who": who}
        # Текст в выгрузке есть, но говорящий подписан иначе — «Санжар» вместо
        # «Санжар Брат». Это орфография имени, а не выдумка: за выдумки батч
        # уезжает в перегон, то есть во вторую покупку разбора.
        return {"verdict": ASK, "text": text, "said": day, "who": who,
                "seen": [(d, t) for d, t, _ in seen][:5]}

    said = batch.owner_said(text)
    here = [(d, t) for d, t in said if d == day and (time is None or t == time)]
    if here:
        # Место сошлось. Осталось решить, дословна ли сама цитата: заглавная
        # буква вместо строчной — не ошибка места, а ошибка передачи.
        if batch.only_case_differs(text):
            return {"verdict": CASED, "text": text, "said": day, "should": here[0]}
        if LAYOUT in where.lower() and batch.owner_said_cut(text):
            return {"verdict": CUT, "text": text, "said": day, "should": here[0]}
        return None
    if not said:
        told = batch.parts_speakers(text)
        if told:
            alien = [part for part, mine in told if not mine]
            if alien:
                return {"verdict": GLUED, "text": text, "said": day, "чужое": alien}
            return {
                "verdict": MERGED,
                "text": text,
                "said": day,
                "parts": batch.owner_said_in_parts(text) or [part for part, _ in told],
            }
        elsewhere = batch.anyone_said(text)
        if elsewhere:
            return {"verdict": FOREIGN, "text": text, "said": day, "seen": elsewhere[:5]}
        return {"verdict": INVENTED, "text": text, "said": day}
    if len(said) == 1:
        return {"verdict": FIXED, "text": text, "said": day, "should": said[0]}
    return {"verdict": ASK, "text": text, "said": day, "seen": said[:5]}


def coverage(answer, batch):
    """Сколько сообщений владельца доехало до раскладки и какие потерялись.

    Считается по главам раскладки и по безымянному месту до первой главы.
    Оборот под блоком раскладке не зачитывается: короткий пример помечал
    «разложенными» сообщения, которых в раскладке нет, и завышенная полнота
    гасила нужный переспрос.
    """
    laid = set()
    for day, time, who, text, where in quotes(answer):
        if who not in (None, OWNER):
            continue
        if where and LAYOUT not in where.lower():
            continue
        # Засчитывается то место, на которое агент показал, а не все повторы
        # этой фразы: процитировав «Салем брат» один раз, он не разложил тем
        # самым все остальные приветствия за год.
        for spot in batch.owner_said(text):
            if spot[0] == day and (time is None or spot[1] == time):
                laid.add(spot)
        # Склеенная цитата — это несколько разложенных сообщений в одной
        # строке. Не засчитать их значит объявить пропущенным то, что на месте.
        for spot in batch.owner_said_in_parts(text):
            laid.add(spot)
    missing = [row for row in batch.owner if (row[0], row[1]) not in laid]
    total = len(batch.owner)
    return {
        "total": total,
        "laid": total - len(missing),
        "share": (total - len(missing)) / total if total else 1.0,
        "missing": missing,
    }


def check(answer, batch):
    """Полный разбор ответа: находки по цитатам и полнота раскладки.

    Полнота меряется по починенному ответу, а не по исходному. Сообщение с
    уехавшей отметкой разложено — просто подписано не тем днём, и считать его
    пропущенным неверно: на одном батче это занижало полноту с 95% до 66%.
    """
    found = quotes(answer)
    notes = [note for note in (judge(quote, batch) for quote in found) if note]
    counts = {}
    for note in notes:
        counts[note["verdict"]] = counts.get(note["verdict"], 0) + 1
    mended, _ = repair(answer, batch)
    spread = coverage(mended, batch)
    bad = counts.get(INVENTED, 0) + counts.get(FOREIGN, 0) + counts.get(GLUED, 0)
    junk_share = bad / len(found) if found else 0.0
    hopeless = bad >= JUNK_LEAST and junk_share > JUNK_HOPELESS
    if spread["share"] < COVERAGE_HOPELESS or hopeless:
        verdict = "перегнать"
    elif notes or spread["share"] < COVERAGE_OK:
        verdict = "переспросить"
    else:
        verdict = "принято"
    return {
        "quotes": len(found),
        "counts": counts,
        "notes": notes,
        "coverage": spread,
        "verdict": verdict,
    }


def repair(answer, batch):
    """Ответ с починенными отметками. Спорное и выдуманное не трогаем.

    Чинится только то, где текст в выгрузке встречается один раз: тогда
    подстановка не догадка, а единственный возможный ответ. Всё остальное
    уходит на переспрос — угадывать за агента нельзя.
    """
    fixes = []
    lines = answer.splitlines()
    for number, quote in _scan(answer):
        note = judge(quote, batch)
        if not note or note["verdict"] != FIXED:
            continue
        day, time = note["should"]
        line = lines[number]
        lines[number] = line.replace(quote[0], day, 1)
        if quote[1] and quote[1] != time:
            lines[number] = lines[number].replace(quote[1], time, 1)
        fixes.append({"text": quote[3], "was": quote[0], "now": day})
    return "\n".join(lines), fixes


def ask_again(result, limit=40):
    """Письмо агенту: что именно переделать. Адресно, а не «сделай заново».

    Полная перегонка стоит дороже и теряет то, что агент уже сделал верно.
    """
    lines = []
    bad = [note for note in result["notes"] if note["verdict"] != FIXED]
    if bad:
        lines += [
            "Эти цитаты не сошлись с выгрузкой. Приведи их дословно и с верной",
            "отметкой либо убери — придумывать нельзя.",
            "",
        ]
        for note in bad[:limit]:
            if note["verdict"] == INVENTED:
                lines.append(f"- «{note['text']}» — такого текста в выгрузке нет вовсе")
            elif note["verdict"] == CASED:
                lines.append(f"- «{note['text']}» — верно по месту, но не по буквам: сверь регистр")
            elif note["verdict"] == MERGED:
                lines.append(
                    f"- «{note['text'][:60]}…» — это {len(note['parts'])} отдельных сообщения,"
                    " а не одно: приведи каждое своей строкой со своим временем"
                )
            elif note["verdict"] == CUT:
                lines.append(
                    f"- «{note['text']}» — обрезано: сообщение длиннее, приведи целиком"
                )
            elif note["verdict"] == GLUED:
                чужое = "; ".join(f"«{one}»" for one in note["чужое"][:3])
                lines.append(
                    f"- «{note['text'][:60]}…» — сюда затесались слова собеседника:"
                    f" {чужое}. Разнеси по строкам и оставь только речь владельца"
                )
            elif note["verdict"] == FOREIGN:
                lines.append(f"- «{note['text']}» — это слова собеседника, не владельца")
            else:
                where = ", ".join(f"{d} {t}" for d, t, *_ in note.get("seen", []))
                lines.append(f"- «{note['text']}» — встречается несколько раз: {where}. Какой имелся в виду?")
        lines.append("")
    spread = result["coverage"]
    if spread["share"] < COVERAGE_OK:
        lines += [
            f"В раскладку не попали {len(spread['missing'])} сообщений владельца из"
            f" {spread['total']}. Разнеси их по блокам или назови в главе",
            "«что осталось за блоками». Молча пропускать нельзя.",
            "",
        ]
        for day, time, text in spread["missing"][:limit]:
            lines.append(f"- {day} {time} я: {text[:80]}")
        if len(spread["missing"]) > limit:
            lines.append(f"- …и ещё {len(spread['missing']) - limit}")
    return "\n".join(lines)


# Пометки агент оформляет как хочет: жирным, в бэктиках, буллетом. STAMPED
# понимает все виды цитат, а PATCH не понимал — непустое письмо давало
# «внесено 0, не найдено 0», и это считалось успехом.
PATCH = re.compile(
    r"^\s*(?:[-*]\s+)?[`*]*(?P<mark>БЫЛО|СТАЛО)[`*]*:\s*\**\s?(?P<line>.*?)`?\s*$"
)

# Отступ продолжения при вклейке — тот же, что у рендера батча: по нему
# `_scan` узнаёт продолжение цитаты.
WRAP = "    "


def _pairs(patch):
    """Пары «было — стало» из письма агента. Обе стороны многострочны.

    Продолжение цитаты идёт отступной строкой без пометки — терять его значит
    не найти пару в разборе. Проза письма от левого края пары не продолжает.
    """
    pairs, olds, news, side = [], None, [], None
    for line in patch.splitlines():
        found = PATCH.match(line)
        if found and found.group("mark") == "БЫЛО":
            if olds is not None:
                pairs.append((olds, news))
            olds, news, side = [found.group("line").strip()], [], "было"
        elif found:
            if olds is None:
                continue
            news.append(found.group("line").strip())
            side = "стало"
        elif side and line.strip() and line.startswith((" ", "\t")):
            (olds if side == "было" else news).append(line.strip())
        elif line.strip():
            side = None
    if olds is not None:
        pairs.append((olds, news))
    return pairs


def patch_pairs(patch):
    """Сколько пар разобралось из письма. Ноль из непустого — провал формата."""
    return len(_pairs(patch))


def apply_patch(answer, patch):
    """Заплатка от агента, вклеенная в его же разбор.

    Переспрос сперва просил вернуть разбор целиком — агент возвращал, попутно
    выбрасывая четыреста цитат из полутора тысяч. Ошибки чинились, материал
    терялся. Поэтому теперь он присылает только пары «было — стало», а
    подстановкой занимается скрипт: что не названо, то не тронуто.

    Строка замены без отметки времени — продолжение и встаёт с отступом,
    иначе `_scan` не признает её продолжением и сверка снова увидит
    «обрезано». Строка со своей отметкой — самостоятельное сообщение, ей
    отступ нельзя. Удаление многострочной цитаты сносит и отступные хвосты:
    оставшись, они приклеивались к предыдущей цитате.
    """
    lines = answer.splitlines()
    done, missed = 0, []
    for old, new in _pairs(patch):
        replacement = [] if new in ([], ["—"], ["-"]) else new
        span = len(old)
        for number in range(len(lines) - span + 1):
            if [one.strip() for one in lines[number : number + span]] == old:
                indent = lines[number][: len(lines[number]) - len(lines[number].lstrip())]
                built = [
                    indent + one if i == 0 or STAMPED.match(one) else indent + WRAP + one
                    for i, one in enumerate(replacement)
                ]
                end = number + span
                if not replacement:
                    while end < len(lines) and lines[end].startswith((" ", "\t")) and lines[end].strip():
                        end += 1
                lines[number:end] = built
                done += 1
                break
        else:
            missed.append(old[0])
    return "\n".join(lines), {"внесено": done, "не найдено": missed}


def report(result):
    """Человеку в руки: что нашли и что решили."""
    spread = result["coverage"]
    lines = [
        f"Цитат в ответе: {result['quotes']}",
        f"Раскладка: {spread['laid']} из {spread['total']}"
        f" ({100 * spread['share']:.0f}%)",
        "",
    ]
    if result["counts"]:
        lines.append("Находки:")
        for verdict, count in sorted(result["counts"].items(), key=lambda row: -row[1]):
            lines.append(f"  {verdict}: {count}")
    else:
        lines.append("К цитатам вопросов нет.")
    lines += ["", f"Решение: {result['verdict']}"]
    return "\n".join(lines)
