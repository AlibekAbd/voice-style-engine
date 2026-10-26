import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from tgservice import gemini as gemini_module
from tgservice.gemini import (
    ATTEMPTS,
    MAX_OUTPUT_TOKENS,
    MAX_PROMPT_CHARS,
    NETWORK_ATTEMPTS,
    NETWORK_BACKOFF,
    Gemini,
    GeminiEmpty,
    GeminiError,
    GeminiPromptTooLong,
    GeminiQuotaExhausted,
    GeminiRefused,
    GeminiTruncated,
)

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def answer(payload, prompt_tokens=100, output_tokens=20, thoughts=5):
    return {
        "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": output_tokens,
            "thoughtsTokenCount": thoughts,
        },
    }


def test_returns_parsed_payload(tmp_path):
    calls = []

    def transport(url, body, headers):
        calls.append((url, json.loads(body)))
        return answer({"ok": True})

    client = Gemini("key", transport=transport, usage_path=tmp_path / "usage.jsonl")
    result = client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")

    assert result == {"ok": True}
    assert "model-x" in calls[0][0]
    assert calls[0][1]["generationConfig"]["responseSchema"] == SCHEMA


def test_retries_then_succeeds(tmp_path):
    attempts = {"n": 0}

    def transport(url, body, headers):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TimeoutError("сеть")
        return answer({"ok": True})

    client = Gemini(
        "key", transport=transport, usage_path=tmp_path / "usage.jsonl", pause=lambda s: None
    )
    result = client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")

    assert result == {"ok": True}
    assert attempts["n"] == 3


def test_gives_up_after_three_attempts(tmp_path):
    def transport(url, body, headers):
        raise TimeoutError("сеть")

    client = Gemini(
        "key", transport=transport, usage_path=tmp_path / "usage.jsonl", pause=lambda s: None
    )

    try:
        client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")
    except TimeoutError:
        pass
    else:
        raise AssertionError("ожидалась ошибка после трёх попыток")


def test_writes_usage_row(tmp_path):
    usage = tmp_path / "usage.jsonl"

    def transport(url, body, headers):
        return answer({"ok": True}, prompt_tokens=1234, output_tokens=56, thoughts=7)

    client = Gemini("key", transport=transport, usage_path=usage)
    client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="b1")

    row = json.loads(usage.read_text(encoding="utf-8").strip())
    assert row["model"] == "model-x"
    assert row["chat"] == "-1001"
    assert row["stage"] == "b1"
    assert row["input"] == 1234
    assert row["output"] == 56
    assert row["thoughts"] == 7


# --- учёт токенов из нескольких потоков ---------------------------------------

WRITERS = 8
ROWS_EACH = 25
# Строка длиннее буфера файла: короткую система дописывает разом, длинная уходит
# на диск кусками, и без замка куски двух потоков перемешаются.
LONG_STAGE = "щ" * 5000


def test_usage_rows_from_threads_are_whole_and_all_there(tmp_path):
    """Проходы идут параллельно, а расход пишется в один файл. Строки — это деньги."""
    usage = tmp_path / "usage.jsonl"

    def transport(url, body, headers):
        return answer({"ok": True}, prompt_tokens=1000, output_tokens=10, thoughts=1)

    client = Gemini("key", transport=transport, usage_path=usage)
    start = threading.Barrier(WRITERS)

    def write(number):
        start.wait()
        for _ in range(ROWS_EACH):
            client.ask("model-x", "лента", SCHEMA, chat_id=str(number), stage=LONG_STAGE)

    threads = [threading.Thread(target=write, args=(number,)) for number in range(WRITERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = usage.read_text(encoding="utf-8").splitlines()
    assert len(lines) == WRITERS * ROWS_EACH  # ни одна строка не потерялась
    rows = [json.loads(line) for line in lines]  # ни одна не порвана посередине
    assert all(row["stage"] == LONG_STAGE and row["input"] == 1000 for row in rows)
    counted = {str(number): 0 for number in range(WRITERS)}
    for row in rows:
        counted[row["chat"]] += 1
    assert set(counted.values()) == {ROWS_EACH}


def test_output_limit_is_set_explicitly(tmp_path):
    calls = []

    def transport(url, body, headers):
        calls.append(json.loads(body))
        return answer({"ok": True})

    client = Gemini("key", transport=transport, usage_path=tmp_path / "usage.jsonl")
    client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")

    assert calls[0]["generationConfig"]["maxOutputTokens"] == MAX_OUTPUT_TOKENS


def test_truncated_answer_is_not_retried_and_usage_is_kept(tmp_path):
    usage = tmp_path / "usage.jsonl"
    attempts = {"n": 0}

    def transport(url, body, headers):
        attempts["n"] += 1
        return {
            "candidates": [
                {
                    "content": {"parts": [{"text": '{"items": [{"mid": 1'}]},
                    "finishReason": "MAX_TOKENS",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 900000,
                "candidatesTokenCount": 65536,
                "thoughtsTokenCount": 10,
            },
        }

    client = Gemini("key", transport=transport, usage_path=usage, pause=lambda s: None)

    with pytest.raises(GeminiTruncated) as caught:
        client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")

    assert attempts["n"] == 1
    assert isinstance(caught.value, GeminiError)
    assert "делить" in str(caught.value)
    row = json.loads(usage.read_text(encoding="utf-8").strip())
    assert row["output"] == 65536


@pytest.mark.parametrize("reason", ["SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST"])
def test_refusal_is_named_and_not_retried(tmp_path, reason):
    attempts = {"n": 0}

    def transport(url, body, headers):
        attempts["n"] += 1
        return {
            "candidates": [{"finishReason": reason}],
            "usageMetadata": {"promptTokenCount": 100},
        }

    client = Gemini(
        "key", transport=transport, usage_path=tmp_path / "usage.jsonl", pause=lambda s: None
    )

    with pytest.raises(GeminiRefused) as caught:
        client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")

    assert attempts["n"] == 1
    assert isinstance(caught.value, GeminiError)
    assert reason in str(caught.value)


def test_empty_answer_names_prompt_feedback(tmp_path):
    attempts = {"n": 0}

    def transport(url, body, headers):
        attempts["n"] += 1
        return {
            "candidates": [],
            "promptFeedback": {"blockReason": "OTHER"},
            "usageMetadata": {"promptTokenCount": 100},
        }

    client = Gemini(
        "key", transport=transport, usage_path=tmp_path / "usage.jsonl", pause=lambda s: None
    )

    with pytest.raises(GeminiEmpty) as caught:
        client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")

    assert attempts["n"] == 1
    assert isinstance(caught.value, GeminiError)
    assert "OTHER" in str(caught.value)


def test_answer_without_candidates_key_is_explained(tmp_path):
    def transport(url, body, headers):
        return {"usageMetadata": {"promptTokenCount": 100}}

    client = Gemini(
        "key", transport=transport, usage_path=tmp_path / "usage.jsonl", pause=lambda s: None
    )

    with pytest.raises(GeminiEmpty) as caught:
        client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")

    assert "-1001" in str(caught.value)


def test_daily_quota_is_not_retried(tmp_path):
    attempts = {"n": 0}
    detail = (
        "HTTP 429: {'error': {'message': 'You exceeded your current quota. "
        "quota_metric: generate_content_free_tier_requests, "
        "quota_id: GenerateRequestsPerDayPerProjectPerModel'}}"
    )

    def transport(url, body, headers):
        attempts["n"] += 1
        raise GeminiError(detail, code=429)

    client = Gemini(
        "key", transport=transport, usage_path=tmp_path / "usage.jsonl", pause=lambda s: None
    )

    with pytest.raises(GeminiQuotaExhausted) as caught:
        client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")

    assert attempts["n"] == 1
    assert "сегодня" in str(caught.value)


def test_minute_quota_is_still_retried(tmp_path):
    attempts = {"n": 0}
    detail = (
        "HTTP 429: {'error': {'message': 'Resource has been exhausted, "
        "quota_id: GenerateRequestsPerMinutePerProjectPerModel'}}"
    )

    def transport(url, body, headers):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise GeminiError(detail, code=429)
        return answer({"ok": True})

    client = Gemini(
        "key", transport=transport, usage_path=tmp_path / "usage.jsonl", pause=lambda s: None
    )

    assert client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a") == {"ok": True}
    assert attempts["n"] == 3


def test_schema_complaint_reaches_the_user_whole(monkeypatch):
    detail = "Invalid JSON payload received. Unknown name responseSchema: " + "щ" * 3000

    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            "https://x", 400, "Bad Request", {}, io.BytesIO(detail.encode("utf-8"))
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(GeminiError) as caught:
        gemini_module._http("https://x", b"{}", {})

    assert caught.value.code == 400
    assert detail in str(caught.value)


def test_check_models_splits_present_and_missing():
    calls = []

    def transport(url, body, headers):
        calls.append((url, body, headers))
        return {
            "models": [
                {
                    "name": "models/gemini-3.7-flash",
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                },
                {
                    "name": "models/text-embedding-004",
                    "supportedGenerationMethods": ["embedContent"],
                },
            ]
        }

    client = Gemini("key", transport=transport)
    result = client.check_models(
        ["gemini-3.7-flash", "gemini-3.1-pro-preview", "text-embedding-004"]
    )

    assert result.present == ["gemini-3.7-flash"]
    assert result.missing == ["gemini-3.1-pro-preview", "text-embedding-004"]
    url, body, headers = calls[0]
    assert url.startswith("https://generativelanguage.googleapis.com/v1beta/models")
    assert body is None
    assert headers["x-goog-api-key"] == "key"


def test_network_failure_gets_a_longer_backoff(tmp_path):
    """Пропавший DNS — отказ без кода: попыток больше, отступление длиннее."""
    attempts = {"n": 0}
    waits = []

    def transport(url, body, headers):
        attempts["n"] += 1
        raise urllib.error.URLError("[Errno 11001] getaddrinfo failed")

    client = Gemini(
        "key", transport=transport, usage_path=tmp_path / "usage.jsonl", pause=waits.append
    )

    with pytest.raises(urllib.error.URLError):
        client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")

    assert attempts["n"] == NETWORK_ATTEMPTS
    assert waits == list(NETWORK_BACKOFF)
    assert sum(waits) >= 180  # переживаем пропажу сети на несколько минут
    assert sum(waits) <= 600  # и не превращаем падение в получасовое зависание


def test_run_survives_a_short_outage(tmp_path):
    attempts = {"n": 0}

    def transport(url, body, headers):
        attempts["n"] += 1
        if attempts["n"] < NETWORK_ATTEMPTS:
            raise ConnectionResetError("соединение разорвано")
        return answer({"ok": True})

    client = Gemini(
        "key", transport=transport, usage_path=tmp_path / "usage.jsonl", pause=lambda s: None
    )

    assert client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a") == {"ok": True}
    assert attempts["n"] == NETWORK_ATTEMPTS


def test_server_code_keeps_the_short_backoff(tmp_path):
    """Код от Google означает, что сеть жива: ждать минутами нечего."""
    attempts = {"n": 0}
    waits = []

    def transport(url, body, headers):
        attempts["n"] += 1
        raise GeminiError("HTTP 503: сервис недоступен", code=503)

    client = Gemini(
        "key", transport=transport, usage_path=tmp_path / "usage.jsonl", pause=waits.append
    )

    with pytest.raises(GeminiError):
        client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")

    assert attempts["n"] == ATTEMPTS
    assert waits == [5, 10]


def test_unparsed_answer_keeps_the_short_backoff(tmp_path):
    """Ответ пришёл и оплачен: повторять его долго и много раз — платить зря."""
    attempts = {"n": 0}
    waits = []

    def transport(url, body, headers):
        attempts["n"] += 1
        return {
            "candidates": [{"content": {"parts": [{"text": "это не json"}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10},
        }

    client = Gemini(
        "key", transport=transport, usage_path=tmp_path / "usage.jsonl", pause=waits.append
    )

    with pytest.raises(GeminiError):
        client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")

    assert attempts["n"] == ATTEMPTS
    assert waits == [5, 10]


def test_permanent_failure_is_not_saved_by_the_longer_backoff(tmp_path):
    """Отказ модели приходит без кода, но повтора не заслуживает."""
    attempts = {"n": 0}
    waits = []

    def transport(url, body, headers):
        attempts["n"] += 1
        return {
            "candidates": [{"finishReason": "SAFETY"}],
            "usageMetadata": {"promptTokenCount": 100},
        }

    client = Gemini(
        "key", transport=transport, usage_path=tmp_path / "usage.jsonl", pause=waits.append
    )

    with pytest.raises(GeminiRefused):
        client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")

    assert attempts["n"] == 1
    assert waits == []


def test_oversized_prompt_is_named_apart():
    def transport(url, body, headers):
        raise AssertionError("запрос не должен уйти")

    client = Gemini("key", transport=transport)

    with pytest.raises(GeminiPromptTooLong) as caught:
        client.ask("model-x", "щ" * (MAX_PROMPT_CHARS + 1), SCHEMA, "-1001", "a")

    assert "делить на части" in str(caught.value)


def test_daily_quota_in_snake_case_is_not_retried(tmp_path):
    # Жалоба Google пишет день и так: quota_id в snake_case. Без снятия
    # подчёркиваний суточная квота уходила в ветку повторов 429 — три
    # бесполезных попытки на каждый чат очереди.
    attempts = {"n": 0}
    detail = (
        "HTTP 429: {'error': {'message': 'You exceeded your current quota. "
        "quota_id: generate_requests_per_day_per_project'}}"
    )

    def transport(url, body, headers):
        attempts["n"] += 1
        raise GeminiError(detail, code=429)

    client = Gemini(
        "key", transport=transport, usage_path=tmp_path / "usage.jsonl", pause=lambda s: None
    )

    with pytest.raises(GeminiQuotaExhausted):
        client.ask("model-x", "лента", SCHEMA, chat_id="-1001", stage="a")

    assert attempts["n"] == 1
