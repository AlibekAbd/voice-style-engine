"""Шаги, которые ходят в Телеграм: выгрузка, длительности, обзор групп.

Всё, что здесь есть, требует сети и живой сессии. Отбор и нарезка считаются по
тому, что эти шаги положили на диск, и в Телеграм уже не ходят.
"""

from datetime import datetime, timezone

from .. import telegram
from . import tg
from .common import (
    DAYS,
    FETCH_STATE,
    GROUPS_TAKEN,
    LIVE_INDEX,
    RAW,
    RAW_INDEX,
    SMALL_GROUPS,
    VOICE,
    read_json,
    say,
    since_of,
    write_json,
    _now,
)

# Наговоренное голосом. Своё в разбор не идёт, входящее остаётся раздражителем.
VOICE_KINDS = ("голосовое", "кружок")

# Обзор показывает **все** группы: отбирает владелец, и отбирать он должен из
# полного списка. Прежний потолок в пятнадцать участников скрыл 48 групп, и
# среди них оказались ровно те, что владелец потом попросил взять, — поэтому
# порога на показе нет вовсе.
#
# А в сбор группа крупнее этого не берётся: решение владельца. Там уже не свои,
# а публика, и манера в такой группе не личная.
GROUP_CAP = 80


async def _collect(app, targets, state, since, notify=say):
    """Обход чатов с продолжением после обрыва: готовое пропускается."""
    done = state.setdefault("chats", {})
    for number, target in enumerate(targets, 1):
        chat_id = target["id"]
        title = target["title"]
        if done.get(chat_id, {}).get("status") == "done":
            continue
        head = f"[{number}/{len(targets)}] {title}"
        try:
            rows = await tg.speech(
                app,
                target["entity"],
                since=since,
                in_group=target["kind"] == "group",
                notify=lambda text: notify(f"{head}: {text}"),
            )
        except (telegram.FloodStop, telegram.ChatClosed) as error:
            notify(f"{head}: отложен — {error}")
            done[chat_id] = {"title": title, "status": "failed", "why": str(error)}
            write_json(FETCH_STATE, state)
            continue
        rows.reverse()  # Телеграм отдаёт от свежих к старым, читать надо наоборот
        mine = sum(1 for row in rows if row["out"])
        record = {
            "meta": {
                "id": chat_id,
                "name": title,
                "username": target.get("username"),
                "phone": target.get("phone"),
                "kind": target["kind"],
                "archived": target.get("archived"),
                "msgs": len(rows),
                "mine": mine,
                "theirs": len(rows) - mine,
                "first": rows[0]["date"] if rows else None,
                "last": rows[-1]["date"] if rows else None,
                "since": since.isoformat()[:16],
            },
            "messages": rows,
        }
        write_json(RAW / f"{chat_id}.json", record)
        done[chat_id] = {
            "title": title,
            "status": "done",
            "msgs": len(rows),
            "at": _now().isoformat()[:16],
        }
        write_json(FETCH_STATE, state)
        if rows:
            notify(f"{head}: {len(rows)}")


def taken_groups():
    """Группы, взятые в сбор, — по идентификаторам.

    Именами их брать нельзя: в списке малолюдных групп имена повторяются.
    «Цех», «Фотки», «Клуб» встречаются дважды и трижды — это разные чаты,
    обычно старая группа и супергруппа, в которую она переехала. Выбор именем
    молча взял бы не тот.
    """
    stored = read_json(GROUPS_TAKEN)
    if stored:
        return {row["id"]: row["title"] for row in stored["groups"]}
    return {}


async def _targets(app):
    """Что выгружаем: все личные диалоги плюс взятые в сбор группы."""
    found = await telegram.people(app)
    wanted = taken_groups()
    seen = set()
    async for dialog, archived in telegram.dialogs(app):
        if dialog.is_user or str(dialog.id) not in wanted:
            continue
        seen.add(str(dialog.id))
        found.append(
            {
                "id": str(dialog.id),
                "title": dialog.name,
                "username": getattr(dialog.entity, "username", None),
                "phone": None,
                "kind": "group",
                "archived": archived,
                "entity": dialog.entity,
            }
        )
    missing = [f"{title} ({key})" for key, title in wanted.items() if key not in seen]
    if missing:
        raise LookupError(f"не нашлись группы: {', '.join(missing)}")
    return found


def dump_window():
    """Окно, по которому собрана лежащая на диске выгрузка.

    Берётся самое частое: чат, перечитанный под другое окно, окном всей
    выгрузки не становится. Если такой чат есть — он не совпадёт с общим и
    будет перечитан заново, чем несовпадение и вылечится.
    """
    seen = {}
    for path in RAW.glob("*.json"):
        moment = read_json(path)["meta"].get("since")
        if moment:
            seen[moment] = seen.get(moment, 0) + 1
    if not seen:
        return None
    return max(seen, key=seen.get)


def _recover(state):
    """Готовые чаты, восстановленные по самой выгрузке.

    Состояние — удобство, а не источник правды: правда лежит в `raw`. Потерянный
    или обнулённый файл состояния не повод перечитывать год заново, поэтому
    готовое опознаётся по файлам с тем же окном.
    """
    done = state.setdefault("chats", {})
    for path in RAW.glob("*.json"):
        record = read_json(path)
        meta = record["meta"]
        if meta["id"] in done or meta.get("since") != state["since"]:
            continue
        done[meta["id"]] = {
            "title": meta["name"],
            "status": "done",
            "msgs": meta["msgs"],
            "at": "восстановлено по выгрузке",
        }
    return state


async def fetch(days=DAYS, restart=False, only=None, since=None):
    """Выгрузка личных чатов и взятых групп за окно."""
    VOICE.mkdir(parents=True, exist_ok=True)
    state = read_json(FETCH_STATE, {}) or {}
    # Окно ставится один раз и живёт в состоянии. Считать его от «сегодня» на
    # каждом запуске значит однажды прийти на другой день и обнулить годовую
    # выгрузку из-за суток дрейфа: год есть год, сутки его не меняют.
    fresh = restart or since is not None
    if fresh:
        state = {"since": (since or since_of(days)).isoformat()[:16], "days": days, "chats": {}}
    elif not state.get("chats"):
        # Состояния нет или оно пустое, а выгрузка на диске есть. Окно тогда
        # берётся у неё: навязать ей сегодняшнее значило бы перечитать год
        # из-за суток дрейфа календаря.
        state = {
            "since": dump_window() or since_of(days).isoformat()[:16],
            "days": days,
            "chats": {},
        }
    since = datetime.fromisoformat(state["since"]).replace(tzinfo=timezone.utc)
    if not fresh:
        # При перезапуске восстановление не делается: окно совпадает до
        # минуты, и все файлы «восстановились бы по выгрузке» — --restart в
        # день исходной выгрузки выходил холостым при справке «выгрузить всё
        # заново».
        _recover(state)
        if days != DAYS and state.get("days") != days:
            say(f"окно уже стоит с {state['since']}: --days {days} без --restart не применяется")
    app = await telegram.queued(notify=say)
    try:
        targets = await _targets(app)
        if only:
            names = [name.strip().lower() for name in only.split(",") if name.strip()]
            targets = [
                row
                for row in targets
                if any((row["title"] or "").lower().startswith(name) for name in names)
            ]
            if not targets:
                raise LookupError(f"по именам «{only}» ничего не нашлось")
        ready = sum(1 for row in state["chats"].values() if row["status"] == "done")
        say(f"чатов в обходе: {len(targets)}, окно с {since.date()}, уже готово {ready}")
        await _collect(app, targets, state, since)
    finally:
        await app.disconnect()
    write_json(FETCH_STATE, state)
    failed = [row for row in state["chats"].values() if row["status"] != "done"]
    say(f"выгружено чатов: {len(state['chats']) - len(failed)}, отложено: {len(failed)}")
    for row in failed:
        say(f"  не взят: {row['title']} — {row.get('why')}")
    # Сироты: файлы чатов, выпавших из обхода. Владелец убрал группу из
    # groups_taken.json, а её старый файл лежал и штатно уезжал в батчи —
    # решение об исключении молча не исполнялось. При «--only» обход неполон,
    # и судить о сиротах по нему нельзя.
    if not only:
        for path in orphans(state):
            pen = RAW / "вне-обхода"
            pen.mkdir(exist_ok=True)
            path.rename(pen / path.name)
            say(f"вне обхода: {path.name} — убран в raw/вне-обхода/")
    _index()


def _voiced(messages):
    """Номера входящих голосовых, у которых длительность ещё не спрошена.

    Спрошенное и не отвеченное несёт `secs: null` — удалённое сообщение
    длительность не отдаст никогда, и без пометки его переспрашивали бы
    каждым прогоном.
    """
    return [
        row["mid"]
        for row in messages
        if not row["out"] and row.get("media") in VOICE_KINDS and "secs" not in row
    ]


def orphans(state):
    """Файлы выгрузки, чьих чатов в обходе больше нет."""
    known = set(state.get("chats", {}))
    return [
        path
        for path in sorted(RAW.glob("*.json"))
        if read_json(path)["meta"]["id"] not in known
    ]


def _lay(messages, ids, found):
    """Кладёт длительности в сообщения; спрошенное без ответа помечает."""
    for row in messages:
        if row["mid"] in found:
            row["secs"] = found[row["mid"]]
        elif row["mid"] in ids:
            row["secs"] = None


async def lengths():
    """Длительности, которых в выгрузке не хватает, — досылаются запросом.

    **Обычному прогону этот шаг не нужен:** длительность пишется прямо при
    чтении чата, в том же проходе. Он остаётся починкой для выгрузок, снятых
    до того, как это завелось, — перечитывать ради одного поля год значит
    сорок минут против минуты.
    """
    live = {row["id"] for row in (read_json(LIVE_INDEX, {}) or {}).get("live", [])}
    if not live:
        raise LookupError("нет отбора живых: сначала python -m tgservice.voice build")
    work = []
    for path in sorted(RAW.glob("*.json")):
        record = read_json(path)
        if record["meta"]["id"] not in live:
            continue
        ids = _voiced(record["messages"])
        if ids:
            work.append((path, record, ids))
    if not work:
        say("длительности уже проставлены")
        return
    say(f"голосовых без длительности: {sum(len(ids) for _, _, ids in work)} в {len(work)} чатах")
    app = await telegram.queued(notify=say)
    try:
        done = 0
        for path, record, ids in work:
            entity = await telegram.entity_by_id(app, int(record["meta"]["id"]))
            found = await tg.lengths(app, entity, ids, notify=say)
            _lay(record["messages"], ids, found)
            write_json(path, record)
            done += len(found)
        say(f"длительностей проставлено: {done}")
    finally:
        await app.disconnect()


async def groups():
    """Все группы владельца: из них он отбирает, что взять в сбор.

    Отсеивать до показа нельзя. Пробовал — поставил потолок в пятнадцать
    участников, и за ним осталось сорок восемь групп, среди которых были нужные.
    Число участников печатается, чтобы решать было по чему, но решение за
    владельцем.
    """
    app = await telegram.queued(notify=say)
    found = []
    try:
        async for dialog, archived in telegram.dialogs(app):
            if not dialog.is_group:
                continue
            # Число участников Телеграм отдаёт не всегда. Раньше группа без
            # числа молча пропускалась — и не показывалась владельцу вовсе.
            count = getattr(dialog.entity, "participants_count", None)
            found.append(
                {
                    "id": str(dialog.id),
                    "title": dialog.name,
                    "participants": count,
                    "archived": archived,
                    "taken": str(dialog.id) in taken_groups(),
                }
            )
    finally:
        await app.disconnect()
    found.sort(key=lambda row: (row["participants"] is None, row["participants"] or 0))
    write_json(SMALL_GROUPS, {"found": found})
    say(f"групп всего: {len(found)}")
    for number, row in enumerate(found, 1):
        size = "?" if row["participants"] in (None, 0) else str(row["participants"])
        mark = " — уже в сборе" if row["taken"] else ""
        if (row["participants"] or 0) > GROUP_CAP:
            mark += f" — крупнее {GROUP_CAP}, в сбор не берётся"
        say(f"  {number:>3}. {size:>5} чел.  {row['title']}{mark}")
    return found


def _index():
    """Свод по выгрузке: что за чаты и сколько в них сообщений."""
    rows = []
    for path in sorted(RAW.glob("*.json")):
        meta = read_json(path)["meta"]
        rows.append(meta)
    rows.sort(key=lambda row: row["msgs"], reverse=True)
    write_json(RAW_INDEX, {"chats": len(rows), "list": rows})
    return rows
