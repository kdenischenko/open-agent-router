from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_router.api import create_app
from agent_router.events import EventBus
from agent_router.jobstore import SqliteJobStore
from agent_router.service import build_dispatcher
from agent_router.store import SqliteStore
from helpers import BASE_URL
from test_jobstore_route_samples import make_attempt, make_job, rate

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "agent_router"
CALIBRATION = {
    "target": "1:1",
    "percent": "37.5",
    "resets_date": "",
    "resets_time": "",
    "resets_in": "2d",
}


def login(client: TestClient, token: str):
    return client.post("/login", data={"token": token}, follow_redirects=False)


def csrf_from(response) -> str:
    found = re.search(r'<input[^>]+name="csrf"[^>]+value="([^"]+)"', response.text)
    assert found is not None
    return found.group(1)


def authenticated_client(client: TestClient, tokens) -> TestClient:
    response = login(client, tokens.admin)
    assert response.status_code == 303
    return client


@pytest.mark.parametrize("path", ["/", "/fragments/limits", "/fragments/approvals", "/calibrate"])
def test_protected_get_without_session_redirects_to_login(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_page_is_public(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert 'name="token"' in response.text


def test_pages_link_a_public_favicon(client):
    page = client.get("/login")
    assert '<link rel="icon" href="/static/favicon.svg' in page.text
    icon = client.get("/static/favicon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")


def test_invalid_token_is_rejected_without_cookie(client):
    response = login(client, "wrong token")
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert client.cookies.get("ar_session") is None


def test_client_token_is_rejected_without_cookie(client, tokens):
    response = login(client, tokens.client)
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert client.cookies.get("ar_session") is None


def test_admin_token_sets_secure_session_cookie(client, tokens):
    response = login(client, tokens.admin)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("ar_session=")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie


def test_login_cookie_lasts_panel_session_days(client, tokens):
    response = login(client, tokens.admin)
    assert response.status_code == 303
    assert "max-age=2592000" in response.headers["set-cookie"].lower()


def test_panel_survives_restart_without_dispatcher(tmp_path, config, clock, tokens, home):
    database = tmp_path / "agent-router.sqlite"
    with TestClient(
        create_app(config, SqliteStore(database), clock, tokens, home=home, background=False),
        base_url=BASE_URL,
    ) as client:
        assert login(client, tokens.admin).status_code == 303
        cookie = client.cookies.get("ar_session")
        assert cookie
    with TestClient(
        create_app(config, SqliteStore(database), clock, tokens, home=home, background=False),
        base_url=BASE_URL,
    ) as client:
        client.cookies.set("ar_session", cookie, path="/")
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 200


def test_panel_survives_restart_with_dispatcher(tmp_path, config, clock, tokens, home):
    database = tmp_path / "agent-router.sqlite"

    def build() -> object:
        store = SqliteStore(database)
        jobs = SqliteJobStore(store)
        bus = EventBus(clock)
        dispatcher = build_dispatcher(config, store, jobs, bus, clock, home, UTC)
        return create_app(
            config,
            store,
            clock,
            tokens,
            home=home,
            bus=bus,
            dispatcher=dispatcher,
            background=False,
        )

    with TestClient(build(), base_url=BASE_URL) as client:
        assert login(client, tokens.admin).status_code == 303
        cookie = client.cookies.get("ar_session")
        assert cookie
    with TestClient(build(), base_url=BASE_URL) as client:
        client.cookies.set("ar_session", cookie, path="/")
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 200


def test_main_page_shows_pool_titles(client, tokens):
    authenticated_client(client, tokens)
    response = client.get("/")
    assert response.status_code == 200
    for title in ("Claude Pro", "Kimi Code", "OpenCode Go"):
        assert title in response.text


@pytest.mark.parametrize("tier", ["cheap", "standard", "strong"])
def test_main_page_shows_advice_for_each_tier(client, tokens, tier):
    authenticated_client(client, tokens)
    response = client.get("/")
    assert response.status_code == 200
    assert "Where to send now" in response.text
    assert f'data-tier="{tier}"' in response.text


def test_main_page_shows_excluded_routes_with_reasons(client, tokens):
    authenticated_client(client, tokens)
    response = client.get("/")
    assert response.status_code == 200
    assert "claude-cli/sonnet" in response.text
    assert "route disabled" in response.text
    assert "opencode-cli/glm-5.3-flash" in response.text
    assert "does not serve tier standard" in response.text


def test_advice_fragment_requires_session(client):
    response = client.get("/fragments/advice", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def standard_advice_sections(text: str) -> tuple[str, str]:
    article = re.search(
        r'<article class="panel" data-tier="standard">(.*?)</article>', text, re.DOTALL
    )
    assert article is not None
    return article.group(1).split("<h4>Excluded routes</h4>", maxsplit=1)


def test_advice_fragment_follows_calibration(client, tokens):
    authenticated_client(client, tokens)
    before = client.get("/fragments/advice")
    assert before.status_code == 200
    recommended, excluded = standard_advice_sections(before.text)
    assert "kimi-cli/k3.1-pro" in recommended
    assert "kimi-cli/k3.1-pro" not in excluded

    csrf = csrf_from(client.get("/calibrate"))
    response = client.post(
        "/calibrate",
        data=CALIBRATION | {"pool": "kimi-code", "window": "month", "percent": "100", "csrf": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303

    after = client.get("/fragments/advice")
    assert after.status_code == 200
    recommended, excluded = standard_advice_sections(after.text)
    assert "kimi-cli/k3.1-pro" not in recommended
    assert "kimi-cli/k3.1-pro" in excluded


def test_index_includes_advice_block(client, tokens):
    authenticated_client(client, tokens)
    response = client.get("/")
    assert response.status_code == 200
    assert response.text.count('id="advice"') == 1


def test_routes_section_without_history_shows_empty_line(client, tokens):
    authenticated_client(client, tokens)
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="routes"' in response.text
    assert "no finished jobs" in response.text


def test_routes_section_shows_seeded_route(config, store, clock, tokens, home):
    jobs = SqliteJobStore(store)
    now = clock.now()
    route_id = "kimi-cli/k3.1-pro"
    jobs.create(make_job("j_routes", route=route_id, finished_at=now))
    jobs.add_attempt(make_attempt("j_routes", 1, route=route_id, finished_at=now, cost_usd=0.2))
    rate(jobs, "j_routes", "good", at=now)
    bus = EventBus(clock)
    dispatcher = build_dispatcher(config, store, jobs, bus, clock, home, UTC)
    app = create_app(
        config,
        store,
        clock,
        tokens,
        home=home,
        bus=bus,
        dispatcher=dispatcher,
        background=False,
    )
    with TestClient(app, base_url=BASE_URL) as client:
        authenticated_client(client, tokens)
        response = client.get("/")
    assert response.status_code == 200
    assert route_id in response.text
    assert "good 1/1" in response.text
    assert "fewer than 5 jobs" in response.text
    assert "$0.20" in response.text


def test_calibration_form_offers_only_real_pairs(client, tokens):
    authenticated_client(client, tokens)
    response = client.get("/calibrate")
    assert response.status_code == 200
    target = response.text[response.text.index('<select id="target"') :]
    target = target[: target.index("</select>")]
    group = r'<optgroup label="([^"]+)" data-pool="(\d+)">(.*?)</optgroup>'
    groups = re.findall(group, target, re.S)
    options = {
        (label, pool): re.findall(
            r'<option value="([^"]+)" data-kind="(fixed|rolling)"'
            r' data-months="\d+" data-days="\d+" data-hours="\d+">([^<]+)</option>',
            body,
        )
        for label, pool, body in groups
    }
    assert options == {
        ("Claude Pro", "0"): [("0:0", "fixed", "5h"), ("0:1", "fixed", "week")],
        ("Kimi Code", "1"): [("1:0", "rolling", "5h"), ("1:1", "fixed", "month")],
        ("OpenCode Go", "2"): [
            ("2:0", "rolling", "5h"),
            ("2:1", "rolling", "week"),
            ("2:2", "fixed", "month"),
        ],
    }
    provider = response.text[response.text.index('<select id="provider"') :]
    provider = provider[: provider.index("</select>")]
    assert re.findall(r'<option value="([^"]+)">([^<]+)</option>', provider) == [
        ("0", "Claude Pro"),
        ("1", "Kimi Code"),
        ("2", "OpenCode Go"),
    ]
    assert 'name="target"' in response.text
    assert 'name="pool"' not in response.text
    assert 'name="window"' not in response.text
    assert 'name="resets_date" type="date"' in response.text
    assert 'name="resets_time" type="time"' in response.text


def test_calibration_by_target_records_observation(client, tokens, store):
    authenticated_client(client, tokens)
    csrf = csrf_from(client.get("/calibrate"))
    response = client.post("/calibrate", data=CALIBRATION | {"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    observation = store.last_observation("kimi-code", "month")
    assert observation is not None
    assert observation.fraction == 0.375
    assert observation.source == "manual"


@pytest.mark.parametrize("target", ["x:y", "9:0", "0:9", "-1:0"])
def test_calibration_rejects_bad_target(client, tokens, target):
    authenticated_client(client, tokens)
    csrf = csrf_from(client.get("/calibrate"))
    data = CALIBRATION | {"pool": "claude-pro", "window": "week", "csrf": csrf}
    if target is None:
        data.pop("target")
    else:
        data["target"] = target
    response = client.post("/calibrate", data=data, follow_redirects=False)
    assert response.status_code == 422
    assert "check the calibration field values" in response.text


def test_calibration_without_csrf_is_forbidden(client, tokens, store):
    authenticated_client(client, tokens)
    response = client.post("/calibrate", data=CALIBRATION, follow_redirects=False)
    assert response.status_code == 403
    assert store.last_observation("kimi-code", "month") is None


def test_calibration_with_csrf_records_observation(client, tokens, store):
    authenticated_client(client, tokens)
    csrf = csrf_from(client.get("/calibrate"))
    data = CALIBRATION | {"target": "0:1", "csrf": csrf}
    response = client.post("/calibrate", data=data, follow_redirects=False)
    assert response.status_code == 303
    observation = store.last_observation("claude-pro", "week")
    assert observation is not None
    assert observation.fraction == 0.375
    assert observation.source == "manual"


def test_logout_without_csrf_is_forbidden(client, tokens):
    authenticated_client(client, tokens)
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 403
    assert client.get("/").status_code == 200


def test_logout_with_csrf_drops_session(client, tokens):
    authenticated_client(client, tokens)
    csrf = csrf_from(client.get("/"))
    response = client.post("/logout", data={"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


LOCAL_TZ = timezone(timedelta(hours=3))


def window_json(**changes):
    values = {
        "id": "week",
        "kind": "fixed",
        "role": "pace",
        "known": True,
        "used": 0.4,
        "expected": 0.5,
        "lag": 0.1,
        "resets_at": None,
        "source": "manual",
        "figure_source": "manual",
        "observed_at": "2026-11-13T09:00:00+00:00",
        "age_s": 60.0,
        "estimated": False,
        "blocked": False,
        "note": None,
        "anchored": False,
    }
    values.update(changes)
    return values


def pool_json(window, block=None):
    return {
        "id": "claude-pro",
        "title": "Claude Pro",
        "unit": "percent",
        "known": True,
        "lag": 0.1,
        "blocked_by": None,
        "blocked_until": None,
        "reserve_hit": False,
        "block": block,
        "via_gateway": None,
        "counter_sampled_at": None,
        "windows": [window],
    }


def show_window(client, tokens, app, monkeypatch, window):
    authenticated_client(client, tokens)
    monkeypatch.setattr(app.state.gateway, "snapshot_view", lambda: {"pools": [pool_json(window)]})
    return client.get("/fragments/limits")


def test_limits_show_local_reset_time(config, store, clock, tokens, home, monkeypatch):
    app = create_app(config, store, clock, tokens, home=home, tz=LOCAL_TZ, background=False)
    with TestClient(app, base_url=BASE_URL) as client:
        response = show_window(
            client,
            tokens,
            app,
            monkeypatch,
            window_json(resets_at="2026-11-15T06:00:00+00:00"),
        )
    assert response.status_code == 200
    assert "15.11 09:00" in response.text
    assert "+00:00" not in response.text


def test_closed_pool_shows_local_date_and_time(config, store, clock, tokens, home, monkeypatch):
    app = create_app(config, store, clock, tokens, home=home, tz=LOCAL_TZ, background=False)
    block = {"until": "2026-11-15T06:00:00+00:00", "reason": "limit reached"}
    with TestClient(app, base_url=BASE_URL) as client:
        authenticated_client(client, tokens)
        monkeypatch.setattr(
            app.state.gateway,
            "snapshot_view",
            lambda: {"pools": [pool_json(window_json(), block=block)]},
        )
        response = client.get("/")
    assert response.status_code == 200
    assert "Closed until 15.11 09:00: limit reached" in response.text
    assert "+00:00" not in response.text


def test_advice_fragment_has_csrf_and_local_block_time(
    config, store, clock, tokens, home, monkeypatch
):
    app = create_app(config, store, clock, tokens, home=home, tz=LOCAL_TZ, background=False)
    block = {"until": "2026-11-15T06:00:00+00:00", "reason": "limit reached"}
    with TestClient(app, base_url=BASE_URL) as client:
        authenticated_client(client, tokens)
        monkeypatch.setattr(
            app.state.gateway,
            "snapshot_view",
            lambda: {"pools": [pool_json(window_json(), block=block)]},
        )
        csrf = csrf_from(client.get("/"))
        response = client.get("/fragments/advice")
    assert response.status_code == 200
    assert "Closed until 15.11 09:00: limit reached" in response.text
    assert "+00:00" not in response.text
    assert 'action="/pools/claude-pro/unblock"' in response.text
    assert f'name="csrf" value="{csrf}"' in response.text


def test_rolling_window_has_no_reset_hint(client, tokens, app, monkeypatch):
    response = show_window(
        client, tokens, app, monkeypatch, window_json(id="5h", kind="rolling", role="guard")
    )
    assert response.status_code == 200
    assert "set the reset time" not in response.text
    assert "—" in response.text  # non-ascii on purpose


def test_anchored_fixed_window_has_no_reset_hint(client, tokens, app, monkeypatch):
    response = show_window(client, tokens, app, monkeypatch, window_json(anchored=True))
    assert response.status_code == 200
    assert "set the reset time" not in response.text
    assert "—" in response.text  # non-ascii on purpose


@pytest.mark.parametrize(
    ("age_s", "shown"),
    [(42.0, "42 s"), (150.0, "2 min"), (7500.0, "2 h 5 min"), (200000.0, "2 d 7 h")],
)
def test_limits_show_the_data_age_in_units(client, tokens, app, monkeypatch, age_s, shown):
    response = show_window(client, tokens, app, monkeypatch, window_json(age_s=age_s))
    assert response.status_code == 200
    assert f"<td>{shown}</td>" in response.text


def test_fixed_window_without_reset_asks_for_it(client, tokens, app, monkeypatch):
    response = show_window(client, tokens, app, monkeypatch, window_json())
    assert response.status_code == 200
    assert "set the reset time" in response.text


def test_limits_fragment_shows_calibration_values(client, tokens, store):
    authenticated_client(client, tokens)
    csrf = csrf_from(client.get("/calibrate"))
    client.post("/calibrate", data=CALIBRATION | {"csrf": csrf})
    response = client.get("/fragments/limits")
    assert response.status_code == 200
    assert 'id="limits"' in response.text
    assert "37.5" in response.text
    assert "manual" in response.text
    assert "Reset" in response.text
    assert "Age" in response.text


def test_pool_title_is_escaped(config, store, clock, tokens, home):
    pools = (replace(config.pools[0], title="<script>alert(1)</script>"), *config.pools[1:])
    app = create_app(
        replace(config, pools=pools), store, clock, tokens, home=home, background=False
    )
    with TestClient(app, base_url=BASE_URL) as client:
        authenticated_client(client, tokens)
        response = client.get("/")
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


SVG_NAMESPACE = 'xmlns="http://www.w3.org/2000/svg"'


@pytest.mark.parametrize("directory", ["templates", "static"])
def test_assets_have_no_external_urls(directory):
    root = PACKAGE / directory
    expected = (
        {
            "base.html",
            "login.html",
            "index.html",
            "_limits.html",
            "_advice.html",
            "_approvals.html",
            "_routes.html",
            "calibrate.html",
            "jobs.html",
            "job.html",
            "_job.html",
            "_jobs.html",
            "message.html",
            "confirm_action.html",
            "settings.html",
            "submit.html",
        }
        if directory == "templates"
        else {"app.css", "app.js", "favicon.svg"}
    )
    assert {path.name for path in root.iterdir() if path.is_file()} == expected
    for path in root.iterdir():
        if path.is_file():
            # the SVG namespace is an identifier, not a resource the browser fetches
            text = path.read_text(encoding="utf-8").replace(SVG_NAMESPACE, "")
            assert "http://" not in text
            assert "https://" not in text


def test_limits_script_refreshes_only_successful_fragment(client):
    response = client.get("/static/app.js")
    assert response.status_code == 200
    script = response.text
    assert "async function refresh(path, id)" in script
    assert script.count("async function refresh(") == 1
    assert "fetch(path, {credentials: 'same-origin'})" in script
    for path, element_id, period in (
        ("'/fragments/limits' + location.search", "limits", 30000),
        ("'/fragments/advice'", "advice", 30000),
        ("'/fragments/jobs' + location.search", "jobs", 5000),
        ("'/fragments/approvals'", "approvals", 5000),
    ):
        assert f"setInterval(() => refresh({path}, '{element_id}'), {period})" in script
    # the four fragment pollers above plus one interval that ticks the approval countdowns
    assert script.count("setInterval(") == 5
    assert script.count("fetch(") == 1
    assert "response.status !== 200" in script
    assert script.index("response.status !== 200") < script.index("innerHTML")


def test_calibration_reset_date_and_time_use_the_dashboard_time_zone(
    config, store, clock, tokens, home
):
    app = create_app(config, store, clock, tokens, home=home, tz=LOCAL_TZ, background=False)
    with TestClient(app, base_url=BASE_URL) as client:
        authenticated_client(client, tokens)
        csrf = csrf_from(client.get("/calibrate"))
        data = CALIBRATION | {
            "csrf": csrf,
            "resets_in": "",
            "resets_date": "2026-11-20",
            "resets_time": "14:30",
        }
        response = client.post("/calibrate", data=data, follow_redirects=False)
        assert response.status_code == 303
        observation = store.last_observation("kimi-code", "month")
        assert observation is not None
        assert observation.resets_at == datetime(2026, 11, 20, 14, 30, tzinfo=LOCAL_TZ)


def test_calibration_reset_date_alone_means_midnight(config, store, clock, tokens, home):
    app = create_app(config, store, clock, tokens, home=home, tz=LOCAL_TZ, background=False)
    with TestClient(app, base_url=BASE_URL) as client:
        authenticated_client(client, tokens)
        csrf = csrf_from(client.get("/calibrate"))
        data = CALIBRATION | {"csrf": csrf, "resets_in": "", "resets_date": "2026-11-21"}
        response = client.post("/calibrate", data=data, follow_redirects=False)
        assert response.status_code == 303
        observation = store.last_observation("kimi-code", "month")
        assert observation is not None
        assert observation.resets_at == datetime(2026, 11, 21, tzinfo=LOCAL_TZ)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"resets_in": "", "resets_time": "14:00"}, "set the reset date"),
        ({"resets_date": "2026-11-21"}, "set either the reset date or reset in, not both"),
    ],
)
def test_calibration_rejects_an_incomplete_reset(client, tokens, store, fields, message):
    authenticated_client(client, tokens)
    csrf = csrf_from(client.get("/calibrate"))
    response = client.post(
        "/calibrate", data=CALIBRATION | {"csrf": csrf} | fields, follow_redirects=False
    )
    assert response.status_code == 422
    assert message in response.text
    assert store.last_observation("kimi-code", "month") is None


# --- calibration: no default target, confirmation ---------------------------------


def test_calibration_form_has_no_default_target(client, tokens):
    authenticated_client(client, tokens)
    text = client.get("/calibrate").text
    for select in ("target", "provider"):
        body = text[text.index(f'<select id="{select}"') :]
        body = body[: body.index("</select>")]
        first = re.search(r"<option[^>]*>", body)
        assert first is not None
        assert 'value=""' in first.group(0)
        assert "selected" in first.group(0) and "disabled" in first.group(0)


@pytest.mark.parametrize("target", [None, ""])
def test_calibration_without_target_shows_error(client, tokens, store, target):
    authenticated_client(client, tokens)
    csrf = csrf_from(client.get("/calibrate"))
    data = CALIBRATION | {"csrf": csrf}
    if target is None:
        data.pop("target")
    else:
        data["target"] = target
    response = client.post("/calibrate", data=data, follow_redirects=False)
    assert response.status_code == 422
    assert "choose a pool and window" in response.text
    assert store.last_observation("kimi-code", "month") is None


def test_calibration_redirects_with_confirmation(client, tokens):
    authenticated_client(client, tokens)
    csrf = csrf_from(client.get("/calibrate"))
    response = client.post("/calibrate", data=CALIBRATION | {"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location == "/?recorded_pool=kimi-code&recorded_window=month#limit-kimi-code"
    page = client.get(location).text
    assert re.search(r"Recorded Kimi Code / month: used 38%, reset \d\d\.\d\d \d\d:\d\d", page)
    assert "Recorded" not in client.get("/").text
    unknown = client.get("/?recorded_pool=nope&recorded_window=month").text
    assert "Recorded" not in unknown


def test_calibration_confirmation_without_reset_has_no_tail(client, tokens):
    authenticated_client(client, tokens)
    csrf = csrf_from(client.get("/calibrate"))
    data = CALIBRATION | {"csrf": csrf, "target": "1:0", "resets_in": ""}
    response = client.post("/calibrate", data=data, follow_redirects=False)
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "Recorded Kimi Code / 5h: used 38%" in page
    assert "Recorded Kimi Code / 5h: used 38%, reset" not in page


def test_calibration_form_marks_rolling_windows(client, tokens):
    authenticated_client(client, tokens)
    text = client.get("/calibrate").text
    rolling = (
        '<option value="1:0" data-kind="rolling" '
        'data-months="0" data-days="0" data-hours="5">5h</option>'
    )
    fixed = (
        '<option value="1:1" data-kind="fixed" '
        'data-months="1" data-days="0" data-hours="0">month</option>'
    )
    assert rolling in text
    assert fixed in text


def test_calibration_script_handles_rolling_windows(client):
    script = client.get("/static/app.js").text
    assert "rolling window: no reset time" in script
    assert "dataset.kind" in script
