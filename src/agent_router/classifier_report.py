"""Classifier accuracy and latency report over stored votes."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from agent_router.config import TIERS, Tier
from agent_router.jobs import VoteRow


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
