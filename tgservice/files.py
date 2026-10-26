"""Файлы памяти на диске: JSON целиком и журналы JSONL построчно.

Общие для всех частей пакета: память отправок ядра, ступени разведки и
суточный круг читают и пишут свои файлы одними и теми же функциями.
"""
import json
from pathlib import Path


def read_json(path, default=None):
    """Читает файл памяти. Нет файла — пусто; битый — отказ, а не пусто.

    Молча начать с чистого листа значит потерять накопленное и не заметить
    этого.
    """
    path = Path(path)
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError(f"{path} не читается: {error}") from error


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def read_lines(path):
    """Строки журнала. Недописанная последняя пропускается: обрыв прогона
    оставляет обрубок, и одна порченая строка не стоит всего журнала.

    Битая строка не в конце — отказ. Обрубок бывает только последним; порча
    внутри означает чужой формат или правку руками, и молча пропущенные строки
    превращают журнал отправок в «никому не писали». Проверено 08.09.2026 по
    всем журналам на диске: 233 527 строк, битых нет — правило ничего живого
    не ломает.
    """
    path = Path(path)
    if not path.exists():
        return []
    строки = [(номер, line) for номер, line in
              enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
              if line.strip()]
    rows = []
    for место, (номер, line) in enumerate(строки):
        try:
            rows.append(json.loads(line))
        except ValueError as error:
            if место == len(строки) - 1:
                continue
            raise ValueError(
                f"{path}: строка {номер} не читается, а за ней есть ещё — "
                f"это не обрубок прогона, а порча журнала: {error}"
            ) from error
    return rows
