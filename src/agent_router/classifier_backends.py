"""Classifier backend clients: SystemOne, Ollama chat and OpenAI-compatible chat."""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from agent_router.config import TIERS, ClassifierModel, ClassifierSettings, Tier
from agent_router.jobs import Vote

_INSTRUCTIONS = "Which model tier is required to complete the task in `prompt`?"
_CRITERIA = [
    "cheap: A simple, routine task for which a low-cost model is sufficient.",
    "standard: A task requiring ordinary reasoning, coding, or several steps.",
    "strong: A complex, ambiguous, or high-stakes task requiring the strongest model.",
]


def _prompt(prompt: str) -> str:
    if len(prompt) <= 4000:
        return prompt
    return prompt[:3000] + "\n…\n" + prompt[-1000:]


def _number(value: Any, low: float, high: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and low <= value <= high
    )


class _NoApiKey(Exception):
    pass


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_loopback_url(url: str) -> bool:
    """Whether the URL host is loopback; such requests bypass the environment proxy."""
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


def post(url: str, **kwargs: Any) -> httpx.Response:
    """httpx.post that reaches a loopback URL directly, without the environment proxy."""
    return httpx.post(url, trust_env=not is_loopback_url(url), **kwargs)


def get(url: str, **kwargs: Any) -> httpx.Response:
    """httpx.get that reaches a loopback URL directly, without the environment proxy."""
    return httpx.get(url, trust_env=not is_loopback_url(url), **kwargs)


def _post(m: ClassifierModel, post: Callable[..., Any], address: str, body: dict[str, Any]) -> Any:
    if m.api_key_env is None:
        return post(address, json=body, timeout=m.timeout_s)
    key = os.environ.get(m.api_key_env)
    if not key:
        raise _NoApiKey
    return post(
        address,
        json=body,
        timeout=m.timeout_s,
        headers={"Authorization": f"Bearer {key}"},
    )


def classify(
    prompt: str, settings: ClassifierSettings, post: Callable[..., Any] = post
) -> tuple[Tier, float] | None:
    if settings.kind != "systemone":
        return None
    body = {
        "model": settings.model,
        "state": {"prompt": _prompt(prompt)},
        "questions": {
            "tier": {
                "type": "score",
                "instructions": _INSTRUCTIONS,
                "criteria": _CRITERIA,
            }
        },
    }
    try:
        m = settings.effective_models[0]
        response = _post(m, post, f"{m.url.rstrip('/')}/v1/systemone", body)
        if response.status_code != 200:
            return None
        answer = response.json()["answers"]["tier"]
        if not isinstance(answer, dict) or answer.get("type") != "score":
            return None
        score = answer["score"]
        confidence = answer["confidence"]
        if not _number(score, 0, 2) or not _number(confidence, 0, 1):
            return None
        return TIERS[int(score + 0.5)], float(confidence)
    except Exception:
        return None


_OLLAMA_SYSTEM = "\n".join(_CRITERIA) + (
    "\nAnswer with a single JSON object and no explanation:"
    ' {"tier": "cheap"|"standard"|"strong", "confidence": 0..1}.'
)
_OLLAMA_FORMAT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tier": {"type": "string", "enum": list(TIERS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["tier", "confidence"],
}
# strict structured outputs accept only closed objects and no numeric range keywords
_OPENAI_FORMAT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tier": {"type": "string", "enum": list(TIERS)},
        "confidence": {"type": "number"},
    },
    "required": ["tier", "confidence"],
    "additionalProperties": False,
}


def _latency_ms(started: float, clock: Callable[[], float]) -> int:
    return int(round((clock() - started) * 1000))


def _systemone_answer(
    prompt: str, m: ClassifierModel, post: Callable[..., Any]
) -> tuple[Tier | None, float | None, str | None]:
    body = {
        "model": m.model,
        "state": {"prompt": _prompt(prompt)},
        "questions": {
            "tier": {
                "type": "score",
                "instructions": _INSTRUCTIONS,
                "criteria": _CRITERIA,
            }
        },
    }
    response = _post(m, post, f"{m.url.rstrip('/')}/v1/systemone", body)
    if response.status_code != 200:
        return None, None, f"http_{response.status_code}"
    try:
        answer = response.json()["answers"]["tier"]
    except (KeyError, TypeError, AttributeError, ValueError):
        return None, None, "bad_answer"
    if not isinstance(answer, dict) or answer.get("type") != "score":
        return None, None, "bad_answer"
    score = answer.get("score")
    confidence = answer.get("confidence")
    if not _number(score, 0, 2) or not _number(confidence, 0, 1):
        return None, None, "bad_answer"
    index = int(score + 0.5)
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, dict) and _number(probabilities.get(str(index)), 0, 1):
        confidence = float(probabilities[str(index)])
    return TIERS[index], float(confidence), None


def _tier_logprob_confidence(logprobs: Any, content: str, tier: Tier) -> float | None:
    """Tier probability from the tokens of the value following the `"tier"` key."""
    if not isinstance(logprobs, list) or not logprobs:
        return None
    tokens: list[str] = []
    for entry in logprobs:
        if not isinstance(entry, dict) or not isinstance(entry.get("token"), str):
            return None
        tokens.append(entry["token"])
    if "".join(tokens) != content:
        return None
    key = content.find('"tier"')
    if key < 0:
        return None
    colon = content.find(":", key)
    if colon < 0:
        return None
    start = colon + 1
    while start < len(content) and content[start] in ' \t\r\n"':
        start += 1
    end = start + len(tier)
    if content[start:end] != tier:
        return None
    product = 1.0
    offset = 0
    for entry in logprobs:
        token = entry["token"]
        token_end = offset + len(token)
        if token_end > start and offset < end:
            logprob = entry.get("logprob")
            if not _number(logprob, -math.inf, 0):
                return None
            product *= math.exp(float(logprob))
        offset = token_end
    return product if product > 0 else None


def _ollama_answer(
    prompt: str, m: ClassifierModel, post: Callable[..., Any]
) -> tuple[Tier | None, float | None, str | None]:
    body = {
        "model": m.model,
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": 4096},
        "format": _OLLAMA_FORMAT,
        "messages": [
            {"role": "system", "content": _OLLAMA_SYSTEM},
            {"role": "user", "content": _prompt(prompt)},
        ],
        "logprobs": True,
        "top_logprobs": 3,
    }
    response = _post(m, post, f"{m.url.rstrip('/')}/api/chat", body)
    if response.status_code != 200:
        return None, None, f"http_{response.status_code}"
    try:
        data = response.json()
        content = data["message"]["content"]
    except (KeyError, TypeError, AttributeError, ValueError):
        return None, None, "bad_answer"
    if not isinstance(content, str):
        return None, None, "bad_answer"
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None, None, "bad_answer"
    if not isinstance(payload, dict):
        return None, None, "bad_answer"
    tier = payload.get("tier")
    confidence = payload.get("confidence")
    if tier not in TIERS or not _number(confidence, 0, 1):
        return None, None, "bad_answer"
    from_logprobs = _tier_logprob_confidence(data.get("logprobs"), content, tier)
    if from_logprobs is not None:
        confidence = from_logprobs
    return tier, float(confidence), None


def _openai_answer(
    prompt: str, m: ClassifierModel, post: Callable[..., Any]
) -> tuple[Tier | None, float | None, str | None]:
    body = {
        "model": m.model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _OLLAMA_SYSTEM},
            {"role": "user", "content": _prompt(prompt)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "tier", "strict": True, "schema": _OPENAI_FORMAT},
        },
    }
    response = _post(m, post, f"{m.url.rstrip('/')}/chat/completions", body)
    if response.status_code != 200:
        return None, None, f"http_{response.status_code}"
    try:
        data = response.json()
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        return None, None, "bad_answer"
    if not isinstance(content, str):
        return None, None, "bad_answer"
    text = content.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[len("```json") : -len("```")].strip()
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, None, "bad_answer"
    if not isinstance(payload, dict):
        return None, None, "bad_answer"
    tier = payload.get("tier")
    confidence = payload.get("confidence")
    if tier not in TIERS or not _number(confidence, 0, 1):
        return None, None, "bad_answer"
    logprobs = choice.get("logprobs")
    logprobs = logprobs.get("content") if isinstance(logprobs, dict) else None
    from_logprobs = _tier_logprob_confidence(logprobs, content, tier)
    if from_logprobs is not None:
        confidence = from_logprobs
    return tier, float(confidence), None


def classify_one(
    prompt: str,
    m: ClassifierModel,
    post: Callable[..., Any] = post,
    clock: Callable[[], float] = time.monotonic,
) -> Vote:
    """One vote through the configured backend; failures become the error field."""
    started = clock()
    try:
        if m.kind == "systemone":
            tier, confidence, error = _systemone_answer(prompt, m, post)
        elif m.kind == "ollama":
            tier, confidence, error = _ollama_answer(prompt, m, post)
        elif m.kind == "openai":
            tier, confidence, error = _openai_answer(prompt, m, post)
        else:
            tier, confidence, error = None, None, "bad_answer"
    except _NoApiKey:
        tier, confidence, error = None, None, "no_api_key"
    except httpx.TimeoutException:
        tier, confidence, error = None, None, "timeout"
    except Exception:
        tier, confidence, error = None, None, "unavailable"
    return Vote(m.id, m.model, tier, confidence, _latency_ms(started, clock), error)
