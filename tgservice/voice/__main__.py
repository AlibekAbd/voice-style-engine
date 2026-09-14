"""Запуск шагов сбора из командной строки."""

import argparse
import asyncio
import pathlib
import sys
import urllib.error

from .. import telegram
from . import analyse
from . import collect
from . import verify as verify_step
from .assemble import build, measure
from .common import DAYS, say


def _positive(value):
    """Ноль потоков доезжал до пула и ронял прогон уже после стартовой строки."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("потоков должно быть хотя бы один")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m tgservice.voice",
        description="сбор переписки для разбора манеры речи",
    )
    parser.add_argument(
        "step",
        choices=[
            "fetch", "build", "measure", "lengths", "groups", "verify",
            "разбор", "разбор-все", "переспрос", "проверка",
            "прогон", "прогон-все", "дочистка", "дочистка-все",
            "all",
        ],
    )
    parser.add_argument("--batch", help="какой батч разбирать или по какому сверять")
    parser.add_argument("--answer", help="ответ агента для проверки")
    parser.add_argument("--fix", action="store_true", help="починить отметки на месте")
    parser.add_argument(
        "--workers",
        type=_positive,
        default=analyse.WORKERS,
        help="сколько батчей читать одновременно",
    )
    parser.add_argument(
        "--rounds", type=_positive, default=2,
        help="сколько кругов переспроса тратить на батч",
    )
    parser.add_argument("--days", type=int, default=DAYS, help="окно в днях")
    parser.add_argument("--restart", action="store_true", help="выгрузить всё заново")
    parser.add_argument("--only", help="только названные чаты, по началу имени")
    args = parser.parse_args(argv)
    try:
        if args.step in ("fetch", "all"):
            asyncio.run(collect.fetch(days=args.days, restart=args.restart, only=args.only))
        if args.step in ("build", "all"):
            build()
        if args.step == "lengths":
            asyncio.run(collect.lengths())
            build()  # длительности видны только в перенарезанных батчах
        if args.step == "groups":
            asyncio.run(collect.groups())
        if args.step == "verify":
            if not (args.answer and args.batch):
                raise LookupError("нужны --answer и --batch")
            answer = pathlib.Path(args.answer).read_text(encoding="utf-8")
            batch = verify_step.load(args.batch)
            result = verify_step.check(answer, batch)
            say(verify_step.report(result))
            letter = verify_step.ask_again(result)
            if letter:
                place = pathlib.Path(args.answer).with_suffix(".ask.md")
                place.write_text(letter, encoding="utf-8")
                say(f"что переспросить — {place}")
            if args.fix:
                mended, fixes = verify_step.repair(answer, batch)
                pathlib.Path(args.answer).write_text(mended, encoding="utf-8")
                say(f"отметок починено: {len(fixes)}")
        if args.step in ("measure", "all"):
            measure()
        if args.step in ("разбор", "переспрос", "проверка", "прогон", "дочистка"):
            if not args.batch:
                raise LookupError("нужен --batch: какой батч разбирать")
        if args.step == "разбор":
            analyse.review(args.batch)
        if args.step == "разбор-все":
            analyse.all_reviews(workers=args.workers)
        if args.step == "проверка":
            analyse.audit(args.batch)
        if args.step == "прогон":
            say(verify_step.report(analyse.run_batch(args.batch)))
        if args.step == "прогон-все":
            analyse.all_batches(workers=args.workers)
        if args.step == "дочистка":
            say(verify_step.report(analyse.polish(args.batch, rounds=args.rounds)))
        if args.step == "дочистка-все":
            analyse.all_polish(rounds=args.rounds, workers=args.workers)
        if args.step == "переспрос":
            # Тот же клей, что и в прогоне: две копии этой
            # последовательности уже успели разойтись в поведении.
            say(verify_step.report(
                analyse.mend(args.batch, verify_step.load(args.batch))
            ))
    except (
        telegram.SessionGone,
        LookupError,
        analyse.Empty,
        urllib.error.URLError,
    ) as error:
        say(str(error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
