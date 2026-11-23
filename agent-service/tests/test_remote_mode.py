"""GARRISON_URL + GARRISON_KEY: the agent runs on the control plane's bundle.
Without them it's library mode on anvil-fooddash.yaml."""

import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest
import yaml

from .conftest import MENU, RESTAURANTS, StubBackend, StubLLM, received

CONFIG = yaml.safe_load((Path(__file__).resolve().parents[2] / "anvil-fooddash.yaml").read_text())
KEY = "bk_" + "k" * 43


class FakeGarrison:
    """The control plane's GET /v1/sdk/bundle, with ETags."""

    def __init__(self, bundle, app="fooddash"):
        self.app = app
        self.requests: list[httpx.Request] = []
        self.publish(bundle, 1)

    def publish(self, bundle, revision):
        self.bundle, self.revision = copy.deepcopy(bundle), revision
        self.etag = hashlib.sha256(json.dumps(bundle, sort_keys=True).encode()).hexdigest()

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"detail": "bad key"})
        if request.headers.get("if-none-match", "").strip('"') == self.etag:
            return httpx.Response(304)
        return httpx.Response(200, json={"app": self.app, "revision": self.revision, "etag": self.etag,
                                         "bundle": self.bundle})


def _remote_env(monkeypatch):
    monkeypatch.setenv("GARRISON_URL", "http://garrison.test")
    monkeypatch.setenv("GARRISON_KEY", KEY)
    monkeypatch.setenv("GARRISON_REFRESH_SECONDS", "0")  # tests refresh explicitly


@pytest.fixture
async def remote(monkeypatch, tmp_path):
    import app.main as main

    _remote_env(monkeypatch)
    service, llm, backend = FakeGarrison(CONFIG), StubLLM({}), StubBackend()
    platform = main.build_platform(openai_client=llm, http_transport=httpx.MockTransport(backend),
                                   service_transport=httpx.MockTransport(service), cache_dir=tmp_path,
                                   dashboard=False)  # FakeGarrison has no /v1/traces
    await platform.start()
    main.app.state.platform = platform
    from .conftest import FIRST_PARTY

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://agent",
                                 headers=FIRST_PARTY) as client:
        yield client, platform, service, llm, backend
    await platform.close()


async def test_remote_mode_runs_on_the_service_bundle(remote):
    client, platform, service, llm, backend = remote
    assert platform.app == "fooddash" and platform.bundle_revision == 1
    assert service.requests[0].url.path == "/v1/sdk/bundle"
    msg = "What's on the Golden Dragon menu?"
    llm.plans[msg] = [{"tool": "get_menu", "args": {"restaurant_id": "rest_1"}}, {"text": "Two items."}]
    out = (await client.post("/agent/chat", json={"message": msg, "session_id": "tab"})).json()
    assert out["response"] == "Two items." and out["session_id"] == "tab"
    assert backend.calls() == [("GET", "/restaurants/rest_1/menu")]
    assert received(llm, msg) == [(True, MENU)]
    assert llm.requests[0][0]["content"].startswith("You are the FoodDash ordering assistant.")


async def test_prompt_edit_in_the_service_applies_without_restart(remote):
    client, platform, service, llm, _ = remote
    llm.plans.update({"first": [{"text": "one"}], "second": [{"text": "two"}]})
    await client.post("/agent/chat", json={"message": "first", "session_id": "tab"})

    edited = copy.deepcopy(CONFIG)
    edited["agents"]["fooddash.ordering_agent"].update(instructions="You are FoodDash's assistant. Be brief.",
                                                       prompt_version="v2")
    service.publish(edited, 2)
    assert await platform.refresh() is True
    await client.post("/agent/chat", json={"message": "second", "session_id": "tab"})
    sent = llm.requests[-1]
    assert sent[0]["content"] == "You are FoodDash's assistant. Be brief."
    assert any(isinstance(m, dict) and m.get("content") == "first" for m in sent)  # same conversation
    assert platform.describe_agent("ordering_agent")["prompt_version"] == "v2"


def test_key_for_another_app_is_refused(monkeypatch, tmp_path):
    import app.main as main

    _remote_env(monkeypatch)
    other = FakeGarrison({"apps": {"notify": {"tools": {}}}}, app="notify")
    with pytest.raises(RuntimeError, match="belongs to app 'notify'"):
        main.build_platform(service_transport=httpx.MockTransport(other), cache_dir=tmp_path, dashboard=False)


def test_fooddash_base_url_is_ignored_in_remote_mode_with_a_warning(monkeypatch, tmp_path, caplog):
    import app.main as main

    _remote_env(monkeypatch)
    monkeypatch.setenv("FOODDASH_BASE_URL", "http://app:8000")
    with caplog.at_level("WARNING"):
        platform = main.build_platform(service_transport=httpx.MockTransport(FakeGarrison(CONFIG)),
                                       cache_dir=tmp_path, dashboard=False)
    assert "FOODDASH_BASE_URL is ignored in remote mode" in caplog.text
    assert platform.config.platform.apps["fooddash"]["base_url"] == "http://localhost:8000"


def test_without_the_env_vars_it_is_library_mode():
    import app.main as main

    platform = main.build_platform()
    assert platform.bundle_revision is None and platform.app == "fooddash"
    assert platform.config.platform.apps["fooddash"]["base_url"] == "http://localhost:8000"


async def test_fooddash_base_url_and_openai_model_override_the_yaml(monkeypatch):
    import app.main as main

    monkeypatch.setenv("FOODDASH_BASE_URL", "http://app:8000/")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4.1-mini")
    llm = StubLLM({"Restaurants?": [{"tool": "list_restaurants", "args": {}}, {"text": "Golden Dragon."}]})
    backend = StubBackend()
    platform = main.build_platform(openai_client=llm, http_transport=httpx.MockTransport(backend))
    assert platform.config.platform.agents["fooddash.ordering_agent"].model == "gpt-4.1-mini"
    await platform.run_agent("ordering_agent", "Restaurants?")
    await platform.close()
    assert received(llm, "Restaurants?") == [(True, RESTAURANTS)]
    assert llm.models == ["gpt-4.1-mini", "gpt-4.1-mini"]  # what the agent actually asked for
    (req,) = backend.requests
    assert (req.url.host, req.url.port, req.url.path) == ("app", 8000, "/restaurants")


async def test_startup_requires_openai_api_key(monkeypatch):
    import app.main as main

    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        async with main.lifespan(main.app):
            pass


async def test_startup_creates_and_shutdown_closes_the_platform():
    import app.main as main

    async with main.lifespan(main.app):
        platform = main.app.state.platform
        assert [a["name"] for a in platform.list_agents()] == ["ordering_agent"]
