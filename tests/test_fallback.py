"""
Основной провайдер и два резерва. Человек с тарелкой не должен узнать о падении
провайдера, а администратор — должен.
"""

import json

import httpx
import pytest

from libra.vision import client as vc
from libra.vision import route

ANSWER = {"readable": True, "dishes": []}
HOSTS = {"main.test": "main", "reserve1.test": "reserve1", "reserve2.test": "reserve2"}


@pytest.fixture
def env(monkeypatch):
    # up, down (нет соединения), 502, missing (нет модели), missing-a (нет только model-a), broke (нет денег)
    state = {"main": "up", "reserve1": "up", "reserve2": "up", "calls": [], "sent": []}

    def handler(request: httpx.Request) -> httpx.Response:
        name = HOSTS[request.url.host]
        mode = state[name]
        if mode == "down":
            raise httpx.ConnectTimeout("нет соединения")
        if mode == "502":
            return httpx.Response(502)
        model = json.loads(request.content)["model"]
        if mode == "missing" or (mode == "missing-a" and model == "model-a"):
            return httpx.Response(404, json={"error": {"message": "model not found"}})
        if mode == "broke":
            return httpx.Response(402)
        state["calls"].append((name, model))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ANSWER)}}]})

    real = httpx.Client

    def fake(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(vc.httpx, "Client", fake)
    monkeypatch.setattr(vc.time, "sleep", lambda s: None)
    monkeypatch.setattr(route, "RESTORE_AFTER", 0)

    def alive(p: route.Provider) -> bool:
        return state[p.name] == "up"

    route.setup([
        route.Provider("main", "основной провайдер", "https://main.test/v1/chat", "k1", lambda m: m, alive),
        route.Provider("reserve1", "резерв 1", "https://reserve1.test/v1/chat", "k2", lambda m: m, alive,
                       spare="spare-model"),
        route.Provider("reserve2", "резерв 2", "https://reserve2.test/v1/chat", "k3", lambda m: m, alive),
    ], notify=state["sent"].append)
    return state


def ask(model="model-a"):
    return vc.ask(model, [{"role": "user", "content": "тарелка"}], "food", {}, 2, 10, vc.time.monotonic() + 30)


def test_живой_основной(env):
    assert ask() == ANSWER
    assert env["calls"] == [("main", "model-a")]
    assert env["sent"] == []


@pytest.mark.parametrize("mode", ["down", "502"])
def test_основной_лёг_запрос_уходит_в_резерв(env, mode):
    env["main"] = mode
    assert ask() == ANSWER
    assert env["calls"] == [("reserve1", "model-a")]
    assert "недоступен" in env["sent"][0] and "резерв 1" in env["sent"][0]
    # Второй запрос основной уже не трогает, сообщение админу одно
    ask()
    assert env["calls"][-1][0] == "reserve1" and len(env["sent"]) == 1


def test_модели_нет_запрос_к_следующему_и_пара_запоминается(env):
    env["main"] = "missing"
    assert ask() == ANSWER
    assert env["calls"] == [("reserve1", "model-a")]
    assert route.is_missing("main", "model-a")


def test_у_резерва_работает_запасная_модель(env):
    env["main"] = "down"
    env["reserve1"] = "missing-a"
    assert ask() == ANSWER
    assert env["calls"] == [("reserve1", "spare-model")]


def test_пустой_баланс_списывает_провайдера(env):
    env["main"] = "broke"
    assert ask() == ANSWER
    assert env["calls"] == [("reserve1", "model-a")]
    assert route._down_since("main") is not None


def test_ожил_возвращается_по_приоритету(env):
    env["main"] = "down"
    ask()
    env["main"] = "up"
    route.probe("main")
    assert route._down_since("main") is None
    assert "снова доступен" in env["sent"][-1]
    ask()
    assert env["calls"][-1] == ("main", "model-a")


def test_лежат_все_пробуем_всех(env):
    for name in ("main", "reserve1", "reserve2"):
        route.mark_down(name, "тест")
    assert [p.name for p in route.routes()] == ["main", "reserve1", "reserve2"]


def test_цепочка_моделей_переходит_к_запасной():
    seen = []

    def ask_model(model, deadline):
        seen.append(model)
        if model == "first":
            raise vc.VisionError("не тот формат")
        return {"ok": model}

    assert vc.chain(["first", "second"], ask_model) == {"ok": "second"}
    assert seen == ["first", "second"]
