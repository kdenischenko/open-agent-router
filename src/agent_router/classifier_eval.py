"""Classifier offline evaluation: replaying past prompts and unloading live models."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

from agent_router.classifier_backends import classify_one, is_loopback_url
from agent_router.classifier_votes import (
    BUSY_FLAG,
    EVAL_FLAG,
    _default_alive,
    _flag_fresh,
    flag_value,
)
from agent_router.config import ClassifierModel
from agent_router.jobs import Vote

_EVAL_BUSY_WAIT_S = 30.0
_HTTP_TIMEOUT_S = 30.0
_UNLOAD_TIMEOUT_S = 30.0
_PS_POLLS = 20  # how many times to poll /api/ps after unloading live models


def is_local_ollama(model: ClassifierModel) -> bool:
    """Whether classifier memory can be managed through a loopback Ollama API."""
    return model.kind in ("systemone", "ollama") and is_loopback_url(model.url)


class EvalError(Exception):
    """Offline evaluation error: unloading or checking live models."""


class EvalBusy(EvalError):
    """Offline evaluation is already running in another process."""


def _unload(post: Callable[..., Any], model: ClassifierModel) -> None:
    response = post(
        f"{model.url.rstrip('/')}/api/generate",
        json={"model": model.model, "keep_alive": 0},
        timeout=_UNLOAD_TIMEOUT_S,
    )
    if response.status_code != 200:
        raise EvalError(f"failed to unload model {model.model}: http {response.status_code}")


def _ps_model_names(payload: Any) -> list[str] | None:
    """Names of loaded models from the /api/ps response; None — the response is unexpected."""
    if not isinstance(payload, dict):
        return None
    items = payload.get("models")
    if not isinstance(items, list):
        return None
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("name", "model"):
            value = item.get(key)
            if isinstance(value, str):
                names.append(value)
    return names


def _wait_live_unloaded(
    get: Callable[..., Any],
    live: list[ClassifierModel],
    sleep: Callable[[float], None],
) -> None:
    """Waits for the configured live models to disappear from their server's /api/ps."""
    grouped: dict[str, list[ClassifierModel]] = {}
    for item in live:
        grouped.setdefault(item.url.rstrip("/"), []).append(item)
    for base, items in grouped.items():
        expected = {item.model for item in items}
        remaining = sorted(expected)
        for _ in range(_PS_POLLS):
            response = get(f"{base}/api/ps", timeout=_HTTP_TIMEOUT_S)
            if response.status_code != 200:
                raise EvalError(
                    f"failed to check unloading: {base}/api/ps — http {response.status_code}"
                )
            try:
                names = _ps_model_names(response.json())
            except Exception as exc:
                raise EvalError(
                    f"failed to check unloading: {base}/api/ps — response is not JSON"
                ) from exc
            if names is None:
                raise EvalError(f"failed to check unloading: {base}/api/ps — no models list")
            remaining = sorted(expected & set(names))
            if not remaining:
                break
            sleep(0.5)
        else:
            raise EvalError(f"live models not unloaded: {', '.join(remaining)}")


def _wait_busy(
    flags: Any,
    now: Callable[[], datetime],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    alive: Callable[[int], bool],
) -> None:
    deadline = clock() + _EVAL_BUSY_WAIT_S
    while _flag_fresh(flags.get(BUSY_FLAG), now(), alive):
        if clock() >= deadline:
            return
        sleep(0.5)


def run_eval(
    prompts: list[tuple[str, str]],
    models: list[ClassifierModel],
    *,
    live: list[ClassifierModel],
    flags: Any,
    add_vote: Callable[[str, Vote, datetime], None],
    now: Callable[[], datetime],
    pid: int,
    post: Callable[..., Any],
    get: Callable[..., Any],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    alive: Callable[[int], bool] = _default_alive,
) -> int:
    """Offline evaluation of past jobs; only local Ollama models are unloaded."""
    mine = flag_value(pid, now())
    owner = flags.claim(EVAL_FLAG, mine, free=lambda cur: not _flag_fresh(cur, now(), alive))
    if owner is not None:
        owner_pid = owner.split("|", 1)[0]
        raise EvalBusy(f"offline evaluation is already running (pid {owner_pid})")
    try:
        _wait_busy(flags, now, clock, sleep, alive)
        local_live = [model for model in live if is_local_ollama(model)]
        if local_live:
            for model in local_live:
                _unload(post, model)
            _wait_live_unloaded(get, local_live, sleep)
        count = 0
        for model in models:
            for job_id, prompt in prompts:
                vote = classify_one(prompt, model, post=post, clock=clock)
                add_vote(job_id, replace(vote, offline=True), now())
                count += 1
            if is_local_ollama(model):
                _unload(post, model)
        return count
    finally:
        flags.release(EVAL_FLAG, mine)
