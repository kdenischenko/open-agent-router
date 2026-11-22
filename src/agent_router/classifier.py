"""Facade re-exporting classifier votes/eval/report; tier resolution and dashboard snapshot here."""

from __future__ import annotations

from typing import Any

from agent_router.classifier_backends import classify, get, post
from agent_router.classifier_backends import classify_one as classify_one
from agent_router.classifier_backends import is_loopback_url as is_loopback_url
from agent_router.classifier_eval import EvalBusy, EvalError, is_local_ollama, run_eval
from agent_router.classifier_report import report
from agent_router.classifier_votes import (
    BUSY_FLAG,
    CLASSIFY_BACKLOG,
    EVAL_FLAG,
    VoteQueue,
    flag_value,
)
from agent_router.config import TIERS, ClassifierSettings, Mode, Tier
from agent_router.jobs import TierSource, Vote

# backend clients live in classifier_backends; callers keep reaching them through here
__all__ = [
    "BUSY_FLAG",
    "classify",
    "CLASSIFY_BACKLOG",
    "EVAL_FLAG",
    "EvalBusy",
    "EvalError",
    "flag_value",
    "get",
    "is_local_ollama",
    "post",
    "report",
    "resolve_tier",
    "run_eval",
    "snapshot_classifier",
    "VoteQueue",
]


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
