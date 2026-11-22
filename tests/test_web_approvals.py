"""Approvals inbox: live region on the home page, job cards and action confirmations."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from helpers import BASE_URL
from test_web_dashboard import login
from test_web_jobs import WebRig, csrf_from


@pytest.fixture
def rig(home, config, store, jobstore, clock, tokens) -> WebRig:
    return WebRig(home, config, store, jobstore, clock, tokens)


@pytest.fixture
def web(rig) -> TestClient:
    with TestClient(rig.app, base_url=BASE_URL) as client:
        assert login(client, rig.tokens.admin).status_code == 303
        yield client


def approvals_section(text: str) -> str:
    """The inbox markup: from its live region up to the limits region (or the page end)."""
    found = re.search(r'<(?:div|section) id="approvals"', text)
    assert found is not None
    start = found.start()
    end = text.find('<div id="limits"', start)
    return text[start:] if end == -1 else text[start:end]


# --- inbox rendering --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/fragments/approvals"])
def test_empty_inbox_keeps_an_empty_live_region(web, path) -> None:
    text = web.get(path).text

    section = approvals_section(text)
    assert section.startswith('<section id="approvals" aria-live="polite">')
    assert 'href="/jobs/' not in section


def test_approvals_fragment_is_a_bare_fragment(web, rig) -> None:
    job = rig.add_job("awaiting_approval")

    response = web.get("/fragments/approvals")

    assert response.status_code == 200
    assert 'id="approvals"' in response.text
    assert f'href="/jobs/{job.id}"' in response.text
    assert "<html" not in response.text.lower()


def test_inbox_section_comes_before_limits(web, rig) -> None:
    rig.add_job("awaiting_approval")

    text = web.get("/").text

    assert text.index('<section id="approvals"') < text.index('<div id="limits"')
    assert 'aria-labelledby="approvals-title"' in approvals_section(text)
    assert 'aria-live="polite"' in approvals_section(text)


def test_inbox_omits_jobs_that_do_not_await_approval(web, rig) -> None:
    waiting = rig.add_job("awaiting_approval")
    queued = rig.add_job("queued")

    section = approvals_section(web.get("/").text)

    assert waiting.id in section
    assert queued.id not in section


def test_card_shows_link_route_pool_and_reasons(web, rig) -> None:
    job = rig.add_job("awaiting_approval")

    section = approvals_section(web.get("/").text)

    assert f'href="/jobs/{job.id}"' in section
    assert "kimi-cli/kimi-for-coding" in section
    assert "Kimi Code" in section  # title of the pool the approval route belongs to
    assert "requires confirmation" in section
    assert "route choice reason" in section  # job.reason


def test_card_escapes_the_prompt_and_truncates_it_to_200_characters(web, rig) -> None:
    prompt = "<script>" + "A" * 200 + "DO_NOT_SHOW"
    rig.add_job("awaiting_approval", prompt=prompt)

    section = approvals_section(web.get("/").text)

    assert "<script>" not in section
    assert "&lt;script&gt;" + "A" * 192 in section
    assert "A" * 193 not in section
    assert "DO_NOT_SHOW" not in section


def test_card_has_a_time_element_and_a_server_countdown(web, rig) -> None:
    job = rig.add_job("awaiting_approval")

    section = approvals_section(web.get("/").text)

    found = re.search(r'<time\b[^>]*datetime="([^"]+)"', section)
    assert found is not None
    moment = datetime.fromisoformat(found.group(1).replace("Z", "+00:00"))
    assert moment == job.approval_deadline
    assert "1 h" in section  # one hour left, computed on the server


def test_expired_deadline_renders_expired_with_a_warning_class(web, rig) -> None:
    rig.add_job("awaiting_approval", approval_deadline=rig.clock.now() - timedelta(minutes=5))

    section = approvals_section(web.get("/").text)

    found = re.search(r'<[^>]*class="([^"]*)"[^>]*>\s*expired', section, re.I)
    assert found is not None
    assert "approval-badge--warn" in found.group(1)


# --- actions from the inbox --------------------------------------------------------------------


def test_approve_form_posts_to_the_existing_route_with_csrf(web, rig) -> None:
    job = rig.add_job("awaiting_approval")

    section = approvals_section(web.get("/").text)
    assert f'action="/jobs/{job.id}/approve"' in section
    assert 'name="csrf"' in section

    csrf = csrf_from(web.get("/"))
    response = web.post(f"/jobs/{job.id}/approve", data={"csrf": csrf}, follow_redirects=False)

    assert response.status_code == 303
    assert rig.jobs.get(job.id).status == "queued"


def test_deny_is_a_get_link_to_a_confirmation_form(web, rig) -> None:
    job = rig.add_job("awaiting_approval")

    section = approvals_section(web.get("/").text)
    assert f'href="/jobs/{job.id}/deny"' in section
    assert f'action="/jobs/{job.id}/deny"' not in section  # no direct POST form in the inbox

    page = web.get(f"/jobs/{job.id}/deny")
    assert page.status_code == 200
    assert job.id in page.text
    assert "kimi-cli/kimi-for-coding" in page.text
    assert f'action="/jobs/{job.id}/deny"' in page.text

    csrf = csrf_from(page)
    response = web.post(f"/jobs/{job.id}/deny", data={"csrf": csrf}, follow_redirects=False)

    assert response.status_code == 303
    changed = rig.jobs.get(job.id)
    assert changed.status == "queued"
    assert changed.had_refusal


def test_cancel_on_the_job_page_is_a_get_link_to_a_confirmation_form(web, rig) -> None:
    job = rig.add_job("queued")

    page = web.get(f"/jobs/{job.id}")
    assert f'href="/jobs/{job.id}/cancel"' in page.text

    confirm = web.get(f"/jobs/{job.id}/cancel")
    assert confirm.status_code == 200
    assert job.id in confirm.text
    assert f'action="/jobs/{job.id}/cancel"' in confirm.text

    csrf = csrf_from(confirm)
    response = web.post(f"/jobs/{job.id}/cancel", data={"csrf": csrf}, follow_redirects=False)

    assert response.status_code == 303
    assert rig.jobs.get(job.id).status == "cancelled"


@pytest.mark.parametrize("action", ["deny", "cancel"])
def test_get_confirmation_requires_login(client, action) -> None:
    response = client.get(f"/jobs/j_web_1/{action}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize("action", ["deny", "cancel"])
def test_get_confirmation_of_a_missing_job_is_404(web, action) -> None:
    response = web.get(f"/jobs/j_missing/{action}")

    assert response.status_code == 404
    assert "job not found" in response.text


@pytest.mark.parametrize(
    ("action", "status"), [("approve", "awaiting_approval"), ("deny", "awaiting_approval")]
)
def test_post_without_csrf_is_forbidden_and_state_unchanged(web, rig, action, status) -> None:
    job = rig.add_job(status)

    response = web.post(f"/jobs/{job.id}/{action}")

    assert response.status_code == 403
    assert rig.jobs.get(job.id).status == status


# --- static script ---------------------------------------------------------------------------


def test_script_enhances_the_confirmation_dialog_and_ticks_countdowns(client) -> None:
    script = client.get("/static/app.js").text

    assert "showModal" in script
    assert "[data-confirm-action]" in script
    assert 'input[name="csrf"]' in script
    assert "setupApprovalCountdowns" in script


def test_deny_links_name_their_job(web, rig) -> None:
    job = rig.add_job("awaiting_approval")

    for path in ("/", "/jobs", f"/jobs/{job.id}"):
        text = web.get(path).text
        assert f'aria-label="Deny {job.id}">Deny</a>' in text


def test_countdowns_stay_out_of_the_live_region(web, rig) -> None:
    rig.add_job("awaiting_approval")

    section = approvals_section(web.get("/").text)

    countdowns = re.findall(r"<time\b[^>]*data-approval-countdown[^>]*>", section)
    assert countdowns
    assert all('aria-live="off"' in tag for tag in countdowns)


def test_refresh_keeps_focus_dialogs_and_wrapper_attributes(client) -> None:
    script = client.get("/static/app.js").text
    refresh = script[script.index("async function refresh") : script.index("setInterval(")]

    assert "current.contains(document.activeElement)" in refresh
    assert "dialog[open]" in refresh
    assert "fresh.attributes" in refresh
