"""Parsing of the [classifier] config section: single-model and [[classifier.models]] forms."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from agent_router.config_model import (
    TIERS,
    ClassifierModel,
    ClassifierSettings,
)
from agent_router.config_values import (
    _boolean,
    _check_keys,
    _choice,
    _integer,
    _number,
    _table,
    _text,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _check_api_key_env(value: Any, label: str, errors: list[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and _ENV_NAME.fullmatch(value) is not None:
        return value
    errors.append(f"{label}: not a valid environment variable name")
    return None


def _check_classifier_address(
    url: str, api_key_env: str | None, label: str, errors: list[str]
) -> None:
    try:
        parsed = urlparse(url)
        _ = parsed.port  # raises on a port that is not a number in 0..65535
    except ValueError:
        parsed = None
    host = parsed.hostname if parsed is not None else None
    if host in _LOOPBACK_HOSTS:
        return
    if parsed is not None and parsed.scheme == "https" and api_key_env:
        return
    errors.append(f"{label}: address must be loopback, or https with api_key_env")


_CLASSIFIER_MODEL_KEYS = frozenset(
    {"id", "kind", "url", "model", "timeout_s", "live", "min_confidence", "api_key_env"}
)

_CLASSIFIER_KINDS = ("systemone", "ollama", "openai")


def _parse_classifier_common(raw: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    """Settings shared by the single-model form and [[classifier.models]]."""
    mode = _choice(
        raw.get("mode", "shadow"), ("shadow", "active"), "classifier.mode", errors, "shadow"
    )
    min_confidence = _number(
        raw.get("min_confidence", 0.6), "classifier.min_confidence", errors, 0.6
    )
    if not 0 <= min_confidence <= 1:
        errors.append("classifier.min_confidence: must be in the range 0..1")
    write_floor = _choice(
        raw.get("write_floor", "standard"), TIERS, "classifier.write_floor", errors, "standard"
    )
    wait_s = None
    if raw.get("wait_s") is not None:
        wait_s = _number(raw.get("wait_s"), "classifier.wait_s", errors, 0.0)
        if wait_s < 0:
            errors.append("classifier.wait_s: cannot be negative")
    vram_budget_mb = _integer(
        raw.get("vram_budget_mb", 5300), "classifier.vram_budget_mb", errors, 5300
    )
    if vram_budget_mb <= 0:
        errors.append("classifier.vram_budget_mb: must be positive")
    return {
        "mode": mode,
        "min_confidence": min_confidence,
        "write_floor": write_floor,
        "wait_s": wait_s,
        "vram_budget_mb": vram_budget_mb,
    }


def _parse_classifier_models(raw: dict[str, Any], errors: list[str]) -> ClassifierSettings:
    for key in ("kind", "url", "model", "timeout_s", "api_key_env"):
        if key in raw:
            errors.append("classifier: old form and [[classifier.models]] used together")
            break
    common = _parse_classifier_common(raw, errors)
    raw_models = raw.get("models") or []
    if not isinstance(raw_models, list) or any(not isinstance(item, dict) for item in raw_models):
        errors.append("classifier.models: expected a list of tables")
        raw_models = []
    models: list[ClassifierModel] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_models):
        label = f"classifier.models[{index}]"
        _check_keys(item, _CLASSIFIER_MODEL_KEYS, label, errors)
        model_id = _text(item.get("id"), f"{label}.id", errors)
        if model_id in seen:
            errors.append(f"{label}.id: duplicate id: {model_id!r}")
        seen.add(model_id)
        kind = _choice(item.get("kind"), _CLASSIFIER_KINDS, f"{label}.kind", errors, "systemone")
        url = _text(item.get("url", "http://localhost:11434"), f"{label}.url", errors)
        raw_key_env = item.get("api_key_env")
        address_key_env = raw_key_env if isinstance(raw_key_env, str) and raw_key_env else None
        _check_classifier_address(url, address_key_env, f"{label}.url", errors)
        api_key_env = _check_api_key_env(raw_key_env, f"{label}.api_key_env", errors)
        model = _text(item.get("model"), f"{label}.model", errors)
        timeout_s = _number(item.get("timeout_s", 3), f"{label}.timeout_s", errors, 3)
        live = _boolean(item.get("live", True), f"{label}.live", errors, True)
        model_min: float | None = None
        if item.get("min_confidence") is not None:
            model_min = _number(item.get("min_confidence"), f"{label}.min_confidence", errors, 0.0)
            if not 0 <= model_min <= 1:
                errors.append(f"{label}.min_confidence: must be in the range 0..1")
                model_min = None
        models.append(
            ClassifierModel(
                id=model_id,
                kind=kind,
                url=url,
                model=model,
                timeout_s=timeout_s,
                live=live,
                min_confidence=model_min,
                api_key_env=api_key_env,
            )
        )
    primary = raw.get("primary")
    if primary is not None and not isinstance(primary, str):
        errors.append("classifier.primary: a non-empty string is required")
        primary = None
    if primary is None and models:
        primary = models[0].id
    if primary is not None and primary not in seen:
        errors.append(f"classifier.primary: unknown classifier: {primary!r}")
    # legacy fields come from the deciding model; without an explicit primary it is also the first
    chosen = next((item for item in models if item.id == primary), None)
    if chosen is not None and not chosen.live:
        errors.append(
            f"classifier.primary: deciding model {chosen.id!r} is disabled (live = false)"
        )
    return ClassifierSettings(
        kind=chosen.kind if chosen is not None else "none",
        url=chosen.url if chosen is not None else "http://localhost:11434",
        model=chosen.model if chosen is not None else "tev1:4b-q4_K_M",
        timeout_s=chosen.timeout_s if chosen is not None else 3,
        api_key_env=chosen.api_key_env if chosen is not None else None,
        primary=primary,
        models=tuple(models),
        **common,
    )


_CLASSIFIER_KEYS = frozenset(
    {
        "kind",
        "url",
        "model",
        "timeout_s",
        "mode",
        "min_confidence",
        "write_floor",
        "wait_s",
        "vram_budget_mb",
        "models",
        "primary",
        "api_key_env",
    }
)


def _parse_classifier(value: Any, errors: list[str]) -> ClassifierSettings:
    raw = _table(value, "classifier", errors)
    _check_keys(raw, _CLASSIFIER_KEYS, "classifier", errors)
    if "models" in raw:
        return _parse_classifier_models(raw, errors)
    url = _text(raw.get("url", "http://localhost:11434"), "classifier.url", errors)
    raw_key_env = raw.get("api_key_env")
    address_key_env = raw_key_env if isinstance(raw_key_env, str) and raw_key_env else None
    _check_classifier_address(url, address_key_env, "classifier.url", errors)
    api_key_env = _check_api_key_env(raw_key_env, "classifier.api_key_env", errors)
    common = _parse_classifier_common(raw, errors)
    return ClassifierSettings(
        kind=_choice(
            raw.get("kind", "none"),
            ("none",) + _CLASSIFIER_KINDS,
            "classifier.kind",
            errors,
            "none",
        ),
        url=url,
        model=_text(raw.get("model", "tev1:4b-q4_K_M"), "classifier.model", errors),
        timeout_s=_number(raw.get("timeout_s", 3), "classifier.timeout_s", errors, 3),
        api_key_env=api_key_env,
        **common,
    )
