"""Ollama classifiers, voting, report, and offline eval."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_router import classifier as classifier_module
from agent_router import cli, cli_common
from agent_router import jobstore as jobstore_module
from agent_router import runner as runner_module
from agent_router.api import create_app
from agent_router.classifier import (
    BUSY_FLAG,
    CLASSIFY_BACKLOG,
    EVAL_FLAG,
    EvalBusy,
    EvalError,
    VoteQueue,
    classify_one,
    flag_value,
    report,
    resolve_tier,
    run_eval,
)
from agent_router.config import (
    ClassifierModel,
    ClassifierSettings,
    ConfigError,
)
from agent_router.jobs import Job, JobRequest, Rating, Vote, VoteRow
from agent_router.jobstore import SqliteJobStore, migrate_jobs_schema
from agent_router.runner import ResolvedExecutable
from agent_router.service import record_classifier_mode
from agent_router.settings_store import SqliteSettings
from agent_router.store import SqliteStore, to_us
from helpers import BASE_URL, full_config_text, set_proxy_env, write_config

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
OLD_FORM = '[classifier]\nkind = "none"'


def classifier_config(home: Path, body: str):
    text = full_config_text()
    assert OLD_FORM in text
    return write_config(home, text.replace(OLD_FORM, body))


NEW_FORM = """
[classifier]
mode = "shadow"
primary = "systemone"
vram_budget_mb = 5300

[[classifier.models]]
id = "systemone"
kind = "systemone"
model = "tev1:4b-q4_K_M"
timeout_s = 3

[[classifier.models]]
id = "lfm3"
kind = "ollama"
model = "lfm3:1.7b-q4_K_M"
timeout_s = 5
min_confidence = 0.4

[[classifier.models]]
id = "gemma"
kind = "ollama"
model = "gemma4:e2b"
live = false
"""


def model(
    mid: str = "systemone", kind: str = "systemone", *, live: bool = True, timeout_s: float = 2.0
) -> ClassifierModel:
    return ClassifierModel(
        id=mid,
        kind=kind,  # type: ignore[arg-type]
        url="http://localhost:11434",
        model=f"{mid}-model",
        timeout_s=timeout_s,
        live=live,
    )


def two_models() -> ClassifierSettings:
    return ClassifierSettings(
        kind="systemone",
        primary="systemone",
        models=(model("systemone", "systemone"), model("lfm3", "ollama")),
    )


# --- config ----------------------------------------------------------------------------


def test_old_form_reads_as_one_classifier(home: Path) -> None:
    config = classifier_config(
        home, '[classifier]\nkind = "systemone"\nmodel = "tev1:4b-q4_K_M"\ntimeout_s = 2.5'
    )
    c = config.classifier
    assert (c.kind, c.url, c.model, c.timeout_s) == (
        "systemone",
        "http://localhost:11434",
        "tev1:4b-q4_K_M",
        2.5,
    )
    assert c.mode == "shadow" and c.models == ()
    [only] = c.effective_models
    assert only == ClassifierModel(
        id="systemone",
        kind="systemone",
        url="http://localhost:11434",
        model="tev1:4b-q4_K_M",
        timeout_s=2.5,
    )
    assert c.primary_model == only


def test_old_form_none_has_no_models(home: Path) -> None:
    c = classifier_config(home, OLD_FORM).classifier
    assert c.kind == "none" and c.effective_models == () and c.primary_model is None


def test_new_form_parses_models_and_defaults(home: Path) -> None:
    c = classifier_config(home, NEW_FORM).classifier
    assert c.mode == "shadow" and c.primary == "systemone" and c.vram_budget_mb == 5300
    assert c.min_confidence == 0.6 and c.write_floor == "standard" and c.wait_s is None
    assert [m.id for m in c.effective_models] == ["systemone", "lfm3", "gemma"]
    systemone, lfm3, gemma = c.models
    assert (
        systemone.kind == "systemone"
        and systemone.url == "http://localhost:11434"
        and systemone.live
    )
    assert lfm3.kind == "ollama" and lfm3.timeout_s == 5 and lfm3.min_confidence == 0.4
    assert gemma.live is False and gemma.timeout_s == 3 and gemma.min_confidence is None
    # legacy fields come from primary: the dashboard and snapshot read them
    assert (c.kind, c.model, c.timeout_s) == ("systemone", "tev1:4b-q4_K_M", 3)
    assert c.primary_model == systemone


def test_new_form_primary_defaults_to_first(home: Path) -> None:
    body = NEW_FORM.replace('primary = "systemone"\n', "").replace(
        'id = "systemone"\nkind = "systemone"', 'id = "first"\nkind = "ollama"'
    )
    c = classifier_config(home, body).classifier
    assert c.primary == "first" and c.kind == "ollama"
    assert c.primary_model is not None and c.primary_model.id == "first"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (NEW_FORM.replace('primary = "systemone"', 'primary = "nope"'), "classifier.primary"),
        (NEW_FORM.replace('id = "lfm3"', 'id = "systemone"'), "duplicate"),
        (
            NEW_FORM.replace('id = "lfm3"', 'id = "lfm3"\nurl = "http://10.0.0.5:11434"'),
            "loopback",
        ),
        ('[classifier]\nkind = "systemone"\nurl = "http://example.com:11434"', "loopback"),
        (NEW_FORM.replace('mode = "shadow"', 'mode = "loud"'), "classifier.mode"),
        (NEW_FORM.replace('kind = "systemone"', 'kind = "tev1"', 1), "kind"),
        (NEW_FORM.replace("vram_budget_mb = 5300", 'write_floor = "huge"'), "write_floor"),
        (NEW_FORM.replace("vram_budget_mb = 5300", "min_confidence = 1.5"), "min_confidence"),
        (NEW_FORM.replace('mode = "shadow"', 'mode = "shadow"\nkind = "systemone"'), "classifier"),
    ],
)
def test_classifier_config_errors(home: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        classifier_config(home, body)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:11434", "http://[::1]:11434", "http://localhost:11434/"]
)
def test_loopback_urls_accepted(home: Path, url: str) -> None:
    body = NEW_FORM.replace('id = "lfm3"', f'id = "lfm3"\nurl = "{url}"')
    assert classifier_config(home, body).classifier.models[1].url == url


def test_init_template_classifier_is_off_and_shows_new_form() -> None:
    text = full_config_text()
    assert (
        "[[classifier.models]]" in text
    )  # the new-form example with one systemone is in a comment
    assert 'mode = "shadow"' in text


# --- backends --------------------------------------------------------------------------


class Response:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Ticks:
    """Test monotonic clock: each call advances time by one step."""

    def __init__(self, step: float = 0.25) -> None:
        self.now = 100.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def chat(content: str, logprobs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"message": {"role": "assistant", "content": content}}
    if logprobs is not None:
        payload["logprobs"] = logprobs
    return payload


def test_ollama_request_shape_and_self_confidence() -> None:
    calls: list[tuple[str, dict[str, Any], float]] = []

    def post(address: str, *, json: dict[str, Any], timeout: float) -> Response:
        calls.append((address, json, timeout))
        return Response(payload=chat('{"tier": "strong", "confidence": 0.7}'))

    prompt = "x" * 5000
    vote = classify_one(prompt, model("lfm3", "ollama", timeout_s=5), post=post, clock=Ticks())
    assert vote == Vote("lfm3", "lfm3-model", "strong", 0.7, 250, None)
    [(address, body, timeout)] = calls
    assert address == "http://localhost:11434/api/chat" and timeout == 5
    assert body["model"] == "lfm3-model"
    assert body["stream"] is False and body["think"] is False
    assert body["keep_alive"] == "30m"
    assert body["options"] == {"temperature": 0, "num_ctx": 4096}
    schema = body["format"]
    assert schema["properties"]["tier"]["enum"] == ["cheap", "standard", "strong"]
    assert set(schema["required"]) == {"tier", "confidence"}
    system, user = body["messages"]
    assert system["role"] == "system" and "cheap" in system["content"]
    assert "JSON" in system["content"]
    assert user == {"role": "user", "content": prompt[:3000] + "\n…\n" + prompt[-1000:]}


def test_ollama_confidence_from_tier_token_logprobs() -> None:
    content = '{"tier": "standard", "confidence": 0.95}'
    tokens = [
        '{"',
        "tier",
        '":',
        ' "',
        "stand",
        "ard",
        '",',
        ' "',
        "confidence",
        '":',
        " 0.95",
        "}",
    ]
    assert "".join(tokens) == content
    logprobs = [{"token": t, "logprob": -0.01} for t in tokens]
    logprobs[4]["logprob"] = math.log(0.8)
    logprobs[5]["logprob"] = math.log(0.5)

    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(payload=chat(content, logprobs))

    vote = classify_one("task", model("lfm3", "ollama"), post=post, clock=Ticks())
    assert vote.tier == "standard"
    assert vote.confidence == pytest.approx(0.4)  # product of tier tokens, not self-confidence


def test_ollama_logprobs_not_matching_content_fall_back_to_self_confidence() -> None:
    content = '{"tier": "cheap", "confidence": 0.9}'
    logprobs = [{"token": "garbage", "logprob": -3.0}]

    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(payload=chat(content, logprobs))

    vote = classify_one("task", model("lfm3", "ollama"), post=post, clock=Ticks())
    assert (vote.tier, vote.confidence) == ("cheap", 0.9)


@pytest.mark.parametrize(
    "payload",
    [
        chat("not json"),
        chat('{"tier": "huge", "confidence": 0.5}'),
        chat('{"tier": "cheap", "confidence": 1.5}'),
        chat('{"tier": "cheap"}'),
        chat('["cheap"]'),
        {"message": {}},
        {},
        ValueError("not json"),
    ],
)
def test_ollama_bad_answer(payload: Any) -> None:
    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(payload=payload)

    vote = classify_one("task", model("lfm3", "ollama"), post=post, clock=Ticks())
    assert (vote.tier, vote.confidence, vote.error) == (None, None, "bad_answer")


@pytest.mark.parametrize("kind", ["ollama", "systemone"])
def test_backend_http_error_and_timeout(kind: str) -> None:
    def post500(*args: Any, **kwargs: Any) -> Response:
        return Response(status_code=500, payload={})

    def post_timeout(*args: Any, **kwargs: Any) -> Response:
        raise httpx.ReadTimeout("slow")

    def post_down(*args: Any, **kwargs: Any) -> Response:
        raise httpx.ConnectError("refused")

    m = model("x", kind)
    assert classify_one("task", m, post=post500, clock=Ticks()).error == "http_500"
    timeout = classify_one("task", m, post=post_timeout, clock=Ticks())
    assert (timeout.tier, timeout.error, timeout.latency_ms) == (None, "timeout", 250)
    assert classify_one("task", m, post=post_down, clock=Ticks()).error == "unavailable"


def systemone(score: float, probabilities: dict[str, Any] | None) -> dict[str, Any]:
    answer: dict[str, Any] = {"type": "score", "score": score, "confidence": 0.41}
    if probabilities is not None:
        answer["probabilities"] = probabilities
    return {"answers": {"tier": answer}}


def test_systemone_backend_confidence_from_probabilities() -> None:
    calls: list[str] = []

    def post(address: str, *, json: dict[str, Any], timeout: float) -> Response:
        calls.append(address)
        assert json["model"] == "d1-model"
        return Response(payload=systemone(2.0, {"0": 0.1, "1": 0.2, "2": 0.7}))

    vote = classify_one("task", model("d1", "systemone"), post=post, clock=Ticks())
    assert calls == ["http://localhost:11434/v1/systemone"]
    assert vote == Vote("d1", "d1-model", "strong", 0.7, 250, None)


def test_systemone_backend_without_probabilities_uses_confidence() -> None:
    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(payload=systemone(0.0, None))

    vote = classify_one("task", model("systemone", "systemone"), post=post, clock=Ticks())
    assert (vote.tier, vote.confidence, vote.error) == ("cheap", 0.41, None)


def test_systemone_backend_bad_answer() -> None:
    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(payload={"answers": {}})

    vote = classify_one("task", model("systemone", "systemone"), post=post, clock=Ticks())
    assert (vote.tier, vote.error) == (None, "bad_answer")


def openai_chat(content: str, logprobs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    choice: dict[str, Any] = {"message": {"role": "assistant", "content": content}}
    if logprobs is not None:
        choice["logprobs"] = {"content": logprobs}
    return {"choices": [choice]}


def backend_answer(kind: str) -> dict[str, Any]:
    if kind == "systemone":
        return systemone(1.0, None)
    if kind == "ollama":
        return chat('{"tier": "standard", "confidence": 0.6}')
    return openai_chat('{"tier": "standard", "confidence": 0.6}')


@pytest.mark.parametrize("kind", ["systemone", "ollama", "openai"])
def test_classifier_reads_api_key_and_sends_bearer_header(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "classifier-test-secret"
    monkeypatch.setenv("CLASSIFIER_TEST_KEY", secret)
    calls: list[dict[str, Any]] = []

    def post(address: str, **kwargs: Any) -> Response:
        calls.append(kwargs)
        return Response(payload=backend_answer(kind))

    m = replace(
        model("cloud", kind),
        url="https://classifier.example/v1",
        api_key_env="CLASSIFIER_TEST_KEY",
    )
    vote = classify_one("task", m, post=post, clock=Ticks())
    assert vote.error is None
    assert calls[0]["headers"] == {"Authorization": f"Bearer {secret}"}
    assert secret not in repr(vote)
    assert secret not in str(vote.error)


def test_classifier_without_api_key_env_sends_no_authorization_header() -> None:
    calls: list[dict[str, Any]] = []

    def post(address: str, **kwargs: Any) -> Response:
        calls.append(kwargs)
        return Response(payload=systemone(1.0, None))

    vote = classify_one("task", model("local", "systemone"), post=post, clock=Ticks())
    assert vote.error is None
    assert "headers" not in calls[0]


@pytest.mark.parametrize("kind", ["systemone", "ollama", "openai"])
def test_classifier_missing_api_key_makes_no_request(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLASSIFIER_TEST_KEY", raising=False)
    calls: list[str] = []

    def post(address: str, **kwargs: Any) -> Response:
        calls.append(address)
        return Response(payload=backend_answer(kind))

    m = replace(model("cloud", kind), api_key_env="CLASSIFIER_TEST_KEY")
    vote = classify_one("task", m, post=post, clock=Ticks())
    assert vote.error == "no_api_key"
    assert vote.tier is None and vote.confidence is None
    assert calls == []


def test_openai_request_shape_and_self_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_TEST_KEY", "secret")
    calls: list[tuple[str, dict[str, Any], float]] = []

    def post(address: str, *, json: dict[str, Any], timeout: float, **kwargs: Any) -> Response:
        calls.append((address, json, timeout))
        return Response(payload=openai_chat('{"tier": "strong", "confidence": 0.7}'))

    m = replace(
        model("remote", "openai", timeout_s=5),
        url="https://openrouter.ai/api/v1/",
        api_key_env="OPENAI_TEST_KEY",
    )
    vote = classify_one("handle the task", m, post=post, clock=Ticks())
    assert vote == Vote("remote", "remote-model", "strong", 0.7, 250, None)
    [(address, body, timeout)] = calls
    assert address == "https://openrouter.ai/api/v1/chat/completions"
    assert timeout == 5
    assert set(body) == {"model", "temperature", "messages", "response_format"}
    assert body["model"] == "remote-model" and body["temperature"] == 0
    assert body["messages"] == [
        {"role": "system", "content": body["messages"][0]["content"]},
        {"role": "user", "content": "handle the task"},
    ]
    assert "cheap" in body["messages"][0]["content"]
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "tier"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["properties"]["tier"]["enum"] == ["cheap", "standard", "strong"]
    # strict structured outputs require a closed object with every property required
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["tier", "confidence"]
    assert set(schema["properties"]["confidence"]) == {"type"}


@pytest.mark.parametrize(
    "content",
    [
        '{"tier": "cheap", "confidence": 0.7}',
        '```json\n{"tier": "cheap", "confidence": 0.7}\n```',
    ],
)
def test_openai_parses_plain_and_fenced_json(content: str) -> None:
    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(payload=openai_chat(content))

    vote = classify_one("task", model("local", "openai"), post=post, clock=Ticks())
    assert (vote.tier, vote.confidence, vote.error) == ("cheap", 0.7, None)


def test_openai_confidence_from_tier_token_logprobs() -> None:
    content = '{"tier": "standard", "confidence": 0.95}'
    tokens = [
        '{"',
        "tier",
        '":',
        ' "',
        "stand",
        "ard",
        '",',
        ' "',
        "confidence",
        '":',
        " 0.95",
        "}",
    ]
    assert "".join(tokens) == content
    logprobs = [{"token": token, "logprob": -0.01} for token in tokens]
    logprobs[4]["logprob"] = math.log(0.8)
    logprobs[5]["logprob"] = math.log(0.5)

    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(payload=openai_chat(content, logprobs))

    vote = classify_one("task", model("local", "openai"), post=post, clock=Ticks())
    assert vote.tier == "standard"
    assert vote.confidence == pytest.approx(0.4)


@pytest.mark.parametrize(
    "payload",
    [
        openai_chat("not json"),
        openai_chat('{"tier": "huge", "confidence": 0.5}'),
        openai_chat('{"tier": "cheap", "confidence": 1.5}'),
        openai_chat('{"tier": "cheap"}'),
        {"choices": []},
        {},
        ValueError("not json"),
    ],
)
def test_openai_bad_answer(payload: Any) -> None:
    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(payload=payload)

    vote = classify_one("task", model("local", "openai"), post=post, clock=Ticks())
    assert (vote.tier, vote.confidence, vote.error) == (None, None, "bad_answer")


def test_openai_http_error() -> None:
    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(status_code=503, payload={})

    vote = classify_one("task", model("local", "openai"), post=post, clock=Ticks())
    assert vote.error == "http_503"


# --- vote queue ------------------------------------------------------------------------


class Now:
    def __init__(self) -> None:
        self.value = T0

    def __call__(self) -> datetime:
        return self.value


class Flags:
    """In-memory settings stub. The eval and busy flags may only change
    atomically (`claim`, `claim_unless`, `release`); a direct `set`/`delete`
    on them is a test error."""

    LOCKS = (EVAL_FLAG, BUSY_FLAG)

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.log: list[tuple[str, str, str | None]] = []
        self.before_claim: Any = None  # hook before the check: "the other process won the race"

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        assert key not in self.LOCKS, f"{key}: claim/claim_unless only"
        self.log.append(("set", key, value))
        self.values[key] = value

    def delete(self, key: str) -> None:
        assert key not in self.LOCKS, f"{key}: release only"
        self.log.append(("delete", key, None))
        self.values.pop(key, None)

    def claim(self, key: str, value: str, free: Any) -> str | None:
        if self.before_claim is not None:
            self.before_claim(key)
        current = self.values.get(key)
        if current is not None and not free(current):
            return current
        self.log.append(("claim", key, value))
        self.values[key] = value
        return None

    def claim_unless(self, key: str, value: str, blocker: str, blocked: Any) -> bool:
        if self.before_claim is not None:
            self.before_claim(key)
        if blocked(self.values.get(blocker)):
            return False
        self.log.append(("claim_unless", key, value))
        self.values[key] = value
        return True

    def release(self, key: str, value: str) -> bool:
        self.log.append(("release", key, value))
        if self.values.get(key) != value:
            return False
        del self.values[key]
        return True


def recording_post(order: list[str]):
    def post(address: str, *, json: dict[str, Any], timeout: float) -> Response:
        if address.endswith("/v1/systemone"):
            order.append(f"{json['state']['prompt']}:systemone")
            return Response(payload=systemone(0.0, {"0": 0.9, "1": 0.05, "2": 0.05}))
        order.append(f"{json['messages'][1]['content']}:lfm3")
        return Response(payload=chat('{"tier": "standard", "confidence": 0.6}'))

    return post


def queue(settings: ClassifierSettings, post, now: Now, flags: Flags | None = None, **kw):
    return VoteQueue(settings, now=now, flags=flags, post=post, clock=Ticks(), start=False, **kw)


def test_queue_primary_of_second_job_before_secondary_of_first() -> None:
    order: list[str] = []
    now = Now()
    q = queue(two_models(), recording_post(order), now)
    q.submit("j_1", "first", T0)
    q.submit("j_2", "second", T0)
    while q.step():
        pass
    assert order == ["first:systemone", "second:systemone", "first:lfm3", "second:lfm3"]
    votes = q.drain()
    assert [(job, v.classifier_id) for job, v in votes] == [
        ("j_1", "systemone"),
        ("j_2", "systemone"),
        ("j_1", "lfm3"),
        ("j_2", "lfm3"),
    ]
    assert votes[0][1] == Vote("systemone", "systemone-model", "cheap", 0.9, 250, None)
    assert q.drain() == []
    assert q.primary == "systemone"


def test_queue_skips_models_not_live() -> None:
    order: list[str] = []
    settings = replace(
        two_models(), models=(model("systemone"), model("lfm3", "ollama", live=False))
    )
    q = queue(settings, recording_post(order), Now())
    q.submit("j_1", "first", T0)
    while q.step():
        pass
    assert order == ["first:systemone"]


def test_queue_drops_old_secondary_vote_without_request() -> None:
    order: list[str] = []
    now = Now()
    q = queue(two_models(), recording_post(order), now)
    q.submit("j_1", "first", T0)
    now.value = T0 + timedelta(minutes=10, seconds=1)
    while q.step():
        pass
    assert order == ["first:systemone"]  # primary is not dropped by age
    votes = dict((v.classifier_id, v) for _, v in q.drain())
    assert votes["lfm3"].error == "dropped" and votes["lfm3"].tier is None
    assert votes["lfm3"].latency_ms == 0 and votes["lfm3"].model == "lfm3-model"
    assert votes["systemone"].error is None


def test_queue_over_backlog_drops_only_secondary() -> None:
    """Backlog drops only non-primary votes; a primary vote is always queued."""
    order: list[str] = []
    q = queue(two_models(), recording_post(order), Now())
    jobs = CLASSIFY_BACKLOG + 5
    for n in range(jobs):
        q.submit(f"j_{n}", f"p{n}", T0)
    dropped = q.drain()
    assert dropped and all(v.classifier_id == "lfm3" for _, v in dropped)
    assert all(v.error == "dropped" for _, v in dropped)
    while q.step():
        pass
    primary = {job for job, v in q.drain() if v.classifier_id == "systemone" and v.error is None}
    assert primary == {f"j_{n}" for n in range(jobs)}
    assert CLASSIFY_BACKLOG == 64


def test_queue_primary_only_never_dropped_by_backlog() -> None:
    settings = replace(two_models(), models=(model("systemone"),))
    q = queue(settings, recording_post([]), Now())
    for n in range(CLASSIFY_BACKLOG + 1):
        q.submit(f"j_{n}", f"p{n}", T0)
    assert q.pending == CLASSIFY_BACKLOG + 1
    assert q.drain() == []


def test_queue_waits_while_eval_flag_is_fresh_and_marks_busy() -> None:
    order: list[str] = []
    flags = Flags()
    flags.values[EVAL_FLAG] = flag_value(4242, T0)
    settings = replace(two_models(), models=(model("systemone"),))
    q = queue(settings, recording_post(order), Now(), flags, alive=lambda pid: pid == 4242)
    q.submit("j_1", "first", T0)
    assert q.step() is False and order == [] and q.pending == 1
    del flags.values[EVAL_FLAG]
    assert q.step() is True and order == ["first:systemone"]
    assert [entry[:2] for entry in flags.log] == [
        ("claim_unless", BUSY_FLAG),
        ("release", BUSY_FLAG),
    ]
    assert BUSY_FLAG not in flags.values


@pytest.mark.parametrize(
    ("flag_at", "alive"),
    [(T0 - timedelta(hours=2, seconds=1), True), (T0, False)],
)
def test_queue_ignores_stale_eval_flag(flag_at: datetime, alive: bool) -> None:
    order: list[str] = []
    flags = Flags()
    flags.values[EVAL_FLAG] = flag_value(4242, flag_at)
    settings = replace(two_models(), models=(model("systemone"),))
    q = queue(settings, recording_post(order), Now(), flags, alive=lambda pid: alive)
    q.submit("j_1", "first", T0)
    assert q.step() is True and order == ["first:systemone"]


def test_queue_busy_flag_removed_after_backend_error() -> None:
    flags = Flags()

    def post(*args: Any, **kwargs: Any) -> Response:
        raise RuntimeError("failure")

    settings = replace(two_models(), models=(model("systemone"),))
    q = queue(settings, post, Now(), flags)
    q.submit("j_1", "first", T0)
    assert q.step() is True
    [(_, vote)] = q.drain()
    assert vote.error == "unavailable"
    assert BUSY_FLAG not in flags.values


def test_queue_thread_runs_and_shutdown_stops_it() -> None:
    order: list[str] = []
    settings = replace(two_models(), models=(model("systemone"),))
    q = VoteQueue(settings, now=Now(), post=recording_post(order), clock=Ticks())
    try:
        q.submit("j_1", "first", T0)
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        votes: list[tuple[str, Vote]] = []
        while not votes and datetime.now(UTC) < deadline:
            time.sleep(0.01)
            votes = q.drain()
        assert [job for job, _ in votes] == ["j_1"]
    finally:
        q.shutdown(wait=True)
    assert q.closed
    q.submit("j_2", "after shutdown", T0)  # does not raise and queues nothing
    assert q.pending == 0


def test_queue_default_post_bypasses_environment_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    set_proxy_env(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []

    def post(address: str, **kwargs: Any) -> Response:
        calls.append((address, kwargs))
        return Response(payload=systemone(1.0, None))

    monkeypatch.setattr(httpx, "post", post)
    settings = replace(two_models(), models=(model("systemone"),))
    q = VoteQueue(settings, now=Now(), clock=Ticks(), start=False)
    q.submit("j_1", "first", T0)
    assert q.step() is True
    [(address, kwargs)] = calls
    assert address == "http://localhost:11434/v1/systemone"
    assert kwargs["trust_env"] is False


# --- vote store ------------------------------------------------------------------------


def make_job(
    job_id: str,
    *,
    tier: str = "standard",
    tier_source: str = "caller",
    mode: str = "read",
    created_at: datetime = T0,
    prompt: str = "task",
) -> Job:
    return Job(
        id=job_id,
        request=JobRequest(prompt, Path("/work/project"), mode=mode),  # type: ignore[arg-type]
        status="completed",
        tier=tier,  # type: ignore[arg-type]
        tier_source=tier_source,  # type: ignore[arg-type]
        repo_root=None,
        created_at=created_at,
    )


def test_votes_roundtrip(jobstore) -> None:
    jobstore.create(make_job("j_1"))
    live = Vote("systemone", "tev1:4b", "cheap", 0.5, 120, None)
    offline = Vote("gemma", "gemma4:e2b", None, None, 3000, "timeout", offline=True)
    jobstore.add_vote("j_1", live, T0)
    jobstore.add_vote("j_1", offline, T0 + timedelta(seconds=1))
    assert jobstore.votes("j_1") == [live, offline]
    assert jobstore.votes("j_none") == []


def test_votes_since_joins_job_and_last_rating_without_limit(jobstore) -> None:
    for n in range(60):
        jobstore.create(make_job(f"j_{n:02}", tier="strong", mode="write"))
        jobstore.add_vote(f"j_{n:02}", Vote("systemone", "m", "cheap", 0.5, 10, None), T0)
    jobstore.add_rating(Rating("j_00", "bad", "cheap", "claude-code", None, None, T0))
    jobstore.add_rating(Rating("j_00", "good", "standard", "claude-code", None, None, T0))
    jobstore.add_vote("j_00", Vote("gemma", "g", "strong", 0.9, 10, None, True), T0)
    jobstore.add_vote(
        "j_01", Vote("systemone", "m", "cheap", 0.5, 10, None), T0 - timedelta(days=1)
    )

    rows = jobstore.votes_since(T0)
    assert len(rows) == 61
    first = rows[0]
    assert first == VoteRow(
        job_id="j_00",
        classifier_id="systemone",
        model="m",
        tier="cheap",
        confidence=0.5,
        latency_ms=10,
        error=None,
        offline=False,
        at=T0,
        job_tier="strong",
        tier_source="caller",
        mode="write",
        created_at=T0,
        rating_value="good",
        rating_should_tier="standard",
    )
    assert len(jobstore.votes_since(T0, offline=True)) == 1
    assert len(jobstore.votes_since(T0, offline=False)) == 60
    assert len(jobstore.votes_since(T0 - timedelta(days=2))) == 62


def test_prompts_since(jobstore) -> None:
    jobstore.create(make_job("j_old", created_at=T0 - timedelta(days=1), prompt="old"))
    jobstore.create(make_job("j_new", created_at=T0, prompt="new"))
    assert jobstore.prompts_since(T0) == [("j_new", "new")]


def test_schema_9_database_opens_as_10(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        store.migrate(2, jobstore_module._SCHEMA_V2)
        store.migrate(3, ["ALTER TABLE attempts ADD COLUMN result TEXT"])
        store.migrate(4, ["ALTER TABLE attempts ADD COLUMN proc_started REAL"])
        store.migrate(5, jobstore_module._SCHEMA_V5)
        store.migrate(6, jobstore_module._SCHEMA_V6)
        store.migrate(7, jobstore_module._SCHEMA_V7)
        store.migrate(8, jobstore_module._SCHEMA_V8)
        store.migrate(9, jobstore_module._SCHEMA_V9)
        job = make_job("j_a")
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO jobs (id, status, created_at, finished_at, data)"
                " VALUES (?, ?, ?, NULL, ?)",
                (job.id, job.status, to_us(job.created_at), jobstore_module._job_data(job)),
            )
        jobs = SqliteJobStore(store)
        assert store.schema_version() == 11
        assert jobs.votes("j_a") == []
        jobs.add_vote("j_a", Vote("systemone", "m", "cheap", 0.5, 10, None), T0)
        assert len(jobs.votes("j_a")) == 1
        SqliteJobStore(store)
        assert store.schema_version() == 11
    finally:
        store.close()


def test_settings_delete(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        SqliteJobStore(store)
        flags = SqliteSettings(store)
        flags.set("k", "v")
        flags.delete("k")
        flags.delete("k")
        assert flags.get("k") is None
    finally:
        store.close()


# --- report ----------------------------------------------------------------------------


def row(
    job_id: str,
    predicted: str | None,
    *,
    classifier_id: str = "systemone",
    job_tier: str = "standard",
    tier_source: str = "caller",
    rating: str | None = None,
    should: str | None = None,
    latency: int = 100,
    error: str | None = None,
    offline: bool = False,
) -> VoteRow:
    return VoteRow(
        job_id=job_id,
        classifier_id=classifier_id,
        model=f"{classifier_id}-model",
        tier=predicted,  # type: ignore[arg-type]
        confidence=None if predicted is None else 0.5,
        latency_ms=latency,
        error=error,
        offline=offline,
        at=T0,
        job_tier=job_tier,  # type: ignore[arg-type]
        tier_source=tier_source,  # type: ignore[arg-type]
        mode="read",
        created_at=T0,
        rating_value=rating,  # type: ignore[arg-type]
        rating_should_tier=should,  # type: ignore[arg-type]
    )


def fabricated_rows() -> list[VoteRow]:
    rows: list[VoteRow] = []
    # 1) should_tier wins: job tier strong, the rating says cheap
    rows += [
        row(f"s{n}", "cheap", job_tier="strong", rating="bad", should="cheap") for n in range(10)
    ]
    # 2) caller tier with a good rating: the job tier is the truth
    rows += [row(f"g{n}", "cheap", job_tier="strong", rating="good") for n in range(10)]
    # 3) caller tier without a rating — weak label
    rows += [row(f"w{n}", "standard", job_tier="standard") for n in range(20)]
    # excluded from accuracy: default tier; caller with a bad rating and no should_tier
    rows += [row(f"d{n}", "strong", tier_source="default") for n in range(10)]
    rows += [row(f"b{n}", "strong", rating="bad") for n in range(5)]
    # errors: votes without a tier
    rows += [row(f"e{n}", None, error="timeout", latency=3000) for n in range(5)]
    # latency 1..? for p50/p95 — set separately below
    # offline votes of another classifier on the same jobs
    rows += [
        row(
            f"s{n}",
            "strong",
            classifier_id="gemma",
            offline=True,
            job_tier="strong",
            rating="bad",
            should="cheap",
        )
        for n in range(10)
    ]
    return rows


def group(result: dict[str, Any], classifier_id: str, offline: bool) -> dict[str, Any]:
    [found] = [
        g
        for g in result["classifiers"]
        if g["classifier_id"] == classifier_id and g["offline"] is offline
    ]
    return found


def test_report_truth_order_matrix_under_and_errors() -> None:
    rows = fabricated_rows()
    assert len({r.job_id for r in rows}) > 50
    result = report(rows, None)
    assert result["mode_since"] is None
    systemone = group(result, "systemone", False)
    assert systemone["model"] == "systemone-model"
    assert systemone["votes"] == 60
    assert systemone["errors"] == 5
    assert systemone["error_rate"] == pytest.approx(5 / 60)

    strong = systemone["truth"]
    assert strong["labeled"] == 20
    assert strong["matched"] == 10  # s*: cheap == should cheap; g*: cheap != strong
    assert strong["accuracy"] == pytest.approx(0.5)
    assert strong["under"] == 10 and strong["under_rate"] == pytest.approx(0.5)
    assert strong["matrix"]["cheap"] == {"cheap": 10, "standard": 0, "strong": 0}
    assert strong["matrix"]["strong"] == {"cheap": 10, "standard": 0, "strong": 0}
    assert strong["matrix"]["standard"] == {"cheap": 0, "standard": 0, "strong": 0}

    weak = systemone["weak"]
    assert weak["labeled"] == 20 and weak["matched"] == 20 and weak["under"] == 0
    assert weak["accuracy"] == pytest.approx(1.0)
    assert weak["matrix"]["standard"]["standard"] == 20

    gemma = group(result, "gemma", True)
    assert gemma["votes"] == 10 and gemma["errors"] == 0
    assert gemma["truth"]["labeled"] == 10 and gemma["truth"]["matched"] == 0
    assert gemma["truth"]["under"] == 0
    assert gemma["truth"]["matrix"]["cheap"]["strong"] == 10
    assert gemma["weak"]["labeled"] == 0 and gemma["weak"]["accuracy"] is None
    assert [(g["classifier_id"], g["offline"]) for g in result["classifiers"]] == [
        ("gemma", True),
        ("systemone", False),
    ]


def test_report_live_and_offline_of_same_classifier_are_separate() -> None:
    rows = [row("j1", "cheap"), row("j1", "strong", offline=True)]
    result = report(rows, "shadow|2026-11-13T09:00:00+00:00")
    assert result["mode_since"] == "shadow|2026-11-13T09:00:00+00:00"
    assert group(result, "systemone", False)["votes"] == 1
    assert group(result, "systemone", True)["votes"] == 1


def test_report_latency_percentiles_nearest_rank_without_errors() -> None:
    rows = [row(f"j{n}", "standard", latency=n) for n in range(1, 101)]
    rows.append(row("bad", None, error="timeout", latency=99_999))
    systemone = group(report(rows, None), "systemone", False)
    assert systemone["latency_p50_ms"] == 50 and systemone["latency_p95_ms"] == 95


def test_report_empty() -> None:
    assert report([], None) == {"mode_since": None, "classifiers": []}


def test_report_counts_dropped_as_error() -> None:
    systemone = group(
        report([row("j1", None, error="dropped", latency=0)], None), "systemone", False
    )
    assert systemone["errors"] == 1 and systemone["latency_p50_ms"] is None
    assert systemone["truth"]["accuracy"] is None and systemone["truth"]["under_rate"] is None


# --- offline eval ----------------------------------------------------------------------


class Ollama:
    """Stubbed Ollama: records requests; /api/ps — loaded models."""

    def __init__(self, flags: Flags) -> None:
        self.flags = flags
        self.calls: list[tuple[str, Any]] = []
        self.loaded: set[str] = {"systemone-model", "lfm3-model"}
        self.unload_status: dict[str, int] = {}  # model → response status for keep_alive: 0
        self.sticky: dict[str, int] = {}  # model → how many more /api/ps polls it stays visible
        self.ps_status = 200
        self.ps_extra: list[str] = []  # unrelated models in /api/ps

    def post(self, address: str, *, json: dict[str, Any], timeout: float) -> Response:
        path = address.removeprefix("http://localhost:11434")
        self.calls.append((path, json))
        assert self.flags.get(EVAL_FLAG) is not None  # everything runs under the eval flag
        if path == "/api/generate":
            assert json["keep_alive"] == 0
            status = self.unload_status.get(json["model"], 200)
            if status != 200:
                return Response(status, {"error": "failure"})
            if json["model"] not in self.sticky:
                self.loaded.discard(json["model"])
            return Response(payload={"done": True})
        self.loaded.add(json["model"])
        if path == "/v1/systemone":
            return Response(payload=systemone(1.0, {"0": 0.2, "1": 0.6, "2": 0.2}))
        return Response(payload=chat('{"tier": "strong", "confidence": 0.8}'))

    def get(self, address: str, *, timeout: float) -> Response:
        path = address.removeprefix("http://localhost:11434")
        self.calls.append((path, None))
        assert path == "/api/ps"
        if self.ps_status != 200:
            return Response(self.ps_status, {"error": "failure"})
        payload = {
            "models": [
                {"name": m, "model": m, "size_vram": 1} for m in sorted(self.loaded) + self.ps_extra
            ]
        }
        for name in list(self.sticky):  # a model leaves memory gradually
            self.sticky[name] -= 1
            if self.sticky[name] <= 0:
                del self.sticky[name]
                self.loaded.discard(name)
        return Response(payload=payload)


def eval_kwargs(flags: Flags, ollama: Ollama, votes: list[tuple[str, Vote, datetime]], **kw):
    sleeps: list[float] = []
    base: dict[str, Any] = {
        "live": [model("systemone"), model("lfm3", "ollama")],
        "flags": flags,
        "add_vote": lambda job_id, vote, at: votes.append((job_id, vote, at)),
        "now": Now(),
        "pid": 777,
        "post": ollama.post,
        "get": ollama.get,
        "clock": Ticks(1.0),
        "sleep": sleeps.append,
        "alive": lambda pid: True,
    }
    base.update(kw)
    return base


def test_eval_writes_offline_votes_and_unloads_after_each_model() -> None:
    flags = Flags()
    ollama = Ollama(flags)
    votes: list[tuple[str, Vote, datetime]] = []
    offline_models = [model("gemma", "ollama", live=False), model("d1", "systemone", live=False)]
    count = run_eval(
        [("j_1", "first"), ("j_2", "second")],
        offline_models,
        **eval_kwargs(flags, ollama, votes),
    )
    assert count == 4
    assert [(job, v.classifier_id, v.offline) for job, v, _ in votes] == [
        ("j_1", "gemma", True),
        ("j_2", "gemma", True),
        ("j_1", "d1", True),
        ("j_2", "d1", True),
    ]
    assert all(at == T0 for _, _, at in votes)
    paths = [(path, None if body is None else body.get("model")) for path, body in ollama.calls]
    # live models unloaded before the run, each offline model right after its own pass
    first_chat = paths.index(("/api/chat", "gemma-model"))
    assert ("/api/generate", "systemone-model") in paths[:first_chat]
    assert ("/api/generate", "lfm3-model") in paths[:first_chat]
    assert ("/api/ps", None) in paths[:first_chat]
    gemma_unload = paths.index(("/api/generate", "gemma-model"))
    first_d1 = paths.index(("/v1/systemone", "d1-model"))
    assert first_chat < gemma_unload < first_d1
    assert paths[-1] == ("/api/generate", "d1-model")
    assert EVAL_FLAG not in flags.values
    set_calls = [entry for entry in flags.log if entry[1] == EVAL_FLAG]
    assert set_calls[0][0] == "claim" and set_calls[0][2] == flag_value(777, T0)
    assert set_calls[-1] == ("release", EVAL_FLAG, flag_value(777, T0))


def test_eval_flag_removed_after_exception() -> None:
    flags = Flags()
    ollama = Ollama(flags)
    votes: list[tuple[str, Vote, datetime]] = []

    def post(address: str, *, json: dict[str, Any], timeout: float) -> Response:
        if address.endswith("/api/chat"):
            raise KeyboardInterrupt  # a user interrupt also releases the flag
        return ollama.post(address, json=json, timeout=timeout)

    with pytest.raises(KeyboardInterrupt):
        run_eval(
            [("j_1", "first")],
            [model("gemma", "ollama", live=False)],
            **eval_kwargs(flags, ollama, votes, post=post),
        )
    assert EVAL_FLAG not in flags.values


def test_eval_refuses_while_other_eval_is_fresh() -> None:
    flags = Flags()
    flags.values[EVAL_FLAG] = flag_value(999, T0 - timedelta(minutes=5))
    ollama = Ollama(flags)
    with pytest.raises(EvalBusy):
        run_eval([("j_1", "first")], [model("gemma", "ollama")], **eval_kwargs(flags, ollama, []))
    assert flags.values[EVAL_FLAG] == flag_value(999, T0 - timedelta(minutes=5))
    assert ollama.calls == []


@pytest.mark.parametrize(
    ("flag_at", "alive"),
    [(T0 - timedelta(hours=3), True), (T0 - timedelta(minutes=1), False)],
)
def test_eval_overwrites_stale_flag(flag_at: datetime, alive: bool) -> None:
    flags = Flags()
    flags.values[EVAL_FLAG] = flag_value(999, flag_at)
    ollama = Ollama(flags)
    votes: list[tuple[str, Vote, datetime]] = []
    run_eval(
        [("j_1", "first")],
        [model("gemma", "ollama")],
        **eval_kwargs(flags, ollama, votes, alive=lambda pid: alive),
    )
    assert len(votes) == 1 and EVAL_FLAG not in flags.values


def test_eval_waits_for_live_request_to_finish() -> None:
    flags = Flags()
    flags.values[BUSY_FLAG] = flag_value(1, T0)
    ollama = Ollama(flags)
    votes: list[tuple[str, Vote, datetime]] = []
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            del flags.values[BUSY_FLAG]  # the live request has finished

    run_eval(
        [("j_1", "first")],
        [model("gemma", "ollama")],
        **eval_kwargs(flags, ollama, votes, sleep=sleep),
    )
    assert len(sleeps) == 3 and len(votes) == 1


def test_eval_and_live_queue_do_not_overlap(tmp_path: Path) -> None:
    """The eval flag in the real settings table stops the live queue."""
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        SqliteJobStore(store)
        flags = SqliteSettings(store)
        live_order: list[str] = []
        settings = replace(two_models(), models=(model("systemone"),))
        q = VoteQueue(
            settings,
            now=Now(),
            flags=flags,
            post=recording_post(live_order),
            clock=Ticks(),
            start=False,
            alive=lambda pid: True,
        )
        q.submit("j_live", "live", T0)
        seen_during_eval: list[bool] = []

        def post(address: str, *, json: dict[str, Any], timeout: float) -> Response:
            if address.endswith("/api/chat"):
                seen_during_eval.append(q.step())  # the live queue tries to work
            if address.endswith("/api/generate"):
                return Response(payload={"done": True})
            return Response(payload=chat('{"tier": "cheap", "confidence": 0.5}'))

        def get(address: str, *, timeout: float) -> Response:
            return Response(payload={"models": []})

        run_eval(
            [("j_old", "old")],
            [model("gemma", "ollama")],
            live=list(settings.effective_models),
            flags=flags,
            add_vote=lambda *args: None,
            now=Now(),
            pid=777,
            post=post,
            get=get,
            clock=Ticks(),
            sleep=lambda s: None,
            alive=lambda pid: True,
        )
        assert seen_during_eval == [False] and live_order == []
        assert q.step() is True and live_order == ["live:systemone"]
    finally:
        store.close()


def test_eval_does_not_unload_or_poll_remote_and_openai_models() -> None:
    flags = Flags()
    ollama = Ollama(flags)
    remote = replace(model("remote", "systemone"), url="https://classifier.example")
    local_openai = model("local-openai", "openai")
    votes: list[tuple[str, Vote, datetime]] = []
    count = run_eval(
        [],
        [remote, local_openai],
        **eval_kwargs(flags, ollama, votes, live=[remote, local_openai]),
    )
    assert count == 0 and votes == []
    assert ollama.calls == []


# --- export ----------------------------------------------------------------------------


def test_export_has_votes_in_jsonl_and_primary_columns_in_csv(tmp_path: Path) -> None:
    from agent_router.export import export

    home = tmp_path / "home"
    home.mkdir()
    store = SqliteStore(home / "agent-router.sqlite")
    try:
        jobs = SqliteJobStore(store)
        jobs.create(make_job("j_1"))
        jobs.add_vote("j_1", Vote("lfm3", "lfm3:1.7b", "strong", 0.8, 300, None), T0)
        jobs.add_vote("j_1", Vote("systemone", "tev1:4b", "cheap", 0.4, 120, None), T0)
        jobs.add_vote("j_1", Vote("systemone", "tev1:4b", "standard", 0.6, 90, None, True), T0)
    finally:
        store.close()
    db = home / "agent-router.sqlite"
    out = tmp_path / "out"
    export(db, out, T0 - timedelta(days=1), None, "jsonl", primary="systemone")
    [job] = [json.loads(line) for line in (out / "jobs.jsonl").read_text("utf-8").splitlines()]
    assert job["votes"] == [
        {
            "classifier_id": "lfm3",
            "model": "lfm3:1.7b",
            "tier": "strong",
            "confidence": 0.8,
            "latency_ms": 300,
            "error": None,
            "offline": False,
        },
        {
            "classifier_id": "systemone",
            "model": "tev1:4b",
            "tier": "cheap",
            "confidence": 0.4,
            "latency_ms": 120,
            "error": None,
            "offline": False,
        },
        {
            "classifier_id": "systemone",
            "model": "tev1:4b",
            "tier": "standard",
            "confidence": 0.6,
            "latency_ms": 90,
            "error": None,
            "offline": True,
        },
    ]
    csv_out = tmp_path / "csv"
    export(db, csv_out, T0 - timedelta(days=1), None, "csv", primary="systemone")
    import csv

    with (csv_out / "jobs.csv").open(encoding="utf-8", newline="") as handle:
        [csv_row] = list(csv.DictReader(handle))
    # live primary vote, not offline
    assert csv_row["primary_classifier"] == "systemone"
    assert csv_row["primary_model"] == "tev1:4b"
    assert csv_row["primary_tier"] == "cheap"
    assert csv_row["primary_confidence"] == "0.4"
    assert csv_row["primary_latency_ms"] == "120"
    assert csv_row["primary_error"] == ""


def test_export_requires_schema_10() -> None:
    from agent_router.export import REQUIRED_SCHEMA

    assert REQUIRED_SCHEMA == 10


# --- snapshot, dashboard, shutdown -----------------------------------------------------


def test_snapshot_new_form_adds_mode_and_models(home: Path) -> None:
    config = classifier_config(home, NEW_FORM)
    result = classifier_module.snapshot_classifier(config.classifier, (3, 4))
    assert result == {
        "kind": "systemone",
        "matched": 3,
        "compared": 4,
        "mode": "shadow",
        "models": [
            {"id": "systemone", "kind": "systemone", "model": "tev1:4b-q4_K_M", "live": True},
            {"id": "lfm3", "kind": "ollama", "model": "lfm3:1.7b-q4_K_M", "live": True},
            {"id": "gemma", "kind": "ollama", "model": "gemma4:e2b", "live": False},
        ],
    }


def test_snapshot_old_form_keeps_old_json(home: Path) -> None:
    config = classifier_config(home, '[classifier]\nkind = "systemone"')
    assert classifier_module.snapshot_classifier(config.classifier, (1, 2)) == {
        "kind": "systemone",
        "matched": 1,
        "compared": 2,
    }


def test_build_dispatcher_wires_vote_queue_and_lifespan_shuts_it(
    home, store, clock, tokens
) -> None:
    from agent_router.api import create_app
    from agent_router.events import EventBus
    from agent_router.jobstore import open_jobstore
    from agent_router.service import build_dispatcher

    config = classifier_config(home, NEW_FORM)
    bus = EventBus(clock)
    jobs = open_jobstore(config.storage, home, store)
    dispatcher = build_dispatcher(config, store, jobs, bus, clock, home, UTC)
    app = create_app(
        config, store, clock, tokens, home=home, bus=bus, dispatcher=dispatcher, background=False
    )
    with TestClient(app, base_url=BASE_URL):
        q = dispatcher.classify_executor
        assert isinstance(q, VoteQueue) and q.primary == "systemone"
        assert not q.closed
    assert q.closed


# --- doctor ----------------------------------------------------------------------------


class DoctorOllama:
    def __init__(self, tags: list[str], ps: dict[str, int], down: bool = False) -> None:
        self.tags = tags
        self.ps = ps
        self.down = down
        self.calls: list[str] = []

    def get(self, address: str, **kwargs: Any) -> Response:
        self.calls.append(address)
        if self.down:
            raise httpx.ConnectError("refused")
        if address.endswith("/api/tags"):
            return Response(payload={"models": [{"name": name} for name in self.tags]})
        assert address.endswith("/api/ps")
        return Response(
            payload={"models": [{"name": n, "size_vram": v} for n, v in self.ps.items()]}
        )


MIB = 1024 * 1024


@pytest.fixture
def doctor_home(home: Path, monkeypatch: pytest.MonkeyPatch, tokens) -> Path:
    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.delenv("AGENT_ROUTER_URL", raising=False)
    monkeypatch.setattr(
        cli, "resolve_executable", lambda name: ResolvedExecutable(None, False), raising=False
    )
    monkeypatch.setattr(cli, "_harness_version", lambda exe: None, raising=False)
    monkeypatch.setattr(runner_module.shutil, "which", lambda name: None)

    class Gateway:
        def get(self, path: str, **params: Any) -> dict:
            return {"seq": 0, "subscribers": {}}

    monkeypatch.setattr(cli_common, "make_client", lambda base_url, token: Gateway())
    return home


def run_doctor(monkeypatch: pytest.MonkeyPatch, ollama: DoctorOllama) -> str:
    monkeypatch.setattr(httpx, "get", ollama.get)
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.output
    return result.output


def test_doctor_warns_when_live_classifiers_exceed_budget(doctor_home, monkeypatch) -> None:
    classifier_config(doctor_home, NEW_FORM)
    ollama = DoctorOllama(
        ["tev1:4b-q4_K_M", "lfm3:1.7b-q4_K_M"],
        {"tev1:4b-q4_K_M": 3767 * MIB, "lfm3:1.7b-q4_K_M": 2000 * MIB, "gemma4:e2b": 273 * MIB},
    )
    output = run_doctor(monkeypatch, ollama)
    assert "Classifier systemone" in output and "3767 MiB" in output
    assert "Classifier gemma" in output and "not in Ollama" in output
    assert "5767" in output and "5300" in output  # gemma is not live and not counted
    assert "Warning" in output


def test_doctor_quiet_within_budget(doctor_home, monkeypatch) -> None:
    classifier_config(doctor_home, NEW_FORM)
    ollama = DoctorOllama(
        ["tev1:4b-q4_K_M", "lfm3:1.7b-q4_K_M", "gemma4:e2b"],
        {"tev1:4b-q4_K_M": 3767 * MIB, "lfm3:1.7b-q4_K_M": 1115 * MIB},
    )
    output = run_doctor(monkeypatch, ollama)
    assert "4882" in output and "5300" in output
    assert "not loaded" in output  # gemma is installed but not loaded
    assert "Warning: live classifiers" not in output


def test_doctor_ollama_down_is_warning_not_failure(doctor_home, monkeypatch) -> None:
    classifier_config(doctor_home, NEW_FORM)
    output = run_doctor(monkeypatch, DoctorOllama([], {}, down=True))
    assert "Ollama unavailable" in output


def test_doctor_ollama_check_bypasses_environment_proxy(
    doctor_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    classifier_config(doctor_home, NEW_FORM)
    set_proxy_env(monkeypatch)
    ollama = DoctorOllama(["tev1:4b-q4_K_M", "lfm3:1.7b-q4_K_M", "gemma4:e2b"], {})
    seen: list[dict[str, Any]] = []
    inner = ollama.get

    def get(address: str, **kwargs: Any) -> Response:
        seen.append(kwargs)
        return inner(address, **kwargs)

    monkeypatch.setattr(httpx, "get", get)
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert len(seen) == 2
    assert all(kwargs["trust_env"] is False for kwargs in seen)


@pytest.mark.parametrize("key_is_set", [True, False])
def test_doctor_remote_classifier_reports_key_without_probing_or_disclosing_it(
    doctor_home: Path, monkeypatch: pytest.MonkeyPatch, key_is_set: bool
) -> None:
    secret = "doctor-classifier-secret"
    if key_is_set:
        monkeypatch.setenv("CLASSIFIER_DOCTOR_KEY", secret)
    else:
        monkeypatch.delenv("CLASSIFIER_DOCTOR_KEY", raising=False)
    body = (
        NEW_FORM
        + """
[[classifier.models]]
id = "cloud"
kind = "openai"
url = "https://classifier.example:8443/v1"
model = "remote-model"
api_key_env = "CLASSIFIER_DOCTOR_KEY"

[[classifier.models]]
id = "local-openai"
kind = "openai"
model = "local-openai-model"
"""
    )
    classifier_config(doctor_home, body)
    ollama = DoctorOllama(["tev1:4b-q4_K_M", "lfm3:1.7b-q4_K_M", "gemma4:e2b"], {})
    output = run_doctor(monkeypatch, ollama)
    state = "set" if key_is_set else "missing"
    assert (
        "Classifier cloud (remote-model): openai at https://classifier.example:8443, "
        f"key from $CLASSIFIER_DOCTOR_KEY: {state}"
    ) in output
    assert (
        "Classifier local-openai (local-openai-model): openai at http://localhost:11434" in output
    )
    assert "local-openai-model): openai at http://localhost:11434, key" not in output
    assert secret not in output
    assert ollama.calls == [
        "http://localhost:11434/api/tags",
        "http://localhost:11434/api/ps",
    ]


def test_doctor_classifier_off_makes_no_requests(doctor_home, monkeypatch) -> None:
    classifier_config(doctor_home, OLD_FORM)

    def boom(*args: Any, **kwargs: Any) -> Response:
        raise AssertionError("request to Ollama while the classifier is off")

    monkeypatch.setattr(httpx, "get", boom)
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Classifier" not in result.output


# --- CLI report / eval -----------------------------------------------------------------


def seed_votes(home: Path) -> None:
    store = SqliteStore(home / "agent-router.sqlite")
    try:
        jobs = SqliteJobStore(store)
        jobs.create(make_job("j_1", tier="cheap", prompt="old task"))
        jobs.add_vote("j_1", Vote("systemone", "tev1:4b-q4_K_M", "cheap", 0.5, 100, None), T0)
        SqliteSettings(store).set("classifier_mode_since", "shadow|2026-11-13T09:00:00+00:00")
    finally:
        store.close()


def test_cli_classifier_report_json(doctor_home: Path) -> None:
    classifier_config(doctor_home, NEW_FORM)
    seed_votes(doctor_home)
    result = CliRunner().invoke(cli.app, ["classifier", "report", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["mode_since"] == "shadow|2026-11-13T09:00:00+00:00"
    [systemone] = body["classifiers"]
    assert systemone["classifier_id"] == "systemone" and systemone["votes"] == 1
    assert systemone["weak"]["matched"] == 1


def test_cli_classifier_report_text_and_since(doctor_home: Path) -> None:
    classifier_config(doctor_home, NEW_FORM)
    seed_votes(doctor_home)
    result = CliRunner().invoke(cli.app, ["classifier", "report"])
    assert result.exit_code == 0, result.output
    assert "systemone" in result.output
    later = CliRunner().invoke(cli.app, ["classifier", "report", "--since", "2026-11-14", "--json"])
    assert later.exit_code == 0, later.output
    assert json.loads(later.output)["classifiers"] == []


def test_cli_classifier_eval_runs_selected_models(doctor_home: Path, monkeypatch) -> None:
    classifier_config(doctor_home, NEW_FORM)
    seed_votes(doctor_home)
    seen: dict[str, Any] = {}

    def fake_run_eval(prompts, models, **kwargs):
        seen["prompts"] = prompts
        seen["models"] = [m.id for m in models]
        seen["live"] = [m.id for m in kwargs["live"]]
        for job_id, _ in prompts:
            kwargs["add_vote"](
                job_id, Vote("gemma", "gemma4:e2b", "cheap", 0.5, 10, None, True), T0
            )
        return len(prompts)

    monkeypatch.setattr(classifier_module, "run_eval", fake_run_eval)
    result = CliRunner().invoke(cli.app, ["classifier", "eval", "--models", "gemma"])
    assert result.exit_code == 0, result.output
    assert seen == {
        "prompts": [("j_1", "old task")],
        "models": ["gemma"],
        "live": ["systemone", "lfm3"],
    }
    store = SqliteStore(doctor_home / "agent-router.sqlite")
    try:
        votes = SqliteJobStore(store).votes("j_1")
    finally:
        store.close()
    assert [v.offline for v in votes] == [False, True]


def test_cli_classifier_eval_passes_direct_http_functions(
    doctor_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    classifier_config(doctor_home, NEW_FORM)
    seed_votes(doctor_home)
    seen: dict[str, Any] = {}

    def fake_run_eval(prompts, models, **kwargs):
        seen["post"] = kwargs["post"]
        seen["get"] = kwargs["get"]
        return len(prompts)

    monkeypatch.setattr(classifier_module, "run_eval", fake_run_eval)
    result = CliRunner().invoke(cli.app, ["classifier", "eval", "--models", "gemma"])
    assert result.exit_code == 0, result.output
    assert seen["post"] is classifier_module.post
    assert seen["get"] is classifier_module.get


def test_cli_classifier_eval_unknown_model(doctor_home: Path) -> None:
    classifier_config(doctor_home, NEW_FORM)
    seed_votes(doctor_home)
    result = CliRunner().invoke(cli.app, ["classifier", "eval", "--models", "gemma,nope"])
    assert result.exit_code == 1
    assert "nope" in result.output


def test_cli_classifier_eval_busy_is_clear_error(doctor_home: Path, monkeypatch) -> None:
    classifier_config(doctor_home, NEW_FORM)
    seed_votes(doctor_home)

    def busy(*args: Any, **kwargs: Any) -> int:
        raise EvalBusy("offline evaluation is already running (pid 999)")

    monkeypatch.setattr(classifier_module, "run_eval", busy)
    result = CliRunner().invoke(cli.app, ["classifier", "eval", "--models", "gemma"])
    assert result.exit_code == 1
    assert "already running" in result.output


# --- active mode: tier rule, report split, mode-change moment ---------------------------


def vote(tier: str | None, confidence: float | None, error: str | None = None) -> Vote:
    return Vote("systemone", "systemone-model", tier, confidence, 100, error)  # type: ignore[arg-type]


ACTIVE = ClassifierSettings(kind="systemone", mode="active")


@pytest.mark.parametrize(
    ("given", "mode", "expected"),
    [
        (None, "read", ("standard", "default")),
        (vote(None, None, "timeout"), "read", ("standard", "default")),
        (vote("cheap", None), "read", ("standard", "default")),
        (vote("cheap", 0.59), "read", ("standard", "default")),
        (vote("cheap", 0.6), "read", ("cheap", "classifier")),
        (vote("strong", 0.9), "read", ("strong", "classifier")),
        (vote("cheap", 0.9), "write", ("standard", "classifier")),
        (vote("strong", 0.9), "write", ("strong", "classifier")),
    ],
)
def test_resolve_tier_rule(given: Vote | None, mode: str, expected: tuple[str, str]) -> None:
    assert resolve_tier(given, mode, ACTIVE) == expected  # type: ignore[arg-type]


def test_resolve_tier_write_floor_only_for_write() -> None:
    settings = replace(ACTIVE, write_floor="strong")
    assert resolve_tier(vote("cheap", 0.9), "write", settings) == ("strong", "classifier")
    assert resolve_tier(vote("cheap", 0.9), "read", settings) == ("cheap", "classifier")


def test_resolve_tier_model_threshold() -> None:
    strict = ClassifierModel(
        "systemone", "systemone", "http://127.0.0.1:11434", "m", 3, min_confidence=0.9
    )
    loose = ClassifierModel(
        "systemone", "systemone", "http://127.0.0.1:11434", "m", 3, min_confidence=0.3
    )
    with_strict = replace(ACTIVE, primary="systemone", models=(strict,))
    with_loose = replace(ACTIVE, primary="systemone", models=(loose,))
    assert resolve_tier(vote("cheap", 0.8), "read", with_strict) == ("standard", "default")
    assert resolve_tier(vote("cheap", 0.4), "read", with_loose) == ("cheap", "classifier")


def test_report_split_by_mode_since() -> None:
    moment = T0 + timedelta(hours=1)
    before = [replace(row(f"b{n}", "cheap"), created_at=T0) for n in range(2)]
    after = [
        replace(row(f"a{n}", "strong"), created_at=moment + timedelta(minutes=n)) for n in range(3)
    ]
    result = report(before + after, f"active|{moment.isoformat()}")
    assert group(result, "systemone", False)["votes"] == 5  # overall totals unchanged
    split = result["split"]
    assert split["mode"] == "active" and split["since"] == moment.isoformat()
    [early] = split["before"]
    [late] = split["after"]
    assert early["classifier_id"] == "systemone" and early["votes"] == 2
    assert (
        late["classifier_id"] == "systemone" and late["votes"] == 3
    )  # the boundary counts as "after"
    assert late["weak"]["matched"] == 0 and early["weak"]["matched"] == 0


@pytest.mark.parametrize(
    "raw", [None, "garbage", "active|not-a-date", "", "active|2026-11-13T09:00:00"]
)
def test_report_without_valid_mode_since_has_no_split(raw: str | None) -> None:
    result = report([row("j1", "cheap")], raw)
    assert "split" not in result
    assert result["mode_since"] == raw


def settings_db(tmp_path: Path) -> tuple[SqliteStore, SqliteSettings]:
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    migrate_jobs_schema(store)
    return store, SqliteSettings(store)


def test_record_classifier_mode_only_on_change(tmp_path: Path) -> None:
    store, settings = settings_db(tmp_path)
    try:
        record_classifier_mode(settings, "shadow", T0)
        assert settings.get("classifier_mode_since") == f"shadow|{T0.isoformat()}"
        record_classifier_mode(settings, "shadow", T0 + timedelta(hours=1))
        assert settings.get("classifier_mode_since") == f"shadow|{T0.isoformat()}"
        later = T0 + timedelta(hours=2)
        record_classifier_mode(settings, "active", later)
        assert settings.get("classifier_mode_since") == f"active|{later.isoformat()}"
    finally:
        store.close()


def test_gateway_start_records_mode(home: Path, store, clock, tokens, bus) -> None:
    config = classifier_config(home, NEW_FORM.replace('mode = "shadow"', 'mode = "active"'))
    create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
    assert SqliteSettings(store).get("classifier_mode_since") == f"active|{clock.now().isoformat()}"
    clock.advance(timedelta(hours=1))
    create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
    assert SqliteSettings(store).get("classifier_mode_since") == f"active|{T0.isoformat()}"


def test_gateway_without_classifier_records_nothing(
    config, store, clock, tokens, home, bus
) -> None:
    create_app(config, store, clock, tokens, home=home, bus=bus, background=False)
    assert SqliteSettings(store).get("classifier_mode_since") is None


def test_cli_classifier_report_text_shows_split(doctor_home: Path) -> None:
    classifier_config(doctor_home, NEW_FORM)
    seed_votes(doctor_home)
    store = SqliteStore(doctor_home / "agent-router.sqlite")
    try:
        SqliteSettings(store).set("classifier_mode_since", f"active|{T0.isoformat()}")
    finally:
        store.close()
    result = CliRunner().invoke(cli.app, ["classifier", "report"])
    assert result.exit_code == 0, result.output
    assert f"mode active since {T0.isoformat()}" in result.output


# --- flags, export, primary ------------------------------------------------------------


def jobs_db(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    SqliteJobStore(store)
    return store


def test_settings_claim_and_release(tmp_path: Path) -> None:
    store = jobs_db(tmp_path)
    try:
        flags = SqliteSettings(store)
        assert flags.claim("lock", "a", lambda current: False) is None
        assert flags.get("lock") == "a"
        assert flags.claim("lock", "b", lambda current: False) == "a"  # busy — returns the owner
        assert flags.get("lock") == "a"
        # a stale value — the new claim takes over
        assert flags.claim("lock", "b", lambda current: current == "a") is None
        assert flags.get("lock") == "b"
        assert flags.release("lock", "a") is False  # someone else's flag is not released
        assert flags.get("lock") == "b"
        assert flags.release("lock", "b") is True and flags.get("lock") is None
        assert flags.release("lock", "b") is False
    finally:
        store.close()


def test_settings_claim_unless_blocker(tmp_path: Path) -> None:
    store = jobs_db(tmp_path)
    try:
        flags = SqliteSettings(store)
        flags.set("eval", "x")
        assert flags.claim_unless("busy", "1", "eval", lambda v: v == "x") is False
        assert flags.get("busy") is None
        assert flags.claim_unless("busy", "1", "eval", lambda v: False) is True
        assert flags.get("busy") == "1"
        flags.delete("eval")
        assert flags.claim_unless("busy", "2", "eval", lambda v: v is not None) is True
        assert flags.get("busy") == "2"
    finally:
        store.close()


def test_settings_claim_race_has_one_winner(tmp_path: Path) -> None:
    """Two processes (own connections) claim the flag at once — exactly one wins."""
    jobs_db(tmp_path).close()
    path = tmp_path / "agent-router.sqlite"
    stores = [SqliteStore(path), SqliteStore(path)]
    barrier = threading.Barrier(2)
    results: list[str | None] = [None, None]

    def worker(n: int) -> None:
        flags = SqliteSettings(stores[n])
        barrier.wait()
        won = flags.claim("lock", f"p{n}", lambda current: False) is None
        results[n] = "won" if won else "lost"

    try:
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert sorted(results) == ["lost", "won"]  # type: ignore[type-var]
    finally:
        for store in stores:
            store.close()


def test_eval_claim_is_atomic_and_refuses_when_other_wins() -> None:
    """Another eval wins the flag between our check and our write."""
    flags = Flags()
    ollama = Ollama(flags)

    def other_wins(key: str) -> None:
        if key == EVAL_FLAG:
            flags.values[EVAL_FLAG] = flag_value(999, T0)

    flags.before_claim = other_wins
    with pytest.raises(EvalBusy):
        run_eval([("j_1", "first")], [model("gemma", "ollama")], **eval_kwargs(flags, ollama, []))
    assert flags.values[EVAL_FLAG] == flag_value(999, T0)
    assert ollama.calls == []


def test_eval_does_not_release_flag_taken_over_by_other_run() -> None:
    flags = Flags()
    ollama = Ollama(flags)
    votes: list[tuple[str, Vote, datetime]] = []
    inner = ollama.post

    def post(address: str, *, json: dict[str, Any], timeout: float) -> Response:
        if address.endswith("/api/chat"):
            flags.values[EVAL_FLAG] = flag_value(999, T0)  # our flag is considered stale
        return inner(address, json=json, timeout=timeout)

    run_eval(
        [("j_1", "first")],
        [model("gemma", "ollama")],
        **eval_kwargs(flags, ollama, votes, post=post),
    )
    assert flags.values[EVAL_FLAG] == flag_value(999, T0)


def test_queue_does_not_start_when_eval_claims_first() -> None:
    """The eval claims the flag after the queue's check — the live request never starts."""
    order: list[str] = []
    flags = Flags()

    def eval_wins(key: str) -> None:
        if key == BUSY_FLAG:
            flags.values[EVAL_FLAG] = flag_value(4242, T0)

    flags.before_claim = eval_wins
    settings = replace(two_models(), models=(model("systemone"),))
    q = queue(settings, recording_post(order), Now(), flags, alive=lambda pid: pid == 4242)
    q.submit("j_1", "first", T0)
    assert q.step() is False
    assert order == [] and q.pending == 1 and BUSY_FLAG not in flags.values
    assert q.drain() == []
    flags.before_claim = None
    del flags.values[EVAL_FLAG]
    assert q.step() is True and order == ["first:systemone"]


def test_eval_and_live_queue_share_real_settings_atomically(tmp_path: Path) -> None:
    """Real table: busy is not set while the eval flag is fresh and is released
    after the live request."""
    store = jobs_db(tmp_path)
    try:
        flags = SqliteSettings(store)
        flags.set(EVAL_FLAG, flag_value(4242, T0))
        order: list[str] = []
        settings = replace(two_models(), models=(model("systemone"),))
        q = VoteQueue(
            settings,
            now=Now(),
            flags=flags,
            post=recording_post(order),
            clock=Ticks(),
            start=False,
            alive=lambda pid: True,
        )
        q.submit("j_1", "first", T0)
        assert q.step() is False and order == [] and flags.get(BUSY_FLAG) is None
        flags.delete(EVAL_FLAG)
        assert q.step() is True and order == ["first:systemone"]
        assert flags.get(BUSY_FLAG) is None
    finally:
        store.close()


def test_eval_aborts_when_live_unload_fails() -> None:
    """A live-model unload failure aborts the run before it starts; the flag is released."""
    flags = Flags()
    ollama = Ollama(flags)
    ollama.unload_status["lfm3-model"] = 500
    votes: list[tuple[str, Vote, datetime]] = []
    with pytest.raises(EvalError, match="lfm3-model"):
        run_eval(
            [("j_1", "first")],
            [model("gemma", "ollama", live=False)],
            **eval_kwargs(flags, ollama, votes),
        )
    assert votes == []
    assert not any(path == "/api/chat" for path, _ in ollama.calls)
    assert EVAL_FLAG not in flags.values


def test_eval_waits_until_live_models_leave_ps() -> None:
    flags = Flags()
    ollama = Ollama(flags)
    ollama.sticky["systemone-model"] = 3  # still visible in three more /api/ps responses
    ollama.ps_extra = ["other:latest"]  # an unrelated model does not block the wait
    votes: list[tuple[str, Vote, datetime]] = []
    sleeps: list[float] = []
    run_eval(
        [("j_1", "first")],
        [model("gemma", "ollama", live=False)],
        **eval_kwargs(flags, ollama, votes, sleep=sleeps.append),
    )
    assert len(votes) == 1
    paths = [path for path, _ in ollama.calls]
    first_chat = paths.index("/api/chat")
    assert paths[:first_chat].count("/api/ps") == 4
    assert len(sleeps) == 3


def test_eval_aborts_when_live_model_stays_loaded() -> None:
    flags = Flags()
    ollama = Ollama(flags)
    ollama.sticky["systemone-model"] = 10_000
    votes: list[tuple[str, Vote, datetime]] = []
    sleeps: list[float] = []
    with pytest.raises(EvalError, match="systemone-model"):
        run_eval(
            [("j_1", "first")],
            [model("gemma", "ollama", live=False)],
            **eval_kwargs(flags, ollama, votes, sleep=sleeps.append),
        )
    assert votes == [] and 0 < len(sleeps) <= 60  # the wait is bounded
    assert not any(path == "/api/chat" for path, _ in ollama.calls)
    assert EVAL_FLAG not in flags.values


@pytest.mark.parametrize("bad", ["status", "json"])
def test_eval_aborts_on_bad_ps_answer(bad: str) -> None:
    flags = Flags()
    ollama = Ollama(flags)
    get = ollama.get
    if bad == "status":
        ollama.ps_status = 500
    else:

        def get(address: str, *, timeout: float) -> Response:
            ollama.get(address, timeout=timeout)
            return Response(payload=ValueError("not JSON"))

    with pytest.raises(EvalError):
        run_eval(
            [("j_1", "first")],
            [model("gemma", "ollama", live=False)],
            **eval_kwargs(flags, ollama, [], get=get),
        )
    assert not any(path == "/api/chat" for path, _ in ollama.calls)
    assert EVAL_FLAG not in flags.values


def test_eval_offline_unload_failure_stops_before_next_model() -> None:
    flags = Flags()
    ollama = Ollama(flags)
    ollama.unload_status["gemma-model"] = 500
    votes: list[tuple[str, Vote, datetime]] = []
    with pytest.raises(EvalError, match="gemma-model"):
        run_eval(
            [("j_1", "first")],
            [model("gemma", "ollama", live=False), model("d1", "systemone", live=False)],
            **eval_kwargs(flags, ollama, votes),
        )
    assert [v.classifier_id for _, v, _ in votes] == ["gemma"]  # already-written votes stay
    assert not any(body and body.get("model") == "d1-model" for _, body in ollama.calls)
    assert EVAL_FLAG not in flags.values


def test_eval_busy_is_eval_error() -> None:
    assert issubclass(EvalBusy, EvalError)


def test_cli_classifier_eval_error_is_clear(doctor_home: Path, monkeypatch) -> None:
    classifier_config(doctor_home, NEW_FORM)
    seed_votes(doctor_home)

    def fail(*args: Any, **kwargs: Any) -> int:
        raise EvalError("live models not unloaded: tev1:4b-q4_K_M")

    monkeypatch.setattr(classifier_module, "run_eval", fail)
    result = CliRunner().invoke(cli.app, ["classifier", "eval", "--models", "gemma"])
    assert result.exit_code == 1
    assert "not unloaded" in result.output


@pytest.mark.parametrize("mode", ["shadow", "active"])
def test_primary_must_be_live(home: Path, mode: str) -> None:
    """A primary model with live = false is a config error in either mode."""
    body = NEW_FORM.replace('primary = "systemone"', 'primary = "gemma"').replace(
        'mode = "shadow"', f'mode = "{mode}"'
    )
    with pytest.raises(ConfigError, match="classifier.primary"):
        classifier_config(home, body)


def test_legacy_fields_follow_primary_not_first_model(home: Path) -> None:
    """Legacy fields and the snapshot follow primary even when it is listed second."""
    body = NEW_FORM.replace('primary = "systemone"', 'primary = "lfm3"')
    c = classifier_config(home, body).classifier
    assert (c.kind, c.url, c.model, c.timeout_s) == (
        "ollama",
        "http://localhost:11434",
        "lfm3:1.7b-q4_K_M",
        5,
    )
    assert c.primary_model is not None and c.primary_model.id == "lfm3"
    assert classifier_module.snapshot_classifier(c, (0, 0))["kind"] == "ollama"


def single_pool_with_caller(home: Path, caller_line: str) -> None:
    example = (
        Path(__file__).resolve().parent.parent / "docs" / "examples" / "opencode-and-cline.toml"
    )
    text = example.read_text(encoding="utf-8")
    head, tail = text.split("\n[callers]\n", 1)
    rest = tail.split("\n\n", 1)[1]
    write_config(home, f"{head}\n[callers]\n{caller_line}\n\n{rest}")


def test_doctor_warns_caller_without_other_pool(doctor_home) -> None:
    single_pool_with_caller(doctor_home, 'opencode = "opencode-go"')

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert (
        "caller opencode: every enabled route is on its own pool opencode-go, "
        "so its delegated jobs have no route"
    ) in result.output


def test_doctor_quiet_when_caller_not_mapped(doctor_home) -> None:
    single_pool_with_caller(doctor_home, 'opencode = ""')

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "every enabled route is on its own pool" not in result.output


def test_doctor_no_caller_warning_without_enabled_routes(doctor_home) -> None:
    single_pool_with_caller(doctor_home, 'opencode = "opencode-go"')
    path = doctor_home / "config.toml"
    text = path.read_text(encoding="utf-8")
    assert "enabled = true" in text
    path.write_text(text.replace("enabled = true", "enabled = false"), "utf-8")

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert "every enabled route is on its own pool" not in result.output
