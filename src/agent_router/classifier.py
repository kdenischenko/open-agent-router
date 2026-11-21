"""Job classifiers: SystemOne, Ollama chat and OpenAI-compatible chat; voting and report."""

from __future__ import annotations

import contextlib
import heapq
import json
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from agent_router.clock import to_utc
from agent_router.config import TIERS, ClassifierModel, ClassifierSettings, Mode, Tier
from agent_router.jobs import TierSource, Vote, VoteRow
from agent_router.runner import process_image

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


CLASSIFY_BACKLOG = 64
EVAL_FLAG = "classifier_eval_running"
BUSY_FLAG = "classifier_live_busy"
_SECONDARY_DROP_S = 600.0  # drop a non-primary vote older than 10 minutes
_FLAG_STALE_S = 2 * 3600.0  # a flag older than two hours is considered stale
_EVAL_BUSY_WAIT_S = 30.0
_HTTP_TIMEOUT_S = 30.0
_UNLOAD_TIMEOUT_S = 30.0
_PS_POLLS = 20  # how many times to poll /api/ps after unloading live models

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


def flag_value(pid: int, moment: datetime) -> str:
    """Flag value string: pid and timestamp, used to check that another process is still alive."""
    return f"{int(pid)}|{to_utc(moment).isoformat()}"


def _default_alive(pid: int) -> bool:
    try:
        return process_image(pid) is not None
    except Exception:
        return True


def _flag_fresh(raw: str | None, now: datetime, alive: Callable[[int], bool]) -> bool:
    if not raw:
        return False
    pid_text, separator, moment_text = raw.partition("|")
    if not separator:
        return False
    try:
        pid = int(pid_text)
        moment = to_utc(datetime.fromisoformat(moment_text))
    except (TypeError, ValueError):
        return False
    if now - moment > timedelta(seconds=_FLAG_STALE_S):
        return False
    try:
        return alive(pid)
    except Exception:
        return True


class VoteQueue:
    """Single priority queue of votes: primary first, one worker thread."""

    def __init__(
        self,
        settings: ClassifierSettings,
        *,
        now: Callable[[], datetime],
        flags: Any = None,
        post: Callable[..., Any] = post,
        clock: Callable[[], float] = time.monotonic,
        alive: Callable[[int], bool] = _default_alive,
        backlog: int = CLASSIFY_BACKLOG,
        start: bool = True,
    ) -> None:
        self._now = now
        self._flags = flags
        self._post = post
        self._clock = clock
        self._alive = alive
        self._backlog = backlog
        self._cond = threading.Condition(threading.Lock())
        self._heap: list[tuple[int, int, str, str, datetime, ClassifierModel]] = []
        self._seq = 0
        self._ready: list[tuple[str, Vote]] = []
        self._closed = False
        self._thread: threading.Thread | None = None
        self._models = tuple(item for item in settings.effective_models if item.live)
        primary = settings.primary_model
        self.primary = None if primary is None else primary.id
        if start and self._models:
            self._thread = threading.Thread(target=self._run, name="classifier", daemon=True)
            self._thread.start()

    @property
    def pending(self) -> int:
        with self._cond:
            return len(self._heap)

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    def submit(self, job_id: str, prompt: str, created_at: datetime) -> None:
        with self._cond:
            if self._closed:
                return
            for model in self._models:
                is_primary = model.id == self.primary
                # the backlog limits only non-primary votes; primary is bounded by the job count
                if not is_primary and len(self._heap) >= self._backlog:
                    self._ready.append(
                        (job_id, Vote(model.id, model.model, None, None, 0, "dropped"))
                    )
                    continue
                priority = 0 if is_primary else 1
                heapq.heappush(self._heap, (priority, self._seq, job_id, prompt, created_at, model))
                self._seq += 1
            self._cond.notify()

    def _eval_fresh(self) -> bool:
        if self._flags is None:
            return False
        try:
            return _flag_fresh(self._flags.get(EVAL_FLAG), self._now(), self._alive)
        except Exception:
            return False

    def step(self) -> bool:
        """One unit of work, synchronously; False — queue is empty, closed, or eval is running."""
        with self._cond:
            if self._closed or self._eval_fresh() or not self._heap:
                return False
            priority, seq, job_id, prompt, created_at, model = heapq.heappop(self._heap)
            if priority == 1 and self._now() - created_at > timedelta(seconds=_SECONDARY_DROP_S):
                self._ready.append((job_id, Vote(model.id, model.model, None, None, 0, "dropped")))
                return True
        mine = flag_value(os.getpid(), self._now())
        hold: str | None = None
        if self._flags is not None:
            try:
                if not self._flags.claim_unless(
                    BUSY_FLAG,
                    mine,
                    EVAL_FLAG,
                    blocked=lambda cur: _flag_fresh(cur, self._now(), self._alive),
                ):
                    # eval claimed the flag first: put the item back with the same priority
                    with self._cond:
                        heapq.heappush(
                            self._heap, (priority, seq, job_id, prompt, created_at, model)
                        )
                    return False
                hold = mine
            except Exception:
                hold = None  # flags store unavailable — run the request without the flag
        try:
            vote = classify_one(prompt, model, post=self._post, clock=self._clock)
        finally:
            if hold is not None:
                with contextlib.suppress(Exception):
                    self._flags.release(BUSY_FLAG, hold)
        with self._cond:
            self._ready.append((job_id, vote))
        return True

    def drain(self) -> list[tuple[str, Vote]]:
        with self._cond:
            ready, self._ready = self._ready, []
            return ready

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._closed:
                    return
            if self.step():
                continue
            with self._cond:
                if self._closed:
                    return
                self._cond.wait(timeout=1.0)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        thread = self._thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join()


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


def _empty_matrix() -> dict[str, dict[str, int]]:
    return {truth: {vote: 0 for vote in TIERS} for truth in TIERS}


def _truth_of(row: VoteRow) -> tuple[Tier, bool] | None:
    """Ground-truth tier and a weak-label flag; None — the row does not count toward accuracy."""
    if row.rating_should_tier is not None:
        return row.rating_should_tier, False
    if row.tier_source == "caller" and row.rating_value == "good":
        return row.job_tier, False
    if row.tier_source == "caller" and row.rating_value is None:
        return row.job_tier, True
    return None


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def _group_stats(rows: list[VoteRow]) -> dict[str, Any]:
    errors = sum(1 for row in rows if row.error is not None)
    latencies = [row.latency_ms for row in rows if row.error is None]
    truth = _empty_matrix()
    weak = _empty_matrix()
    labeled = matched = under = 0
    weak_labeled = weak_matched = weak_under = 0
    for row in rows:
        if row.tier is None or row.error is not None:
            continue
        found = _truth_of(row)
        if found is None:
            continue
        truth_tier, is_weak = found
        target = weak if is_weak else truth
        target[truth_tier][row.tier] += 1
        if is_weak:
            weak_labeled += 1
            weak_matched += int(row.tier == truth_tier)
            weak_under += int(TIERS.index(row.tier) < TIERS.index(truth_tier))
        else:
            labeled += 1
            matched += int(row.tier == truth_tier)
            under += int(TIERS.index(row.tier) < TIERS.index(truth_tier))
    votes = len(rows)
    return {
        "votes": votes,
        "errors": errors,
        "error_rate": (errors / votes) if votes else 0.0,
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "truth": {
            "labeled": labeled,
            "matched": matched,
            "accuracy": (matched / labeled) if labeled else None,
            "under": under,
            "under_rate": (under / labeled) if labeled else None,
            "matrix": truth,
        },
        "weak": {
            "labeled": weak_labeled,
            "matched": weak_matched,
            "accuracy": (weak_matched / weak_labeled) if weak_labeled else None,
            "under": weak_under,
            "under_rate": (weak_under / weak_labeled) if weak_labeled else None,
            "matrix": weak,
        },
    }


def resolve_tier(vote: Vote | None, mode: Mode, s: ClassifierSettings) -> tuple[Tier, TierSource]:
    """Tier from the primary vote in active mode; without a usable vote falls back to standard.

    The threshold is the `min_confidence` of the model in `effective_models` with
    `id == vote.classifier_id` when set, otherwise `s.min_confidence`. In `write` mode
    the tier is never below `write_floor`.
    """
    if vote is None or vote.error is not None or vote.tier is None or vote.confidence is None:
        return "standard", "default"
    threshold = s.min_confidence
    model = next((item for item in s.effective_models if item.id == vote.classifier_id), None)
    if model is not None and model.min_confidence is not None:
        threshold = model.min_confidence
    if vote.confidence < threshold:
        return "standard", "default"
    tier = vote.tier
    if mode == "write":
        tier = TIERS[max(TIERS.index(tier), TIERS.index(s.write_floor))]
    return tier, "classifier"


def _classifier_groups(rows: list[VoteRow]) -> list[dict[str, Any]]:
    """Report groups by classifier and the live/offline flag (shared shape for all slices)."""
    groups: dict[tuple[str, bool], list[VoteRow]] = {}
    models: dict[tuple[str, bool], str] = {}
    for row in rows:
        key = (row.classifier_id, row.offline)
        groups.setdefault(key, []).append(row)
        models.setdefault(key, row.model)
    result: list[dict[str, Any]] = []
    for key in sorted(groups):
        classifier_id, offline = key
        result.append(
            {
                "classifier_id": classifier_id,
                "offline": offline,
                "model": models[key],
                **_group_stats(groups[key]),
            }
        )
    return result


def _mode_since_parts(mode_since: str | None) -> tuple[str, str, datetime] | None:
    """Parse `"<mode>|<ISO timestamp>"`; an unparseable string yields None without raising."""
    if not mode_since:
        return None
    mode, separator, moment_text = mode_since.partition("|")
    if not separator or not mode:
        return None
    try:
        moment = datetime.fromisoformat(moment_text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None  # a naive timestamp cannot be compared with job created_at
    return mode, moment_text, moment


def report(rows: list[VoteRow], mode_since: str | None) -> dict[str, Any]:
    """Classifier report: live and offline separately, ground truth and weak labels apart.

    With a `mode_since` string of the form `"<mode>|<ISO timestamp>"`, adds `split` — the
    same groups computed over jobs before and after the mode change.
    """
    result: dict[str, Any] = {
        "mode_since": mode_since,
        "classifiers": _classifier_groups(rows),
    }
    parts = _mode_since_parts(mode_since)
    if parts is not None:
        mode, moment_text, moment = parts
        result["split"] = {
            "mode": mode,
            "since": moment_text,
            "before": _classifier_groups([row for row in rows if row.created_at < moment]),
            "after": _classifier_groups([row for row in rows if row.created_at >= moment]),
        }
    return result


def snapshot_classifier(settings: ClassifierSettings, agreement: tuple[int, int]) -> dict[str, Any]:
    """Dashboard snapshot: base fields always; mode and model list added when models are set."""
    matched, compared = agreement
    result: dict[str, Any] = {
        "kind": settings.kind,
        "matched": matched,
        "compared": compared,
    }
    if settings.models:
        result["mode"] = settings.mode
        result["models"] = [
            {"id": item.id, "kind": item.kind, "model": item.model, "live": item.live}
            for item in settings.effective_models
        ]
    return result
