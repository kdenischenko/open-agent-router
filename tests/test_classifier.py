from __future__ import annotations

import math
from typing import Any

import httpx
import pytest

from agent_router.classifier import classify
from agent_router.config import ClassifierSettings


class Response:
    def __init__(self, status_code: int = 200, payload: Any = None, error: Exception | None = None):
        self.status_code = status_code
        self.payload = payload
        self.error = error

    def json(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.payload


def answer(score: Any = 1.0, confidence: Any = 0.3) -> dict[str, Any]:
    return {
        "answers": {
            "tier": {
                "type": "score",
                "score": score,
                "confidence": confidence,
                "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6},
                "legend": {"0": "cheap", "1": "standard", "2": "strong"},
            }
        }
    }


def settings(kind: str = "systemone", url: str = "http://localhost:11434") -> ClassifierSettings:
    return ClassifierSettings(kind=kind, url=url, model="tev1-test", timeout_s=1.25)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("score", "tier"),
    [(0.2, "cheap"), (0.9, "standard"), (2.0, "strong"), (0.5, "standard"), (1.5, "strong")],
)
def test_score_maps_to_nearest_tier_with_half_up(score: float, tier: str) -> None:
    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(payload=answer(score=score, confidence=0.3))

    assert classify("task", settings(), post) == (tier, 0.3)


@pytest.mark.parametrize("url", ["http://localhost:11434", "http://localhost:11434/"])
def test_request_has_systemone_shape(url: str) -> None:
    calls: list[tuple[str, dict[str, Any], float]] = []

    def post(address: str, *, json: dict[str, Any], timeout: float) -> Response:
        calls.append((address, json, timeout))
        return Response(payload=answer())

    assert classify("handle the task", settings(url=url), post) == ("standard", 0.3)
    [(address, body, timeout)] = calls
    assert address == "http://localhost:11434/v1/systemone"
    assert timeout == 1.25
    assert body["model"] == "tev1-test"
    assert body["state"] == {"prompt": "handle the task"}
    question = body["questions"]["tier"]
    assert question["type"] == "score"
    assert "prompt" in question["instructions"]
    assert "model" in question["instructions"].lower()
    assert len(question["criteria"]) == 3
    assert [item.split(":", 1)[0] for item in question["criteria"]] == [
        "cheap",
        "standard",
        "strong",
    ]


def test_long_prompt_keeps_first_3000_and_last_1000_characters() -> None:
    prompt = "".join(chr(32 + index % 90) for index in range(10_000))
    seen: list[str] = []

    def post(address: str, *, json: dict[str, Any], timeout: float) -> Response:
        seen.append(json["state"]["prompt"])
        return Response(payload=answer())

    assert classify(prompt, settings(), post) is not None
    [trimmed] = seen
    assert trimmed.startswith(prompt[:3000])
    assert trimmed.endswith(prompt[-1000:])
    assert trimmed == prompt[:3000] + "\n…\n" + prompt[-1000:]


def test_prompt_of_exactly_4000_characters_is_not_trimmed() -> None:
    prompt = "".join(chr(32 + index % 90) for index in range(4000))
    seen: list[str] = []

    def post(address: str, *, json: dict[str, Any], timeout: float) -> Response:
        seen.append(json["state"]["prompt"])
        return Response(payload=answer())

    assert classify(prompt, settings(), post) is not None
    assert seen == [prompt]


@pytest.mark.parametrize(
    "error",
    [httpx.TimeoutException("timeout"), httpx.ConnectError("offline"), RuntimeError("boom")],
)
def test_post_exception_returns_none(error: Exception) -> None:
    def post(*args: Any, **kwargs: Any) -> Response:
        raise error

    assert classify("task", settings(), post) is None


def test_non_200_response_returns_none() -> None:
    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(status_code=500, payload=answer())

    assert classify("task", settings(), post) is None


def test_non_json_response_returns_none() -> None:
    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(error=ValueError("not json"))

    assert classify("task", settings(), post) is None


@pytest.mark.parametrize("payload", [{}, {"answers": {}}, {"answers": []}, []])
def test_missing_answer_shape_returns_none(payload: Any) -> None:
    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(payload=payload)

    assert classify("task", settings(), post) is None


@pytest.mark.parametrize("score", ["1", None, True, math.nan, math.inf, -math.inf, -1, 3.2])
def test_invalid_score_returns_none(score: Any) -> None:
    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(payload=answer(score=score))

    assert classify("task", settings(), post) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"answers": {"tier": {"type": "score", "score": 1.0}}},
        answer(confidence=1.5),
        answer(confidence="0.3"),
        answer(confidence=True),
        answer(confidence=math.nan),
        answer(confidence=math.inf),
    ],
)
def test_invalid_confidence_returns_none(payload: dict[str, Any]) -> None:
    def post(*args: Any, **kwargs: Any) -> Response:
        return Response(payload=payload)

    assert classify("task", settings(), post) is None


def test_disabled_classifier_returns_none_without_post() -> None:
    called = False

    def post(*args: Any, **kwargs: Any) -> Response:
        nonlocal called
        called = True
        return Response(payload=answer())

    assert classify("task", settings(kind="none"), post) is None
    assert not called
