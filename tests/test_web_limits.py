"""Limits overview cards: one panel per pool with usage bars, pace markers, resets and blocks."""

from __future__ import annotations

import re

import pytest

from test_web_dashboard import csrf_from, pool_json, window_json
from test_web_layout import rig, web

__all__ = ["rig", "web"]

PATHS = ["/", "/fragments/limits"]
BLOCK = {"until": "2026-11-15T06:00:00+00:00", "reason": "limit reached"}
USAGE = {
    "source": "opencode-go",
    "mode": "auto",
    "last_attempt_at": "2026-11-13T09:12:00+00:00",
    "last_success_at": "2026-11-13T09:00:00+00:00",
    "last_error": "unauthorized",
    "age_s": 720.0,
    "stale": True,
}


def element_block(text: str, opener: str) -> str:
    """Balanced html of the element opened by the first tag matching the pattern."""
    found = re.search(opener, text)
    assert found is not None, f"no element matching {opener!r}"
    tag = re.match(r"<(\w+)", found.group(0))
    assert tag is not None
    depth = 0
    for close in re.finditer(rf"<(/?){tag.group(1)}\b[^>]*>", text[found.start() :], re.S):
        depth += -1 if close.group(1) else 1
        if depth == 0:
            return text[found.start() : found.start() + close.end()]
    raise AssertionError(f"unbalanced <{tag.group(1)}> elements")


def class_block(text: str, klass: str) -> str:
    """Balanced html of the first element carrying the given class."""
    return element_block(
        text,
        r'<\w+\b(?=[^>]*class="[^"]*\b' + re.escape(klass) + r'\b[^"]*")[^>]*>',
    )


def show_limits(web, rig, monkeypatch, pools, path="/"):
    """Renders a page with the gateway snapshot replaced by the given pools."""
    monkeypatch.setattr(rig.app.state.gateway, "snapshot_view", lambda: {"pools": pools})
    response = web.get(path)
    assert response.status_code == 200
    return response


@pytest.mark.parametrize("path", PATHS)
def test_known_window_renders_a_limit_card(web, rig, monkeypatch, path):
    window = window_json(resets_at="2026-11-13T12:12:00+00:00")
    text = show_limits(web, rig, monkeypatch, [pool_json(window)], path).text

    limits = element_block(text, r'<div\b[^>]*\bid="limits"[^>]*>')
    grid = element_block(limits, r'<div\b[^>]*class="grid limit-grid"[^>]*>')
    card = element_block(grid, r'<article\b[^>]*class="panel limit-card(?: [^"]*)?"[^>]*>')

    labelledby = re.search(r'aria-labelledby="([^"]+)"', card)
    assert labelledby is not None
    assert f'id="{labelledby.group(1)}"' in card
    heading = rf'<h2\b[^>]*\bid="{labelledby.group(1)}"[^>]*>\s*Claude Pro\s*</h2>'
    assert re.search(heading, card)

    window_html = class_block(card, "limit-window")
    assert re.search(r"<h3\b[^>]*>\s*week\s*</h3>", window_html)

    status = class_block(window_html, "limit-status")
    assert "behind pace" in status
    assert "approval-badge--ok" in status

    bar = class_block(window_html, "usage-bar")
    opener = re.search(r'<\w+\b[^>]*class="[^"]*\busage-bar\b[^"]*"[^>]*>', window_html)
    assert opener is not None
    assert 'role="img"' in opener.group(0)
    label = re.search(r'aria-label="([^"]*)"', opener.group(0))
    assert label is not None
    assert "used 40.0%" in label.group(1)
    assert "expected 50.0%" in label.group(1)
    progress = r'<progress\b(?=[^>]*max="100")(?=[^>]*value="40.0")[^>]*>'
    assert re.search(progress, bar)
    assert "expected 50.0%" in class_block(window_html, "limit-usage")
    pace = (
        r'<\w+\b(?=[^>]*class="[^"]*\busage-pace\b)'
        r'(?=[^>]*data-expected="50.0")(?=[^>]*\bhidden\b)[^>]*>'
    )
    assert re.search(pace, bar)

    reset = class_block(card, "limit-reset")
    assert "data-countdown" in reset
    assert 'datetime="2026-11-13T12:12:00' in reset
    assert "13.11 12:12" in reset
    assert "data-countdown-relative" in reset
    assert "in 3 h 12 min" in reset

    table = element_block(limits, r"<details\b[^>]*>")
    assert 'class="limit-table"' in table
    assert re.search(r"<summary>\s*All windows as a table\s*</summary>", table)
    assert "Claude Pro" in table
    assert "week" in table


def test_window_ahead_of_expected_pace(web, rig, monkeypatch):
    window = window_json(used=0.6, expected=0.5, lag=-0.1)
    text = show_limits(web, rig, monkeypatch, [pool_json(window)], "/fragments/limits").text

    status = class_block(text, "limit-status")
    assert "ahead of pace" in status
    assert "approval-badge--warn" in status


@pytest.mark.parametrize("path", PATHS)
def test_pool_card_shows_the_usage_source_line(web, rig, monkeypatch, path):
    window = window_json(figure_source="auto")
    pool = pool_json(window) | {"usage": USAGE}
    text = show_limits(web, rig, monkeypatch, [pool], path).text

    card = class_block(text, "limit-card")
    source = class_block(card, "limit-source")
    assert "Source: opencode-go (auto)" in source
    assert "read 12 min ago" in source
    assert "last read failed: unauthorized" in source
    assert ">stale<" in class_block(card, "limit-stale")


def test_pool_card_shows_never_read_usage_without_a_badge(web, rig, monkeypatch):
    usage = USAGE | {"age_s": None, "last_error": None, "stale": False}
    text = show_limits(web, rig, monkeypatch, [pool_json(window_json()) | {"usage": usage}]).text

    card = class_block(text, "limit-card")
    source = class_block(card, "limit-source")
    assert "Source: opencode-go (auto)" in source
    assert "never read" in source
    assert "last read failed" not in source
    assert "limit-stale" not in card


def test_pool_card_without_a_usage_source_has_no_source_line(web, rig, monkeypatch):
    text = show_limits(web, rig, monkeypatch, [pool_json(window_json())], "/fragments/limits").text

    assert "limit-source" not in text
    assert "never read" not in text


@pytest.mark.parametrize("figure", ["auto", "manual", "journal"])
def test_window_age_line_names_the_figure_source(web, rig, monkeypatch, figure):
    window = window_json(figure_source=figure)
    text = show_limits(web, rig, monkeypatch, [pool_json(window)], "/fragments/limits").text

    age = class_block(text, "limit-age")
    assert f"figure: {figure}" in age


def test_exhausted_window_is_blocked(web, rig, monkeypatch):
    window = window_json(blocked=True)
    text = show_limits(web, rig, monkeypatch, [pool_json(window)], "/fragments/limits").text

    status = class_block(text, "limit-status")
    assert "blocked" in status
    assert "approval-badge--danger" in status


def test_unknown_window_shows_no_data(web, rig, monkeypatch):
    window = window_json(
        known=False,
        used=None,
        expected=None,
        lag=None,
        age_s=None,
        observed_at=None,
        note="no observation recorded",
    )
    text = show_limits(web, rig, monkeypatch, [pool_json(window)], "/fragments/limits").text

    window_html = class_block(text, "limit-window")
    status = class_block(window_html, "limit-status")
    assert "no data" in status
    assert "no observation recorded" in window_html
    assert "usage-bar" not in window_html
    assert "limit-usage" not in window_html
    assert "<progress" not in window_html


@pytest.mark.parametrize(
    ("age_s", "is_stale"),
    [(21601.0, True), (21600.0, False)],
)
def test_stale_window_gets_a_badge(web, rig, monkeypatch, age_s, is_stale):
    text = show_limits(
        web, rig, monkeypatch, [pool_json(window_json(age_s=age_s))], "/fragments/limits"
    ).text

    window_html = class_block(text, "limit-window")
    age = class_block(window_html, "limit-age")
    assert "6 h" in age
    assert ("limit-stale" in window_html) is is_stale
    if is_stale:
        assert ">stale<" in class_block(window_html, "limit-stale")


@pytest.mark.parametrize("path", PATHS)
def test_blocked_pool_card_has_unblock_form(web, rig, monkeypatch, path):
    response = show_limits(web, rig, monkeypatch, [pool_json(window_json(), block=BLOCK)], path)
    card = class_block(response.text, "limit-card")

    assert "Closed until 15.11 06:00: limit reached" in card
    assert "Closed until" in class_block(card, "limit-block")
    form = class_block(card, "limit-unblock")
    opener = re.search(r"<form\b[^>]*>", form)
    assert opener is not None
    assert 'method="post"' in opener.group(0)
    assert 'action="/pools/claude-pro/unblock"' in opener.group(0)
    csrf = csrf_from(response)
    assert f'name="csrf" value="{csrf}"' in form
    assert re.search(r"<button\b[^>]*>\s*Unblock\s*</button>", form)


def test_app_js_enhances_limit_cards(client):
    script = client.get("/static/app.js").text

    assert "data-expected" in script
    assert "setProperty('--expected'" in script
    assert "data-countdown" in script
    assert "data-countdown-relative" in script
    assert "Date.parse" in script
    limits_poll = "refresh('/fragments/limits' + location.search, 'limits'), 30000)"
    assert f"setInterval(() => {limits_poll}" in script
    # reset countdowns tick on a timeout chain, not on an interval of their own
    assert "setTimeout(tick, 30000)" in script
    assert not re.search(r"setInterval\([^;]*updateLimitCountdowns", script, re.DOTALL)


def test_limit_status_keeps_badge_colours(client):
    css = client.get("/static/app.css").text

    rule = re.search(r"^\.limit-status\s*\{([^}]*)\}", css, re.M)
    assert rule is not None
    assert "color:" not in rule.group(1)


def test_unblock_buttons_name_their_pool(web, rig, monkeypatch):
    text = show_limits(web, rig, monkeypatch, [pool_json(window_json(), block=BLOCK)]).text

    assert re.search(r'<button\b[^>]*aria-label="Unblock [^"]+"[^>]*>\s*Unblock\s*</button>', text)


def test_refresh_keeps_disclosures_and_focus(client):
    script = client.get("/static/app.js").text
    refresh = script[script.index("async function refresh") : script.index("setInterval(")]

    # a swap must not pull focus or a disclosure state from under the visitor
    assert "current.contains(document.activeElement)" in refresh
    assert ".open" in refresh
    assert "setupLimitCards(current)" in refresh
