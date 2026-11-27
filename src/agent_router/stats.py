"""Dashboard stats: read-only aggregates of jobs, attempts and spend as inline SVG charts.

The numbers come straight from what the gateway stored; nothing here writes to a store.
Charts are drawn as SVG strings on the server, coloured only through CSS classes, so the
theme tokens in app.css stay the single place where colours live. The SVG carries the
geometry; the legend markup under each chart carries the exact numbers.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from typing import Any

from agent_router.clock import format_age
from agent_router.jobstore import JobStore

# range key, window length in hours, label
RANGES: tuple[tuple[str, int, str], ...] = (
    ("24h", 24, "24 hours"),
    ("7d", 24 * 7, "7 days"),
    ("30d", 24 * 30, "30 days"),
)
# more routes than this in one chart turns the colours into noise
MAX_CHART_ROUTES = 6
SERIES_CLASSES = tuple(f"chart-color--{number}" for number in range(1, MAX_CHART_ROUTES + 1))

# the line chart canvas: viewBox units, scaled to the card width by CSS
_LINE_W = 640
_LINE_H = 220
_LINE_LEFT = 52
_LINE_RIGHT = 628
_LINE_TOP = 14
_LINE_BOTTOM = 186

# the bar chart canvas: route labels right-aligned, bars, then a short note
_BAR_LABEL_X = 148
_BAR_START_X = 154
_BAR_END_X = 540
_BAR_NOTE_X = 548
_BAR_ROW_H = 24
_BAR_H = 10

STATUSES_CHARTED = ("completed", "partial", "failed")
RATINGS_CHARTED = ("good", "partial", "bad")


def parse_range(value: str | None) -> tuple[str, int, str]:
    """Range key, hours and label; unknown or missing values fall back to 24 hours."""
    for key, hours, label in RANGES:
        if value == key:
            return key, hours, label
    return RANGES[0]


def _bucket_span(hours: int) -> timedelta:
    if hours <= 24:
        return timedelta(hours=1)
    if hours <= 24 * 7:
        return timedelta(hours=6)
    return timedelta(hours=24)


def _bucket_note(span: timedelta) -> str:
    hours = span.total_seconds() / 3600
    if hours == 1:
        return "hourly buckets"
    if hours == 24:
        return "daily buckets"
    return f"{hours:g}-hour buckets"


def _tokens_text(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 10_000:
        return f"{round(value / 1000)}k"
    if value >= 1_000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return str(int(value))


def _cost_text(value: float) -> str:
    if value < 1:
        return f"${value:.4f}"
    return f"${value:,.2f}"


def _plural(count: int, singular: str) -> str:
    """`3 jobs`, `1 job`: chart legends read like sentences, not like database rows."""
    return f"{count} {singular}{'s' if count != 1 else ''}"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _nice_step(value: float) -> float:
    """The smallest 1/2/5×10ⁿ step that is at least `value`."""
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    for factor in (1, 2, 5, 10):
        step = magnitude * factor
        if step >= value:
            return step
    return magnitude * 10


def _short_route(route: str, limit: int = 22) -> str:
    return route if len(route) <= limit else route[: limit - 1] + "…"


@dataclass
class Series:
    """One route's token totals over the time buckets."""

    route: str
    values: list[int]
    total: int = 0


@dataclass
class BarSegment:
    css_class: str
    value: float


@dataclass
class BarTick:
    css_class: str
    value: float


@dataclass
class BarRow:
    label: str
    segments: list[BarSegment] = field(default_factory=list)
    ticks: list[BarTick] = field(default_factory=list)
    note: str = ""


def _svg(width: int, height: int, aria: str, body: str) -> str:
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(aria)}">{body}</svg>'
    )


def _y_position(value: float, ceiling: float) -> float:
    plot = _LINE_BOTTOM - _LINE_TOP
    return _LINE_BOTTOM - (value / ceiling if ceiling else 0.0) * plot


def _tokens_svg(
    series: list[Series],
    starts: list[datetime],
    now: datetime,
    label: Callable[[datetime], str],
    range_label: str,
) -> str:
    count = len(starts)
    ceiling = max((max(item.values) for item in series), default=0) or 1
    step = _nice_step(ceiling / 3)
    ceiling = step * 3
    width = _LINE_RIGHT - _LINE_LEFT
    parts: list[str] = []
    for number in range(4):
        value = number * step
        y = _y_position(value, ceiling)
        parts.append(f'<line class="chart-grid" x1="{_LINE_LEFT}" y1="{y:.1f}"')
        parts.append(f' x2="{_LINE_RIGHT}" y2="{y:.1f}"/>')
        parts.append(
            f'<text class="chart-label" x="{_LINE_LEFT - 8}" y="{y + 3.5:.1f}"'
            f' text-anchor="end">{_tokens_text(value)}</text>'
        )
    for index in (0, count // 2, count - 1):
        x = _LINE_LEFT + width * index / (count - 1)
        # the ends label the range boundaries: `since` at the left, now at the right
        moment = now if index == count - 1 else starts[index]
        anchor = "start" if index == 0 else "end" if index == count - 1 else "middle"
        parts.append(
            f'<text class="chart-label" x="{x:.1f}" y="{_LINE_H - 12}"'
            f' text-anchor="{anchor}">{escape(label(moment))}</text>'
        )
    for number, item in enumerate(series):
        points = " ".join(
            f"{_LINE_LEFT + width * index / (count - 1):.1f},{_y_position(value, ceiling):.1f}"
            for index, value in enumerate(item.values)
        )
        parts.append(
            f'<polyline class="chart-series {SERIES_CLASSES[number % len(SERIES_CLASSES)]}"'
            f' points="{points}"/>'
        )
    totals = ", ".join(f"{item.route} {_tokens_text(item.total)}" for item in series)
    return _svg(
        _LINE_W,
        _LINE_H,
        f"line chart of tokens per route over the last {range_label}: {totals}",
        "".join(parts),
    )


def _bars_svg(rows: list[BarRow], aria: str) -> str:
    width = 640
    plot = _BAR_END_X - _BAR_START_X
    scale = max((sum(segment.value for segment in row.segments) for row in rows), default=0) or 1.0
    for row in rows:
        for tick in row.ticks:
            scale = max(scale, tick.value)
    height = 8 + _BAR_ROW_H * len(rows)
    parts: list[str] = []
    for number, row in enumerate(rows):
        top = 6 + number * _BAR_ROW_H
        parts.append(
            f'<text class="chart-label" x="{_BAR_LABEL_X}" y="{top + 14}"'
            f' text-anchor="end">{escape(row.label)}</text>'
        )
        offset = 0.0
        for segment in row.segments:
            fraction = segment.value / scale
            bar_width = fraction * plot
            if bar_width >= 0.5:
                parts.append(
                    f'<rect class="chart-bar {segment.css_class}"'
                    f' x="{_BAR_START_X + offset * plot:.1f}" y="{top + 7}"'
                    f' width="{bar_width:.1f}" height="{_BAR_H}" rx="2"/>'
                )
            offset += fraction
        for tick in row.ticks:
            x = _BAR_START_X + min(1.0, tick.value / scale) * plot
            parts.append(
                f'<line class="chart-tick {tick.css_class}" x1="{x:.1f}" y1="{top + 4}"'
                f' x2="{x:.1f}" y2="{top + 4 + _BAR_H + 6}"/>'
            )
        if row.note:
            parts.append(
                f'<text class="chart-label" x="{_BAR_NOTE_X}" y="{top + 14}"'
                f' text-anchor="start">{escape(row.note)}</text>'
            )
    return _svg(width, height, aria, "".join(parts))


def stats_view(
    jobs: JobStore, now: datetime, range_value: str | None, label: Callable[[datetime], str]
) -> dict[str, Any]:
    """Template context for the stats page: aggregates of what the gateway stored.

    `label` formats a moment for chart axes in the dashboard time zone.
    """
    key, hours, range_label = parse_range(range_value)
    since = now - timedelta(hours=hours)
    attempts = jobs.attempt_stats_since(since)
    job_rows = jobs.job_stats_since(since)
    spend_rows = jobs.spend_stats_since(since)

    span = _bucket_span(hours)
    count = max(1, round(timedelta(hours=hours) / span))
    starts = [now - (count - index) * span for index in range(count)]

    def bucket_of(moment: datetime) -> int:
        index = (moment - starts[0]) // span
        return min(count - 1, max(0, index))

    tokens: dict[str, list[int]] = {}
    tokens_in = tokens_out = 0
    cost_usd = 0.0
    cost_estimated = False
    durations_of_attempts: list[float] = []
    for row in attempts:
        if row.duration_s is not None:
            durations_of_attempts.append(row.duration_s)
        if row.tokens_in is None and row.tokens_out is None:
            continue
        # tokens are known when the attempt finishes; an unfinished one lands where it started
        moment = row.finished_at or row.started_at
        tokens.setdefault(row.route, [0] * count)[bucket_of(moment)] += (row.tokens_in or 0) + (
            row.tokens_out or 0
        )
        tokens_in += row.tokens_in or 0
        tokens_out += row.tokens_out or 0
        if row.cost_usd is not None:
            cost_usd += row.cost_usd
        cost_estimated = cost_estimated or row.cost_estimated

    # proxy requests carry their own tokens: the ledger rows are additive, never a copy
    proxy_tokens = proxy_requests = 0
    proxy_cost = 0.0
    for row in spend_rows:
        if row.source != "proxy":
            continue
        tokens.setdefault(row.route, [0] * count)[bucket_of(row.at)] += (row.tokens_in or 0) + (
            row.tokens_out or 0
        )
        proxy_tokens += (row.tokens_in or 0) + (row.tokens_out or 0)
        proxy_requests += 1
        if row.cost_usd is not None:
            proxy_cost += row.cost_usd

    statuses: dict[str, Counter[str]] = {}
    durations: dict[str, list[float]] = {}
    ratings: dict[str, Counter[str]] = {}
    corrections: Counter[str] = Counter()
    for row in job_rows:
        if row.route is None:
            continue
        if row.status in STATUSES_CHARTED:
            statuses.setdefault(row.route, Counter())[row.status] += 1
        if row.finished_at is not None:
            seconds = max(0.0, (row.finished_at - row.created_at).total_seconds())
            durations.setdefault(row.route, []).append(seconds)
        if row.rating_value in RATINGS_CHARTED:
            ratings.setdefault(row.route, Counter())[row.rating_value] += 1
            if row.rating_should_tier is not None:
                corrections[row.route] += 1

    by_total = sorted(tokens.items(), key=lambda item: -sum(item[1]))[:MAX_CHART_ROUTES]
    series = [Series(route, values, sum(values)) for route, values in by_total]
    tokens_chart = {
        "svg": _tokens_svg(series, starts, now, label, range_label) if series else None,
        "note": f"{_bucket_note(span)} · in + out tokens",
        "legend": [
            {
                "route": item.route,
                "css": SERIES_CLASSES[index % len(SERIES_CLASSES)],
                "text": f"{_tokens_text(item.total)} tokens",
            }
            for index, item in enumerate(series)
        ],
    }

    duration_rows = sorted(
        (
            (route, sum(values) / len(values), _percentile(values, 0.5), _percentile(values, 0.95))
            for route, values in durations.items()
        ),
        key=lambda item: -item[1],
    )[:MAX_CHART_ROUTES]
    duration_chart = {
        "svg": _bars_svg(
            [
                BarRow(
                    label=_short_route(route),
                    segments=[BarSegment("chart-bar--avg", avg)],
                    ticks=[
                        BarTick("chart-tick--p50", p50),
                        BarTick("chart-tick--p95", p95),
                    ],
                    note=f"p95 {format_age(p95)}",
                )
                for route, avg, p50, p95 in duration_rows
            ],
            "bar chart of job duration per route: average bar with p50 and p95 marks",
        )
        if duration_rows
        else None,
        "legend": [
            {
                "route": route,
                "text": (
                    f"avg {format_age(avg)} · p50 {format_age(p50)} · p95 {format_age(p95)}"
                    f" · {_plural(len(durations[route]), 'job')}"
                ),
            }
            for route, avg, p50, p95 in duration_rows
        ],
    }

    status_rows = sorted(statuses.items(), key=lambda item: -sum(item[1].values()))[
        :MAX_CHART_ROUTES
    ]
    status_chart = {
        "svg": _bars_svg(
            [
                BarRow(
                    label=_short_route(route),
                    segments=[
                        BarSegment("chart-bar--ok", counts.get("completed", 0)),
                        BarSegment("chart-bar--warn", counts.get("partial", 0)),
                        BarSegment("chart-bar--danger", counts.get("failed", 0)),
                    ],
                    note=str(sum(counts.values())),
                )
                for route, counts in status_rows
            ],
            "stacked bar chart of completed, partial and failed jobs per route",
        )
        if status_rows
        else None,
        "legend": [
            {
                "route": route,
                "text": (
                    f"{counts.get('completed', 0)} completed"
                    f" · {counts.get('partial', 0)} partial"
                    f" · {counts.get('failed', 0)} failed"
                ),
            }
            for route, counts in status_rows
        ],
    }

    rating_rows = sorted(ratings.items(), key=lambda item: -sum(item[1].values()))[
        :MAX_CHART_ROUTES
    ]
    rating_chart = {
        "svg": _bars_svg(
            [
                BarRow(
                    label=_short_route(route),
                    segments=[
                        BarSegment("chart-bar--ok", counts.get("good", 0)),
                        BarSegment("chart-bar--warn", counts.get("partial", 0)),
                        BarSegment("chart-bar--danger", counts.get("bad", 0)),
                    ],
                    note=_plural(corrections.get(route, 0), "correction")
                    if corrections.get(route, 0)
                    else "",
                )
                for route, counts in rating_rows
            ],
            "stacked bar chart of good, partial and bad ratings per route",
        )
        if rating_rows
        else None,
        "legend": [
            {
                "route": route,
                "text": (
                    f"{counts.get('good', 0)} good"
                    f" · {counts.get('partial', 0)} partial"
                    f" · {counts.get('bad', 0)} bad"
                    + (
                        f" · {_plural(corrections.get(route, 0), 'should-tier correction')}"
                        if corrections.get(route, 0)
                        else ""
                    )
                ),
            }
            for route, counts in rating_rows
        ],
    }

    status_totals = Counter(row.status for row in job_rows)
    active = sum(status_totals[status] for status in ("queued", "running", "awaiting_approval"))
    rated = sum(sum(counts.values()) for counts in ratings.values())
    corrections_total = sum(corrections.values())
    attempt_avg = (
        sum(durations_of_attempts) / len(durations_of_attempts) if durations_of_attempts else None
    )
    total_cost = cost_usd + proxy_cost
    cards = [
        {
            "label": "jobs",
            "value": f"{len(job_rows)}",
            "note": (
                f"{status_totals.get('completed', 0)} completed"
                f" · {status_totals.get('partial', 0)} partial"
                f" · {status_totals.get('failed', 0)} failed"
            ),
        },
        {"label": "active", "value": f"{active}", "note": "queued, running or awaiting approval"},
        {
            "label": "attempts",
            "value": f"{len(attempts)}",
            "note": f"avg {format_age(attempt_avg)} per attempt" if attempts else "no attempts yet",
        },
        {
            "label": "tokens",
            "value": f"{_tokens_text(tokens_in)} in / {_tokens_text(tokens_out)} out",
            "note": (
                f"{_tokens_text(proxy_tokens)} from {_plural(proxy_requests, 'proxy request')}"
                if proxy_requests
                else "job attempts"
            ),
        },
        {
            "label": "spend",
            "value": _cost_text(total_cost),
            "note": (
                "some costs estimated from prices"
                if cost_estimated
                else f"includes {_cost_text(proxy_cost)} of proxy requests"
                if proxy_cost
                else "as reported by the harnesses"
            ),
        },
        {
            "label": "ratings",
            "value": f"{rated}",
            "note": (
                _plural(corrections_total, "should-tier correction")
                if corrections_total
                else "none suggest a different tier"
                if rated
                else "no ratings yet"
            ),
        },
    ]
    return {
        "jobs_available": True,
        "range": key,
        "range_label": range_label,
        "ranges": [
            {
                "key": range_key,
                "label": range_label_text,
                "href": f"/stats?range={range_key}",
                "active": range_key == key,
            }
            for range_key, _, range_label_text in RANGES
        ],
        "cards": cards,
        "tokens_chart": tokens_chart,
        "duration_chart": duration_chart,
        "status_chart": status_chart,
        "rating_chart": rating_chart,
    }
