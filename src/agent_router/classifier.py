"""Classifier report and tier resolution; eval in classifier_eval, votes in classifier_votes."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from agent_router.classifier_backends import classify, get, post
from agent_router.classifier_backends import classify_one as classify_one
from agent_router.classifier_backends import is_loopback_url as is_loopback_url
from agent_router.classifier_eval import EvalBusy, EvalError, is_local_ollama, run_eval
from agent_router.classifier_votes import (
    BUSY_FLAG,
    CLASSIFY_BACKLOG,
    EVAL_FLAG,
    VoteQueue,
    flag_value,
)
from agent_router.config import TIERS, ClassifierSettings, Mode, Tier
from agent_router.jobs import TierSource, Vote, VoteRow

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
