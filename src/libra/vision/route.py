"""
Куда идёт запрос к vision-модели. Провайдеров три, все OpenAI-совместимые:
основной и два резервных. Запрос берёт первого живого провайдера, у которого
есть нужная модель. Человек с тарелкой не должен узнать о падении провайдера,
а администратор — должен.

Провайдер «лежит» — не принял соединение, ответил 502/503/504 своим шлюзом,
не принял ключ (401) или кончились деньги (402 после повторов). Сбой одной
модели (500, не тот формат, не уложилась) разбирает цепочка моделей внутри
провайдера, провайдер из-за него не меняется.

Модели «нет у провайдера» — 404, 422, 400 с жалобой на имя модели. Тогда
запрос сразу идёт к следующему провайдеру, а пара провайдер+модель
пропускается MODEL_RETRY секунд, потом пробуется снова.

Состояние общее для всех процессов воркера — ключи в redis. Лежащий провайдер
проверяется раз в минуту: задачей beat и попутно с запросами. Ожившим он
считается, только если ответила настоящая модель и на счёте есть деньги:
служебный /models у прокси отвечает, даже когда до моделей не достучаться.
Ожил и держится RESTORE_AFTER — ключ снимается, запросы снова идут к нему по
приоритету. Каждый переход пишется в лог и уходит администраторам в Telegram.

Публичная версия: имена провайдеров, их URL и таблицы моделей вынесены в
конфигурацию (`Provider`), логика переключения та же, что в проде.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import redis

log = logging.getLogger("libra.vision")

DOWN_KEY = "libra:vision:down:{}"
PROBE_KEY = "libra:vision:probe:{}"
MISSING_KEY = "libra:vision:missing:{}:{}"
MISSING_SINCE_KEY = "libra:vision:missing_since:{}:{}"
ALIVE_SINCE_KEY = "libra:vision:alive_since:{}"
PROBE_EVERY = 60
# Основной провайдер то отвечал на пробу, то давал 503 на настоящих запросах —
# админу каждую минуту летело «упал / снова доступен». Возвращаем провайдера,
# только когда пробы проходят без сбоя столько секунд подряд.
RESTORE_AFTER = 3 * 60
MODEL_RETRY = 15 * 60


class ProviderDown(RuntimeError):
    pass


class ModelMissing(RuntimeError):
    """Модель у провайдера сейчас не отвечает — запрос идёт к следующему.
    remember=False — разовый сбой после повторов, пару не запоминаем."""

    def __init__(self, reason: str, remember: bool = True) -> None:
        super().__init__(reason)
        self.remember = remember


@dataclass(frozen=True, slots=True)
class Provider:
    name: str
    label: str
    url: str
    key: str
    # Имя модели из цепочки → имя у этого провайдера; None — модели у него нет.
    # Угадывать имя хуже, чем честно отдать запрос запасной модели цепочки.
    model: Callable[[str], str | None]
    # Жив ли провайдер: настоящая модель ответила и баланс положительный
    alive: Callable[[Provider], bool]
    # Запасная модель внутри провайдера, если основная у него легла
    spare: str | None = None

    def url_for(self, model: str) -> str:
        return self.url.format(model=model)

    def models(self, model: str) -> list[str]:
        """Модель у провайдера по порядку попыток; за ней запасная, если есть."""
        name = self.model(model)
        if name is None:
            return []
        return [name, self.spare] if self.spare and name != self.spare else [name]


class _Memory:
    """Хранилище без redis: тесты и запуск без REDIS_BROKER_URL."""

    def __init__(self) -> None:
        self.data: dict[str, tuple[str, float | None]] = {}

    def _alive(self, key: str) -> bool:
        item = self.data.get(key)
        if item and item[1] is not None and item[1] < time.monotonic():
            del self.data[key]
        return key in self.data

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and self._alive(key):
            return None
        self.data[key] = (str(value), time.monotonic() + ex if ex else None)
        return True

    def get(self, key: str) -> str | None:
        return self.data[key][0] if self._alive(key) else None

    def delete(self, key: str) -> int:
        return 1 if self.data.pop(key, None) else 0


_store: redis.Redis | _Memory | None = None
_providers: list[Provider] = []
_notify: Callable[[str], None] = lambda text: None  # noqa: E731


def setup(providers: list[Provider], *, redis_url: str = "", notify: Callable[[str], None] | None = None) -> None:
    """Провайдеры с ключами в порядке приоритета и канал уведомлений админам."""
    global _store, _providers, _notify
    _providers = providers
    _store = (redis.Redis.from_url(redis_url, socket_timeout=2, socket_connect_timeout=2, decode_responses=True)
              if redis_url else _Memory())
    if notify:
        _notify = notify


def store() -> redis.Redis | _Memory:
    global _store
    if _store is None:
        _store = _Memory()
    return _store


def _get(key: str) -> str | None:
    try:
        return store().get(key)
    except redis.RedisError:
        # Без redis не знаем, кто лежит, — пробуем всех по порядку
        return None


def _down_since(name: str) -> float | None:
    value = _get(DOWN_KEY.format(name))
    return float(value) if value else None


def _label(name: str) -> str:
    return next((p.label for p in _providers if p.name == name), name)


def routes() -> list[Provider]:
    """Живые провайдеры по приоритету. Лежат все — пробуем всех: лучше
    попытка, чем гарантированное «не распозналось»."""
    alive = []
    for p in _providers:
        if _down_since(p.name) is not None:
            maybe_probe(p.name)
            if _down_since(p.name) is not None:
                continue
        alive.append(p)
    return alive or list(_providers)


def candidates(model: str) -> list[tuple[Provider, str]]:
    """Пары провайдер+модель в порядке попыток, без пар, недавно числившихся пропавшими."""
    return [(p, name) for p in routes() for name in p.models(model) if not is_missing(p.name, name)]


def _next_alive(after: str) -> str:
    names = [p.name for p in _providers]
    rest = [n for n in names[names.index(after) + 1:] if _down_since(n) is None] if after in names else []
    return f"Распознавание идёт через {_label(rest[0])}." if rest else "Резерва нет — распознавание не работает."


def _first_alive() -> str:
    names = [p.name for p in _providers if _down_since(p.name) is None]
    return _label(names[0]) if names else "—"


def mark_down(name: str, reason: str) -> None:
    try:
        first = store().set(DOWN_KEY.format(name), str(time.time()), nx=True)
    except redis.RedisError:
        return
    if first:
        log.error("%s недоступен (%s)", _label(name), reason)
        notify_admins(f"⚠️ LibraCalc: {_label(name)} недоступен ({reason}). {_next_alive(name)}")


def is_missing(name: str, model: str) -> bool:
    return _get(MISSING_KEY.format(name, model)) is not None


def mark_missing(name: str, model: str, reason: str) -> None:
    try:
        store().set(MISSING_KEY.format(name, model), "1", ex=MODEL_RETRY)
        first = store().set(MISSING_SINCE_KEY.format(name, model), str(time.time()), nx=True)
    except redis.RedisError:
        return
    if first:
        log.error("модели %s нет у %s (%s)", model, _label(name), reason)
        notify_admins(f"⚠️ LibraCalc: модель {model} у {_label(name)} не отвечает ({reason}). "
                      f"Запросы с ней идут к следующему провайдеру, повтор через {MODEL_RETRY // 60} мин.")


def model_ok(name: str, model: str) -> None:
    """Пара провайдер+модель ответила — если числилась пропавшей, сообщить."""
    try:
        since = store().get(MISSING_SINCE_KEY.format(name, model))
        if not since or not store().delete(MISSING_SINCE_KEY.format(name, model)):
            return
    except redis.RedisError:
        return
    minutes = max(1, round((time.time() - float(since)) / 60))
    log.warning("модель %s у %s снова отвечает, перебой ~%s мин", model, _label(name), minutes)
    notify_admins(f"✅ LibraCalc: модель {model} у {_label(name)} снова отвечает, перебой ~{minutes} мин.")


def maybe_probe(name: str) -> None:
    """Проверка не чаще раза в минуту на все процессы."""
    try:
        if not store().set(PROBE_KEY.format(name), "1", nx=True, ex=PROBE_EVERY):
            return
    except redis.RedisError:
        return
    probe(name)


def probe(name: str | None = None) -> None:
    """Лежащий провайдер ожил и держится RESTORE_AFTER — вернуть ему запросы по приоритету.
    Без имени — все лежащие (задача beat)."""
    for p in _providers:
        if name not in (None, p.name):
            continue
        since = _down_since(p.name)
        if since is None:
            continue
        try:
            if not p.alive(p):
                store().delete(ALIVE_SINCE_KEY.format(p.name))
                continue
            store().set(ALIVE_SINCE_KEY.format(p.name), str(time.time()), nx=True)
            if time.time() - float(store().get(ALIVE_SINCE_KEY.format(p.name)) or time.time()) < RESTORE_AFTER:
                continue
            store().delete(ALIVE_SINCE_KEY.format(p.name))
            restored = store().delete(DOWN_KEY.format(p.name))
        except redis.RedisError:
            continue
        if restored:
            minutes = max(1, round((time.time() - since) / 60))
            log.warning("%s снова доступен, простой %s мин", _label(p.name), minutes)
            notify_admins(f"✅ LibraCalc: {_label(p.name)} снова доступен, простой ~{minutes} мин. "
                          f"Распознавание идёт через {_first_alive()}.")


def notify_admins(text: str) -> None:
    try:
        _notify(text)
    except Exception as exc:  # noqa: BLE001 — уведомление не должно ронять распознавание
        log.warning("не отправилось уведомление администратору: %s", type(exc).__name__)
