from __future__ import annotations

import html
import re
import sys
from dataclasses import replace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from helpers import BASE_URL
from test_web_dashboard import authenticated_client, csrf_from
from test_web_jobs import WebRig


@pytest.fixture
def rig(tmp_path, home, config, store, jobstore, clock, tokens, monkeypatch):
    monkeypatch.setattr("agent_router.jobs.repo_root", lambda cwd: None)
    root = tmp_path / "allowed work"
    root.mkdir()
    config = replace(
        config,
        tasks=replace(config.tasks, allowed_roots=(root,)),
        backends={
            name: replace(backend, executable=sys.executable)
            for name, backend in config.backends.items()
        },
    )
    return WebRig(home, config, store, jobstore, clock, tokens)


@pytest.fixture
def web(rig):
    with TestClient(rig.app, base_url=BASE_URL) as client:
        authenticated_client(client, rig.tokens)
        yield client


def form_data(rig, **changes):
    data = {
        "prompt": "Review the current branch",
        "cwd": str(rig.dispatcher.config.tasks.allowed_roots[0]),
        "mode": "read",
        "tier": "",
        "timeout_s": "",
    }
    data.update(changes)
    return data


def submit(web, rig, **changes):
    csrf = csrf_from(web.get("/jobs/new"))
    data = form_data(rig, **changes) | {"csrf": csrf}
    # a list value means a repeated form key, e.g. several exclude_pools boxes;
    # this httpx build only takes a mapping for data=, so the body is encoded here
    items = [
        (name, item)
        for name, value in data.items()
        for item in (value if isinstance(value, list) else [value])
    ]
    return web.post(
        "/jobs/new",
        content=urlencode(items),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def assert_field_error(response, field: str, message: str) -> None:
    assert response.status_code == 422
    control = re.search(
        rf'<(?:input|select|textarea) id="{field}"[^>]*'
        rf'aria-describedby="[^"]*{field}-error[^"]*"[^>]*aria-invalid="true"[^>]*>',
        response.text,
    )
    error = re.search(rf'<p id="{field}-error" class="field-error"[^>]*>([^<]+)</p>', response.text)
    assert control is not None
    assert error is not None
    assert html.unescape(error.group(1)) == message
    assert control.start() < error.start()


@pytest.mark.parametrize("method", ["get", "post"])
def test_job_submit_requires_session(client, method):
    response = getattr(client, method)("/jobs/new", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_job_submit_form_has_all_fields_and_jobs_link(web, rig):
    response = web.get("/jobs/new")

    assert response.status_code == 200
    for name in ("cwd", "mode", "tier", "prompt", "timeout_s", "exclude_pools"):
        assert f'name="{name}"' in response.text
    assert '<textarea id="prompt" name="prompt"' in response.text
    for value in ("read", "write"):
        assert f'<option value="{value}"' in response.text
    for value in ("", "cheap", "standard", "strong"):
        assert f'<option value="{value}"' in response.text
    for pool in rig.dispatcher.config.pools:
        assert f'<option value="{pool.id}"' in response.text
    assert 'href="/jobs/new"' in web.get("/jobs").text


def test_job_submit_form_explains_when_tasks_are_disabled(client, tokens):
    authenticated_client(client, tokens)

    response = client.get("/jobs/new")

    assert response.status_code == 200
    assert "Jobs are disabled: the job dispatcher is not running." in response.text
    assert '<form method="post" action="/jobs/new"' not in response.text
    assert "Submit job" not in response.text


@pytest.mark.parametrize("csrf", [None, "wrong"])
def test_job_submit_with_bad_csrf_creates_no_job(web, rig, csrf):
    data = form_data(rig)
    if csrf is not None:
        data["csrf"] = csrf

    response = web.post("/jobs/new", data=data, follow_redirects=False)

    assert response.status_code == 403
    assert rig.jobs.list(limit=10) == []


def test_job_submit_creates_job_and_redirects_to_it(web, rig):
    response = submit(web, rig, mode="read", tier="strong", timeout_s="120")

    assert response.status_code == 303
    job_id = response.headers["location"].removeprefix("/jobs/")
    assert response.headers["location"] == f"/jobs/{job_id}"
    [job] = rig.jobs.list(limit=10)
    assert job.id == job_id
    assert job.status == "queued"
    assert job.request.prompt == "Review the current branch"
    assert job.request.cwd == rig.dispatcher.config.tasks.allowed_roots[0]
    assert job.request.mode == "read"
    assert job.request.tier == "strong"
    assert job.request.timeout_s == 120


def test_job_submit_empty_tier_uses_gateway_default(web, rig):
    response = submit(web, rig, tier="")

    assert response.status_code == 303
    [job] = rig.jobs.list(limit=10)
    assert job.request.tier is None
    assert job.tier == "standard"
    assert job.tier_source == "default"


def test_job_submit_without_excluded_pools(web, rig):
    response = submit(web, rig)

    assert response.status_code == 303
    [job] = rig.jobs.list(limit=10)
    assert job.excluded_pools == frozenset()


def test_job_submit_keeps_request_exclusions_separate(web, rig):
    response = submit(web, rig, exclude_pools=["claude-pro", "kimi-code"])

    assert response.status_code == 303
    [job] = rig.jobs.list(limit=10)
    assert job.request.excluded_pools == frozenset({"claude-pro", "kimi-code"})
    assert job.excluded_pools == frozenset()


def test_job_submit_drops_empty_excluded_pool_values(web, rig):
    response = submit(web, rig, exclude_pools=["", "claude-pro", ""])

    assert response.status_code == 303
    [job] = rig.jobs.list(limit=10)
    assert job.request.excluded_pools == frozenset({"claude-pro"})
    assert job.excluded_pools == frozenset()


def test_job_submit_unknown_excluded_pool_shows_field_error(web, rig):
    response = submit(web, rig, exclude_pools=["", "missing"])

    assert_field_error(response, "exclude_pools", "unknown pool: missing")
    assert rig.jobs.list(limit=10) == []


def test_job_submit_pool_select_size_is_valid_without_pools(
    home, config, store, jobstore, clock, tokens
):
    empty_config = replace(
        config,
        pools=(),
        routes=(),
        backends={
            name: replace(backend, executable=sys.executable)
            for name, backend in config.backends.items()
        },
    )
    rig = WebRig(home, empty_config, store, jobstore, clock, tokens)
    with TestClient(rig.app, base_url=BASE_URL) as client:
        authenticated_client(client, tokens)
        response = client.get("/jobs/new")

    assert response.status_code == 200
    control = re.search(r'<select id="exclude_pools"[^>]*>', response.text)
    assert control is not None
    assert 'size="1"' in control.group(0)


def test_job_submit_validation_error_keeps_excluded_pools(web, rig):
    response = submit(web, rig, exclude_pools=["claude-pro"], tier="giant")

    assert response.status_code == 422
    assert '<option value="claude-pro" selected>' in response.text
    assert '<option value="kimi-code" selected>' not in response.text
    assert rig.jobs.list(limit=10) == []


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"cwd": None}, "cwd", "directory outside allowed roots"),
        ({"prompt": "   "}, "prompt", "empty prompt"),
        (
            {"timeout_s": "soon"},
            "timeout_s",
            "timeout_s must be an integer number of seconds",
        ),
        ({"timeout_s": "5"}, "timeout_s", "timeout_s out of range 30..7200"),
        ({"timeout_s": "-5"}, "timeout_s", "timeout_s out of range 30..7200"),
        ({"tier": "giant"}, "tier", "unknown tier: giant"),
    ],
)
def test_job_submit_validation_errors_keep_values_and_create_no_job(
    web, rig, tmp_path, changes, field, message
):
    if field == "cwd":
        outside = tmp_path / "outside"
        outside.mkdir()
        changes["cwd"] = str(outside)

    response = submit(web, rig, **changes)
    data = form_data(rig, **changes)

    assert_field_error(response, field, message)
    assert f'value="{html.escape(data["cwd"], quote=True)}"' in response.text
    assert f'value="{data["timeout_s"]}"' in response.text
    assert f"{html.escape(data['prompt'])}</textarea>" in response.text
    assert f'<option value="{data["mode"]}" selected>' in response.text
    if data["tier"]:
        assert f'<option value="{data["tier"]}" selected>' in response.text
    assert rig.jobs.list(limit=10) == []


def test_job_submit_route_needing_approval_waits_for_human(web, rig):
    response = submit(web, rig)

    assert response.status_code == 303
    job_id = response.headers["location"].removeprefix("/jobs/")
    rig.dispatcher.tick()
    job = rig.jobs.get(job_id)
    assert job.status == "awaiting_approval", (job.error_message, rig.jobs.attempts(job_id))
    assert job.approval_route is not None
