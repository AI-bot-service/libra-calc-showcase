"""
Запрос к vision-модели со структурированным ответом по JSON-схеме.

Два уровня отказоустойчивости:
- цепочка моделей по задаче (`chain`): первая не ответила или вернула не тот
  формат — пробуется следующая, у последней бюджет времени больше;
- цепочка провайдеров (`ask`, `route.py`): провайдер лёг — запрос уходит
  следующему, модели у него нет — тоже, но пара запоминается на время.

Модель распознаёт только состав и массу компонентов. Углеводы и ХЕ считает
код по справочнику питания в Decimal, а не LLM.

Публичная версия: без имён моделей, учёта стоимости и особенностей отдельных
провайдеров; классификация ошибок и бюджеты времени — как в проде.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

from libra.vision.route import (
    ModelMissing,
    Provider,
    ProviderDown,
    candidates,
    mark_down,
    mark_missing,
    model_ok,
)

# Соединение с живым провайдером ставится за доли секунды. Дольше — он лежит,
# и ждать минуту общего таймаута, пока человек стоит над тарелкой, нельзя.
CONNECT_TIMEOUT = 5.0
# Так отвечает балансировщик перед упавшим прокси, а не модель
GATEWAY_DOWN = (502, 503, 504)
MAX_TOKENS = 4000

# Жёсткий бюджет на модель. Таймаут чтения httpx не спасает: пока модель
# думает, шлюз шлёт пробелы для поддержания соединения, и запрос может
# висеть бесконечно. Сумма бюджетов меньше лимита задачи Celery (150 с),
# иначе задачу убивают раньше, чем очередь дойдёт до запасной модели.
BUDGET_FIRST = 55.0
BUDGET_LAST = 75.0
# Доля бюджета основной модели провайдера, у которого есть запасная:
# основная то отвечает за 5–10 с, то висит дольше минуты, и запасная
# без своей доли не успевает получить шанс
PRIMARY_SHARE = 0.6

log = logging.getLogger("libra.vision")
T = TypeVar("T")


class VisionError(RuntimeError):
    pass


def chain(models: list[str], ask_model: Callable[[str, float], T]) -> T:
    """Пробует модели по очереди. Молча ронять распознавание нельзя:
    человек стоит над тарелкой."""
    if not models:
        raise VisionError("Список моделей пуст")
    last: Exception | None = None
    for n, model in enumerate(models):
        budget = BUDGET_LAST if n == len(models) - 1 else BUDGET_FIRST
        try:
            return ask_model(model, time.monotonic() + budget)
        except VisionError as exc:
            log.warning("модель %s не справилась: %s", model, exc)
            last = exc
    raise VisionError(f"Ни одна модель не ответила: {last}")


def ask(model: str, messages: list[dict], schema_name: str, schema: dict,
        retries: int, timeout: float, deadline: float) -> dict:
    """Первый живой провайдер, у которого есть модель → ответ по схеме."""
    pairs = candidates(model)
    last: VisionError | None = None
    down: set[str] = set()
    for i, (p, name) in enumerate(pairs):
        if p.name in down:
            continue
        started = time.monotonic()
        spare = i + 1 < len(pairs) and pairs[i + 1][0].name == p.name
        until = min(deadline, started + (deadline - started) * PRIMARY_SHARE) if spare else deadline
        try:
            result = ask_one(p, name, messages, schema_name, schema, retries, timeout, until)
        except ProviderDown as exc:
            down.add(p.name)
            mark_down(p.name, str(exc))
            last = VisionError(f"API недоступен: {exc}")
            continue
        except ModelMissing as exc:
            log.warning("%s: модель %s не ответила (%s), пробую дальше", p.name, name, exc)
            if exc.remember:
                mark_missing(p.name, name, str(exc))
            last = VisionError(f"Модель {name} недоступна: {exc}")
            continue
        except VisionError as exc:
            if not spare:
                raise
            log.warning("%s: модель %s не ответила (%s), пробую запасную", p.name, name, exc)
            last = exc
            continue
        model_ok(p.name, name)
        return result
    raise last or VisionError(f"Модели {model} нет ни у одного провайдера")


def ask_one(p: Provider, model: str, messages: list[dict], schema_name: str, schema: dict,
            retries: int, timeout: float, deadline: float) -> dict:
    """Один запрос к одной модели с повторами на 402/429/5xx."""
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    }
    headers = {"Authorization": f"Bearer {p.key}"}
    last_error = "неизвестно"
    last_status = 0
    with httpx.Client(headers=headers, timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT)) as client:
        for attempt in range(retries):
            response, raw = _post_until(client, p.url_for(model), payload, deadline)
            status = response.status_code
            if status in GATEWAY_DOWN:
                raise ProviderDown(f"HTTP {status}")
            if status == 401:
                raise ProviderDown("ключ не принят, 401")
            if status in (404, 422) or (status == 400 and _model_unknown(raw)):
                raise ModelMissing(f"HTTP {status}")
            last_status = status
            if status in (402, 429) or status >= 500:
                last_error = f"HTTP {status}"
                wait = 2 ** attempt * 2
                # Тело ответа не логируем: там бывает эхо запроса
                log.warning("модель ответила %s, жду %s с", status, wait)
                if time.monotonic() + wait >= deadline:
                    break
                time.sleep(wait)
                continue
            if status != 200:
                raise VisionError(f"API ответил {status}")
            try:
                parsed = json.loads(json.loads(raw)["choices"][0]["message"]["content"])
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                raise VisionError("Модель вернула не тот формат") from exc
            if not isinstance(parsed, dict):
                raise VisionError("Модель вернула не тот формат")
            return parsed

    # 402 после всех повторов — кончились деньги на счёте
    if last_status == 402:
        raise ProviderDown("баланс пуст, 402")
    raise ModelMissing(f"после повторов {last_error}", remember=False)


def _model_unknown(raw: bytes) -> bool:
    """400 из-за имени модели: прокси и агрегаторы пишут об этом по-своему."""
    text = raw[:2000].lower()
    return b"invalid model" in text or b"not a valid model" in text or b"model not found" in text


def _post_until(client: httpx.Client, url: str, payload: dict, deadline: float) -> tuple[httpx.Response, bytes]:
    """POST с ответом, дочитанным до дедлайна. Пробелы keep-alive приходят
    регулярно, поэтому проверка между кусками срабатывает вовремя."""
    chunks: list[bytes] = []
    # Молчащий сервер проверку между кусками не будит: без потолка по дедлайну
    # запасная модель ждала бы полный таймаут чтения, а не бюджет первой
    left = max(1.0, deadline - time.monotonic())
    read = min(client.timeout.read or left, left)
    try:
        with client.stream("POST", url, json=payload,
                           timeout=httpx.Timeout(read, connect=client.timeout.connect)) as response:
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise VisionError("модель не уложилась в отведённое время")
                chunks.append(chunk)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # Соединение не установилось — лежит провайдер, а не модель
        raise ProviderDown(f"сеть: {type(exc).__name__}") from exc
    except httpx.HTTPError as exc:
        # Обрыв связи — повод перейти к следующей модели, а не уронить задачу
        raise VisionError(f"сеть: {type(exc).__name__}") from exc
    return response, b"".join(chunks)
