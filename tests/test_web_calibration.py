from __future__ import annotations

import re
from html import unescape

import pytest

from test_web_dashboard import CALIBRATION, authenticated_client, csrf_from


def post_calibration(client, tokens, **changes):
    authenticated_client(client, tokens)
    csrf = csrf_from(client.get("/calibrate"))
    data = CALIBRATION | {"csrf": csrf} | changes
    return client.post("/calibrate", data=data, follow_redirects=False)


def control(text: str, name: str) -> str:
    found = re.search(rf'<(?:input|select)\b[^>]*\bid="{name}"[^>]*>', text)
    assert found is not None
    return found.group(0)


def field_error(text: str, field: str) -> tuple[str, str]:
    found = re.search(
        rf'<p id="{field}-error" class="field-error"([^>]*)>(.*?)</p>', text, re.DOTALL
    )
    assert found is not None
    return found.group(1), unescape(found.group(2))


def assert_recorded_card(text: str) -> None:
    assert 'id="limit-kimi-code" class="panel limit-card limit-card--recorded"' in text
    assert text.count("limit-card--recorded") == 1
    assert '<span class="approval-badge approval-badge--ok">updated</span>' in text
    assert 'id="limit-claude-pro" class="panel limit-card"' in text
    assert 'id="limit-opencode-go" class="panel limit-card"' in text


def test_valid_calibration_redirects_to_and_highlights_only_saved_card(client, tokens):
    response = post_calibration(client, tokens)

    assert response.status_code == 303
    location = "/?recorded_pool=kimi-code&recorded_window=month#limit-kimi-code"
    assert response.headers["location"] == location
    assert_recorded_card(client.get(location).text)
    fragment = client.get("/fragments/limits?recorded_pool=kimi-code&recorded_window=month").text
    assert_recorded_card(fragment)


def test_unknown_recorded_pool_highlights_nothing(client, tokens):
    authenticated_client(client, tokens)

    for path in (
        "/?recorded_pool=nope&recorded_window=month",
        "/fragments/limits?recorded_pool=nope&recorded_window=month",
    ):
        text = client.get(path).text
        assert "limit-card--recorded" not in text
        assert ">updated</span>" not in text


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"target": ""}, "target", "choose a pool and window"),
        ({"percent": ""}, "percent", "enter a number from 0 to 100"),
        ({"percent": "abc"}, "percent", "enter a number from 0 to 100"),
        ({"percent": "150"}, "percent", "enter a number from 0 to 100"),
        (
            {"resets_in": "", "resets_time": "14:00"},
            "resets_date",
            "set the reset date",
        ),
        (
            {"resets_date": "2026-11-20"},
            "resets_in",
            "set either the reset date or reset in, not both",
        ),
        (
            {"resets_in": "3x"},
            "resets_in",
            "invalid span: '3x' (expected 3h20m, 45m, or 2d)",
        ),
        (
            {"resets_in": "", "resets_date": "2026-11-12"},
            "resets_date",
            "reset time is already in the past",
        ),
        (
            {"target": "1:0"},
            "resets_in",
            "window 5h is rolling: it has no reset time, calibrate without a reset",
        ),
        ({"target": "9:9"}, "target", "check the calibration field values"),
    ],
)
def test_validation_error_marks_field_and_keeps_values(
    client, tokens, store, changes, field, message
):
    response = post_calibration(client, tokens, **changes)
    data = CALIBRATION | changes

    assert response.status_code == 422
    attributes, error = field_error(response.text, field)
    assert "hidden" not in attributes
    assert error == message
    opener = control(response.text, field)
    assert f'aria-describedby="{field}-hint {field}-error"' in opener
    assert 'aria-invalid="true"' in opener
    assert unescape(message) in unescape(response.text)
    for name in ("target", "percent", "resets_date", "resets_in"):
        if name != field:
            attributes, error = field_error(response.text, name)
            assert "hidden" in attributes
            assert error == ""
    for name in ("percent", "resets_date", "resets_time", "resets_in"):
        assert f'value="{data[name]}"' in control(response.text, name)
    if data["target"] in {"0:0", "0:1", "1:0", "1:1", "2:0", "2:1", "2:2"}:
        option = re.search(rf'<option value="{data["target"]}"[^>]*>', response.text)
        assert option is not None
        assert "selected" in option.group(0)
    assert store.last_observation("kimi-code", "month") is None
    assert store.last_observation("kimi-code", "5h") is None


def test_calibration_hints_errors_and_values_are_linked(client, tokens):
    authenticated_client(client, tokens)

    text = client.get("/calibrate").text
    hints = {
        "target": "The pool window this observation is for.",
        "percent": "Share of the window already used, as the provider's usage page shows it.",
        "resets_date": (
            "When the window resets, in the dashboard time zone. "
            "The time is optional and defaults to midnight."
        ),
        "resets_in": (
            "Time left until the reset, e.g. 3h20m, 45m or 2d. Leave empty when you set the date."
        ),
    }
    for field, hint in hints.items():
        assert f'<p id="{field}-hint" class="hint">{hint}</p>' in text
        assert f'aria-describedby="{field}-hint"' in control(text, field)
        assert 'aria-invalid="true"' not in control(text, field)
        attributes, error = field_error(text, field)
        assert "hidden" in attributes
        assert error == ""
    preview = re.search(r"<output\b[^>]*>", text)
    assert preview is not None
    assert 'id="calibration-preview"' in preview.group(0)
    assert 'for="target percent resets_date resets_time resets_in"' in preview.group(0)
    assert 'aria-live="polite"' in preview.group(0)
    assert "hidden" in preview.group(0)


@pytest.mark.parametrize(
    ("target", "months", "days", "hours"),
    [("0:0", 0, 0, 5), ("0:1", 0, 7, 0), ("1:1", 1, 0, 0)],
)
def test_window_options_carry_duration(client, tokens, target, months, days, hours):
    authenticated_client(client, tokens)

    text = client.get("/calibrate").text
    option = re.search(rf'<option value="{target}"[^>]*>', text)
    assert option is not None
    for name, value in (("months", months), ("days", days), ("hours", hours)):
        assert f'data-{name}="{value}"' in option.group(0)


def test_calibration_script_has_preview_validation_and_persistent_highlight(client):
    script = client.get("/static/app.js").text

    assert "calibration-preview" in script
    assert r"/(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?/" in script
    assert "start.setMonth" in script
    assert "start.setDate" in script
    assert "start.setHours" in script
    assert "expected ${expected.toFixed(1)}%" in script
    assert "aria-invalid" in script
    assert "aria-describedby" in script
    assert "event.preventDefault()" in script
    assert "target.form.noValidate = true" in script
    assert "window ahead" in script  # mirrors the server check for a fixed window
    assert "touched.has(name)" in script  # untouched fields keep quiet until submit
    assert "refresh('/fragments/limits' + location.search, 'limits')" in script


def test_calibration_styles_use_existing_tokens(client):
    css = client.get("/static/app.css").text

    field = re.search(r"^\.field-error\s*\{([^}]*)\}", css, re.MULTILINE)
    assert field is not None
    assert "color: var(--color-danger)" in field.group(1)
    invalid = re.search(r'^\[aria-invalid="true"\]\s*\{([^}]*)\}', css, re.MULTILINE)
    assert invalid is not None
    assert "border-color: var(--color-danger)" in invalid.group(1)
    recorded = re.search(r"^\.limit-card--recorded\s*\{([^}]*)\}", css, re.MULTILINE)
    assert recorded is not None
    assert "outline: 2px solid var(--color-accent)" in recorded.group(1)
    assert ".calibration-preview" in css
