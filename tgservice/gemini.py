"""Запросы к Gemini: строгая схема ответа, повторы, учёт токенов."""

import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://generativelanguage.googleapis.com/v1beta/models"
ATTEMPTS = 3
TIMEOUT = 300
# Повторяем только то, что проходит само: таймаут, лимит запросов, сбой на стороне Google.
RETRY_CODES = (408, 429, 500, 502, 503, 504)
# Отказы уровня соединения: до Google не дошло, кода нет и жалобы тоже.
# urllib.error.URLError оборачивает и отвалившийся DNS, и отказ сокета,
# socket.timeout с версии 3.10 — это TimeoutError, обрыв ответа приходит из http.client.
NETWORK_FAILURES = (
    urllib.error.URLError,
    TimeoutError,
    ConnectionError,
    http.client.HTTPException,
)
# Сети нет — ждать её стоит дольше, чем ответа перегруженного сервера: пропажа
# провайдера, переподключение VPN или перезагрузка роутера укладываются в
# несколько минут. Шесть попыток с отступлением ниже ждут суммарно 5 минут 45
# секунд — дольше любого короткого обрыва и заметно меньше получаса.
NETWORK_ATTEMPTS = 6
NETWORK_BACKOFF = (15, 30, 60, 120, 120)
# Окно моделей — около миллиона токенов, на русском это примерно три символа на токен.
MAX_PROMPT_CHARS = 2_000_000
# Потолок вывода у моделей — 65 536 токенов, берём его целиком: сам лимит не оплачивается,
# платим за выданное, а разметка большого чата вместе с рассуждением легко перерастает
# свои 12 тысяч. Задан явно, чтобы поведение не менялось от смены умолчаний у Google.
MAX_OUTPUT_TOKENS = 65_536
# Причины, по которым модель промолчала: контента в кандидате не будет вовсе.
REFUSAL_REASONS = ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST")
# Суточную квоту Google называет по-разному, общее — упоминание дня.
DAILY_QUOTA_MARKS = ("perday", "per day", "daily")

ModelCheck = namedtuple("ModelCheck", "present missing")


class GeminiError(RuntimeError):
    """Отказ Gemini: код HTTP с телом ответа либо неразобранный ответ."""

    # Повтор помогает по умолчанию: сеть и битый ответ бывают разовыми.
    permanent = False

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class GeminiTruncated(GeminiError):
    """Ответ оборван потолком вывода: ленту надо делить на части."""

    permanent = True


class GeminiPromptTooLong(GeminiError):
    """Промпт длиннее окна модели: ленту надо делить на части."""

    permanent = True


class GeminiRefused(GeminiError):
    """Модель отказалась отвечать: цензура, цитирование, запрещённый контент."""

    permanent = True


class GeminiEmpty(GeminiError):
    """Кандидатов нет: запрос отклонён целиком."""

    permanent = True


class GeminiQuotaExhausted(GeminiError):
    """Суточная квота выбрана: паузой не лечится."""

    permanent = True


def _http(url, body, headers):
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Жалоба Google — единственная подсказка о том, что не так: отдаём её целиком.
        detail = error.read().decode("utf-8", "replace")
        raise GeminiError(f"HTTP {error.code}: {detail}", code=error.code) from error


def _daily_quota(error):
    """Суточное превышение отличаем от минутного по упоминанию дня в жалобе Google.

    Жалоба пишет день и так, и сяк: `PerDay`, `per_day`, `per-day`. Без снятия
    подчёркиваний и дефисов snake_case уходил в ветку повторов 429 — три
    бесполезных попытки на каждый чат очереди вместо одной внятной остановки.
    """
    if getattr(error, "code", None) != 429:
        return False
    text = str(error).lower().replace("_", "").replace("-", "")
    return any(mark in text for mark in DAILY_QUOTA_MARKS)


class Gemini:
    def __init__(self, api_key, transport=None, usage_path=None, pause=None):
        self.api_key = api_key
        self.transport = transport or _http
        self.usage_path = Path(usage_path) if usage_path else None
        self.pause = pause or time.sleep
        # Проходы по чату идут в потоках, а расход пишется в один файл. Дописать
        # строку в конец — не одно действие: система сначала ищет конец файла,
        # потом пишет. Два потока успевают найти один и тот же конец и затирают
        # написанное друг другом. Строки здесь — деньги, поэтому пишем по одному.
        self._pen = threading.Lock()

    def check_models(self, wanted):
        """Делит запрошенные модели на доступные и отсутствующие по списку от API."""
        payload = self.transport(
            f"{BASE}?pageSize=1000", None, {"x-goog-api-key": self.api_key}
        )
        available = {
            entry.get("name", "").removeprefix("models/")
            for entry in payload.get("models", [])
            if "generateContent" in entry.get("supportedGenerationMethods", [])
        }
        return ModelCheck(
            present=[name for name in wanted if name in available],
            missing=[name for name in wanted if name not in available],
        )

    def ask(self, model, prompt, schema, chat_id, stage):
        """Отправляет промпт и возвращает разобранный JSON по схеме."""
        if len(prompt) > MAX_PROMPT_CHARS:
            raise GeminiPromptTooLong(
                f"промпт {len(prompt)} символов при пределе {MAX_PROMPT_CHARS}: "
                f"чат {chat_id}, шаг {stage} — ленту надо делить на части"
            )
        url = f"{BASE}/{model}:generateContent"
        body = json.dumps(
            {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": schema,
                    "temperature": 0,
                    "maxOutputTokens": MAX_OUTPUT_TOKENS,
                },
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}

        # Счётчики раздельные: отказ сети и отказ сервера стоят разного терпения.
        network = 0
        answered = 0
        while True:
            try:
                payload = self.transport(url, body, headers)
                # Токены списаны в момент ответа, поэтому пишем расход до разбора.
                self._log(model, chat_id, stage, payload.get("usageMetadata", {}))
                return self._parse(payload, chat_id, stage)
            except Exception as error:  # сеть, битый JSON, пустой ответ
                if _daily_quota(error):
                    raise GeminiQuotaExhausted(
                        f"квота Gemini исчерпана на сегодня, повтор поможет только завтра: "
                        f"чат {chat_id}, шаг {stage}, {error}",
                        code=429,
                    ) from error
                if getattr(error, "permanent", False):
                    raise  # оборванный ответ, отказ модели, пустой ответ — повтор даст то же
                if getattr(error, "code", None) not in (None, *RETRY_CODES):
                    raise  # неверный ключ, нет модели, промпт не по схеме — повтор не поможет
                if isinstance(error, NETWORK_FAILURES):
                    # Обычно запрос не ушёл и денег не стоил. Кроме таймаута
                    # чтения: Google мог принять запрос и списать токены, а
                    # ответ не доехал — в usage.jsonl такой попытки нет, и
                    # ledger занижен относительно счёта Google. Помнить при
                    # сверке счетов.
                    network += 1
                    if network >= NETWORK_ATTEMPTS:
                        raise
                    self.pause(NETWORK_BACKOFF[min(network, len(NETWORK_BACKOFF)) - 1])
                else:
                    # Google ответил, ответ оплачен: повторять его долго и много раз накладно.
                    answered += 1
                    if answered >= ATTEMPTS:
                        raise
                    self.pause(5 * answered)

    def _parse(self, payload, chat_id, stage):
        """Достаёт JSON из ответа, объясняя в ошибке, что пришло вместо него."""
        candidates = payload.get("candidates") or []
        if not candidates:
            feedback = payload.get("promptFeedback") or {}
            reason = feedback.get("blockReason", "причина не названа")
            raise GeminiEmpty(
                f"ответа нет, запрос отклонён целиком ({reason}): чат {chat_id}, шаг {stage}"
            )
        candidate = candidates[0]
        finish = candidate.get("finishReason")
        if finish == "MAX_TOKENS":
            raise GeminiTruncated(
                f"ответ оборван на потолке {MAX_OUTPUT_TOKENS} токенов: "
                f"чат {chat_id}, шаг {stage} — ленту надо делить на части"
            )
        if finish in REFUSAL_REASONS:
            raise GeminiRefused(
                f"модель отказалась отвечать ({finish}): чат {chat_id}, шаг {stage}"
            )
        try:
            text = candidate["content"]["parts"][0]["text"]
            return json.loads(text)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            got = json.dumps(payload, ensure_ascii=False)[:1000]
            raise GeminiError(
                f"ответ не разобран ({error}): чат {chat_id}, шаг {stage}, пришло {got}"
            ) from error

    def _log(self, model, chat_id, stage, usage):
        if self.usage_path is None:
            return
        row = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": model,
            "chat": chat_id,
            "stage": stage,
            "input": usage.get("promptTokenCount", 0),
            "output": usage.get("candidatesTokenCount", 0),
            "thoughts": usage.get("thoughtsTokenCount", 0),
        }
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self._pen:
            self.usage_path.parent.mkdir(parents=True, exist_ok=True)
            with self.usage_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
