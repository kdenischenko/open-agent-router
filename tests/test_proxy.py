"""OpenAI-compatible proxy: /v1/chat/completions and /v1/models over api routes."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agent_router.api import create_app
from helpers import bearer, closed_port_url, http_server, write_config

ANSWER = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
}

SSE = (
    b'data: {"id":"c1","object":"chat.completion.chunk",'
    b'"choices":[{"index":0,"delta":{"role":"assistant","content":"hi"}}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{}}],'
    b'"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
    b"data: [DONE]\n\n"
)


def proxy_config_text(lmstudio_url: str, strata_url: str | None = None) -> str:
    strata_url = strata_url or lmstudio_url
    return f"""
[tasks]
allowed_roots = []

[[pools]]
id = "lmstudio"
title = "LM Studio"
unit = "usd"

  [[pools.windows]]
  id = "day"
  duration = "1d"
  kind = "fixed"
  role = "pace"
  limit_usd = 100

[[pools]]
id = "strata"
title = "Strata"
unit = "usd"

  [[pools.windows]]
  id = "day"
  duration = "1d"
  kind = "fixed"
  role = "pace"
  limit_usd = 100

[upstreams.lmstudio]
kind = "openai"
pool = "lmstudio"
base_url = "{lmstudio_url}"

[upstreams.strata]
kind = "openai"
pool = "strata"
base_url = "{strata_url}"

[[routes]]
id = "lmstudio/qwen"
kind = "api"
pool = "lmstudio"
upstream = "lmstudio"
model = "qwen-local"
tiers = ["cheap"]
quality = 3

[[routes]]
id = "strata/qwen"
kind = "api"
pool = "strata"
upstream = "strata"
model = "qwen-local"
tiers = ["cheap"]
quality = 2

[[routes]]
id = "lmstudio/alias"
kind = "api"
pool = "lmstudio"
upstream = "lmstudio"
model = "real-model"
model_key = "alias"
tiers = ["cheap"]
quality = 1
"""


def test_chat_completions_proxied(home, store, clock, tokens, bus) -> None:
    with http_server(body=json.dumps(ANSWER).encode()) as (url, seen):
        config = write_config(home, proxy_config_text(url))
        app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
        with TestClient(app, base_url="http://127.0.0.1:8787") as client:
            response = client.post(
                "/v1/chat/completions",
                headers=bearer(tokens.client),
                json={"model": "qwen-local", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert response.status_code == 200
            assert response.json() == ANSWER
            assert len(seen) == 1
            assert seen[0].path == "/chat/completions"
            assert json.loads(seen[0].body)["model"] == "qwen-local"
            assert "authorization" not in {name.lower() for name in seen[0].headers}
            spend = app.state.gateway.jobs.spend_since("lmstudio", None)
            assert spend.requests == 1
            assert spend.tokens == 12


def test_model_key_alias_is_rewritten(home, store, clock, tokens, bus) -> None:
    with http_server(body=json.dumps(ANSWER).encode()) as (url, seen):
        config = write_config(home, proxy_config_text(url))
        app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
        with TestClient(app, base_url="http://127.0.0.1:8787") as client:
            response = client.post(
                "/v1/chat/completions",
                headers=bearer(tokens.client),
                json={"model": "alias", "messages": []},
            )
        assert response.status_code == 200
        assert json.loads(seen[0].body)["model"] == "real-model"


def test_fallback_to_second_upstream(home, store, clock, tokens, bus) -> None:
    dead = closed_port_url()
    with http_server(body=json.dumps(ANSWER).encode()) as (url, seen):
        config = write_config(home, proxy_config_text(dead, url))
        app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
        with TestClient(app, base_url="http://127.0.0.1:8787") as client:
            response = client.post(
                "/v1/chat/completions",
                headers=bearer(tokens.client),
                json={"model": "qwen-local", "messages": []},
            )
            assert response.status_code == 200
            assert len(seen) == 1
            assert app.state.gateway.jobs.spend_since("strata", None).requests == 1
            assert app.state.gateway.jobs.spend_since("lmstudio", None).requests == 0


def test_all_upstreams_unreachable(home, store, clock, tokens, bus) -> None:
    config = write_config(home, proxy_config_text(closed_port_url()))
    app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        response = client.post(
            "/v1/chat/completions",
            headers=bearer(tokens.client),
            json={"model": "qwen-local", "messages": []},
        )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"


def test_upstream_error_passes_through(home, store, clock, tokens, bus) -> None:
    body = json.dumps({"error": {"message": "broken", "code": "broken"}}).encode()
    with http_server(status=500, body=body) as (url, _seen):
        config = write_config(home, proxy_config_text(url))
        app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
        with TestClient(app, base_url="http://127.0.0.1:8787") as client:
            response = client.post(
                "/v1/chat/completions",
                headers=bearer(tokens.client),
                json={"model": "qwen-local", "messages": []},
            )
            assert response.status_code == 500
            assert response.content == body
            assert app.state.gateway.jobs.spend_since("lmstudio", None).requests == 0


def test_streaming_completion(home, store, clock, tokens, bus) -> None:
    with http_server(body=SSE, headers={"Content-Type": "text/event-stream"}) as (url, seen):
        config = write_config(home, proxy_config_text(url))
        app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
        with TestClient(app, base_url="http://127.0.0.1:8787") as client:
            response = client.post(
                "/v1/chat/completions",
                headers=bearer(tokens.client),
                json={"model": "qwen-local", "messages": [], "stream": True},
            )
            assert response.status_code == 200
            assert response.content == SSE
            sent = json.loads(seen[0].body)
            assert sent["stream_options"]["include_usage"] is True
            spend = app.state.gateway.jobs.spend_since("lmstudio", None)
            assert spend.requests == 1
            assert spend.tokens == 5


def test_unknown_model(home, store, clock, tokens, bus) -> None:
    config = write_config(home, proxy_config_text(closed_port_url()))
    app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        response = client.post(
            "/v1/chat/completions",
            headers=bearer(tokens.client),
            json={"model": "nope", "messages": []},
        )
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "model_not_found"
    assert "qwen-local" in error["message"]


def test_disabled_upstream_has_no_route(home, store, clock, tokens, bus) -> None:
    text = (
        proxy_config_text(closed_port_url())
        .replace("[upstreams.lmstudio]", "[upstreams.lmstudio]\nenabled = false")
        .replace("[upstreams.strata]", "[upstreams.strata]\nenabled = false")
    )
    config = write_config(home, text)
    app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        response = client.post(
            "/v1/chat/completions",
            headers=bearer(tokens.client),
            json={"model": "qwen-local", "messages": []},
        )
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "no_route"
    assert "upstream disabled" in error["message"]


def test_models_listing(home, store, clock, tokens, bus) -> None:
    config = write_config(home, proxy_config_text(closed_port_url()))
    app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        response = client.get("/v1/models", headers=bearer(tokens.client))
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["data"]}
    assert ids == {"qwen-local", "real-model", "alias"}


def test_auth_required(home, store, clock, tokens, bus) -> None:
    config = write_config(home, proxy_config_text(closed_port_url()))
    app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-local", "messages": []})
        assert response.status_code == 401
        assert client.get("/v1/models").status_code == 401


def test_malformed_body(home, store, clock, tokens, bus) -> None:
    config = write_config(home, proxy_config_text(closed_port_url()))
    app = create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        response = client.post(
            "/v1/chat/completions",
            headers={**bearer(tokens.client), "Content-Type": "application/json"},
            content=b"not json",
        )
        assert response.status_code == 400
        assert (
            client.post(
                "/v1/chat/completions",
                headers=bearer(tokens.client),
                json={"messages": []},
            ).json()["error"]["message"]
            == "model is required"
        )
