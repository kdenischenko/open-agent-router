from __future__ import annotations

import re
import sys
from dataclasses import replace
from datetime import UTC, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_router import api as api_module
from agent_router.api import RatingBody, create_app
from agent_router.clock import format_local
from agent_router.events import EventBus
from agent_router.jobs import Attempt, Job, JobRequest
from agent_router.policy import PoolBlock
from agent_router.service import build_dispatcher
from helpers import BASE_URL, full_config_text, write_config


class WebRig:
    def __init__(self, home, config, store, jobstore, clock, tokens, tz=UTC) -> None:
        self.home = home
        self.clock = clock
        self.tokens = tokens
        self.jobs = jobstore
        self.bus = EventBus(clock)
        self.dispatcher = build_dispatcher(config, store, jobstore, self.bus, clock, home, tz)
        self.app = create_app(
            config,
            store,
            clock,
            tokens,
            home=home,
            bus=self.bus,
            dispatcher=self.dispatcher,
            background=False,
            tz=tz,
        )
        self._number = 0

    def add_job(
        self,
        status="queued",
        *,
        prompt="job prompt",
        text=None,
        tier="standard",
        tier_source="default",
        caller=None,
        **fields,
    ) -> Job:
        self._number += 1
        awaiting = status == "awaiting_approval"
        job = Job(
            id=f"j_web_{self._number}",
            request=JobRequest(prompt=prompt, cwd=self.home, caller=caller),
            status=status,
            tier=tier,
            tier_source=tier_source,
            repo_root=fields.pop("repo_root", None),
            route=fields.pop("route", "kimi-cli/kimi-for-coding"),
            reason=fields.pop("reason", "route choice reason"),
            approval_route=fields.pop("approval_route", "kimi-cli/kimi-for-coding")
            if awaiting
            else None,
            approval_deadline=fields.pop("approval_deadline", self.clock.now() + timedelta(hours=1))
            if awaiting
            else None,
            text=text,
            created_at=self.clock.now() + timedelta(seconds=self._number),
            **fields,
        )
        self.jobs.create(job)
        return job

    def add_attempt(self, job: Job, *, log_tail="output tail", **fields) -> None:
        self.jobs.add_attempt(
            Attempt(
                job_id=job.id,
                n=1,
                route="kimi-cli/kimi-for-coding",
                pool="kimi-code",
                started_at=self.clock.now(),
                finished_at=self.clock.now(),
                outcome="ok",
                log_tail=log_tail,
                **fields,
            )
        )


@pytest.fixture
def rig(home, config, store, jobstore, clock, tokens):
    return WebRig(home, config, store, jobstore, clock, tokens)


@pytest.fixture
def web(rig):
    with TestClient(rig.app, base_url=BASE_URL) as client:
        response = client.post("/login", data={"token": rig.tokens.admin})
        assert response.status_code == 200
        yield client


def csrf_from(response) -> str:
    found = re.search(r'<input[^>]+name="csrf"[^>]+value="([^"]+)"', response.text)
    assert found is not None
    return found.group(1)


def classifier_page(rig, path: str = "/jobs") -> str:
    with TestClient(rig.app, base_url=BASE_URL) as client:
        response = client.post("/login", data={"token": rig.tokens.admin})
        assert response.status_code == 200
        response = client.get(path)
        assert response.status_code == 200
        return response.text


def test_jobs_page_shows_classifier_agreement(home, config, store, jobstore, clock, tokens) -> None:
    config = replace(config, classifier=replace(config.classifier, kind="systemone"))
    rig = WebRig(home, config, store, jobstore, clock, tokens)
    rig.add_job(tier="cheap", tier_source="caller", classifier_tier="cheap")
    rig.add_job(tier="strong", tier_source="caller", classifier_tier="cheap")

    text = classifier_page(rig)

    assert "Classifier: 1 of 2 hints matched (50%)" in text


@pytest.mark.parametrize("path", ["/jobs", "/fragments/jobs"])
def test_jobs_page_shows_classifier_without_comparisons(
    path, home, config, store, jobstore, clock, tokens
) -> None:
    config = replace(config, classifier=replace(config.classifier, kind="systemone"))
    rig = WebRig(home, config, store, jobstore, clock, tokens)

    text = classifier_page(rig, path)

    assert "Classifier: no comparisons yet" in text


def test_jobs_page_hides_disabled_classifier(web) -> None:
    response = web.get("/jobs")
    assert "Classifier:" not in response.text


def test_jobs_page_puts_awaiting_approval_first_with_buttons(web, rig) -> None:
    waiting = rig.add_job("awaiting_approval", prompt="awaiting approval")
    queued = rig.add_job("queued", prompt="regular queue")

    response = web.get("/jobs")

    assert response.status_code == 200
    assert response.text.index(waiting.id) < response.text.index(queued.id)
    assert f'action="/jobs/{waiting.id}/approve"' in response.text
    assert f'href="/jobs/{waiting.id}/deny"' in response.text
    assert "Approve" in response.text
    assert "Deny" in response.text


@pytest.mark.parametrize("action", ["approve", "deny", "cancel"])
def test_job_post_without_csrf_is_forbidden(web, rig, action) -> None:
    status = "queued" if action == "cancel" else "awaiting_approval"
    job = rig.add_job(status)

    response = web.post(f"/jobs/{job.id}/{action}")

    assert response.status_code == 403
    assert rig.jobs.get(job.id).status == status


def test_unblock_without_csrf_is_forbidden(web, rig) -> None:
    rig.jobs.add_block(PoolBlock("kimi-code", rig.clock.now() + timedelta(hours=1), "test"), None)

    response = web.post("/pools/kimi-code/unblock")

    assert response.status_code == 403
    assert "kimi-code" in rig.jobs.active_blocks(rig.clock.now())


@pytest.mark.parametrize("path", ["/jobs/j_web/approve", "/jobs/j_web/deny", "/jobs/j_web/cancel"])
def test_job_post_without_session_redirects_to_login(client, path) -> None:
    response = client.post(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize("action", ["deny", "cancel"])
def test_confirmation_page_is_a_framed_panel(web, rig, action) -> None:
    status = "queued" if action == "cancel" else "awaiting_approval"
    job = rig.add_job(status)

    response = web.get(f"/jobs/{job.id}/{action}")

    assert response.status_code == 200
    assert '<section class="panel">' in response.text
    assert f'<a href="/jobs/{job.id}"><code>{job.id}</code></a>' in response.text
    assert f'action="/jobs/{job.id}/{action}"' in response.text
    assert '<p class="hint">' in response.text
    assert f'<a href="/jobs/{job.id}">Back to the job</a>' in response.text


def test_unblock_without_session_redirects_to_login(client) -> None:
    response = client.post("/pools/kimi-code/unblock", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_approve_form_changes_job_status(web, rig) -> None:
    job = rig.add_job("awaiting_approval")
    csrf = csrf_from(web.get("/jobs"))

    response = web.post(f"/jobs/{job.id}/approve", data={"csrf": csrf}, follow_redirects=False)

    assert response.status_code == 303
    changed = rig.jobs.get(job.id)
    assert changed.status == "queued"
    assert changed.approved_route == "kimi-cli/kimi-for-coding"
    assert changed.approval_route is None


def test_deny_form_excludes_route_and_requeues_job(web, rig) -> None:
    job = rig.add_job("awaiting_approval")
    csrf = csrf_from(web.get("/jobs"))

    response = web.post(f"/jobs/{job.id}/deny", data={"csrf": csrf}, follow_redirects=False)

    assert response.status_code == 303
    changed = rig.jobs.get(job.id)
    assert changed.status == "queued"
    assert changed.had_refusal
    assert "kimi-cli/kimi-for-coding" in changed.excluded_routes


def test_cancel_form_changes_job_status(web, rig) -> None:
    job = rig.add_job("queued")
    csrf = csrf_from(web.get("/jobs"))

    response = web.post(f"/jobs/{job.id}/cancel", data={"csrf": csrf}, follow_redirects=False)

    assert response.status_code == 303
    assert rig.jobs.get(job.id).status == "cancelled"


def test_unblock_form_clears_pool_block(web, rig) -> None:
    rig.jobs.add_block(PoolBlock("kimi-code", rig.clock.now() + timedelta(hours=1), "test"), None)
    csrf = csrf_from(web.get("/"))

    response = web.post("/pools/kimi-code/unblock", data={"csrf": csrf}, follow_redirects=False)

    assert response.status_code == 303
    assert "kimi-code" not in rig.jobs.active_blocks(rig.clock.now())


def test_job_page_escapes_prompt_and_executor_text(web, rig) -> None:
    prompt = "prompt <script>alert('prompt')</script>"
    output = "answer <script>alert('output')</script>"
    job = rig.add_job("completed", prompt=prompt, text=output)

    response = web.get(f"/jobs/{job.id}")

    assert response.status_code == 200
    assert prompt not in response.text
    assert output not in response.text
    assert "prompt &lt;script&gt;alert(" in response.text
    assert "answer &lt;script&gt;alert(" in response.text
    assert "|safe" not in response.text


def test_job_page_limits_prompt_to_2000_characters(web, rig) -> None:
    job = rig.add_job(prompt="A" * 2000 + "DO_NOT_SHOW")

    response = web.get(f"/jobs/{job.id}")

    assert "A" * 2000 in response.text
    assert "A" * 2001 not in response.text
    assert "DO_NOT_SHOW" not in response.text


def test_job_page_shows_result_details(web, rig) -> None:
    job = rig.add_job(
        "partial",
        text="final text",
        changed_files=("src/example.py",),
        violations=("violation",),
    )
    rig.add_attempt(job, log_tail="stderr tail")

    response = web.get(f"/jobs/{job.id}")

    assert response.status_code == 200
    for expected in (
        str(rig.home),
        "read",
        "kimi-cli/kimi-for-coding",
        "route choice reason",
        "final text",
        "src/example.py",
        "violation",
        "stderr tail",
    ):
        assert expected in response.text


def test_main_page_shows_closed_pools_and_awaiting_count(web, rig) -> None:
    rig.add_job("awaiting_approval")
    rig.add_job("queued")
    rig.jobs.add_block(
        PoolBlock("kimi-code", rig.clock.now() + timedelta(hours=1), "limit reached"), None
    )
    rig.bus.publish("pool.blocked", {"pool": "kimi-code", "reason": "limit reached"})

    response = web.get("/")

    assert response.status_code == 200
    assert "Closed pools" in response.text
    assert "Kimi Code" in response.text
    assert "limit reached" in response.text
    assert 'action="/pools/kimi-code/unblock"' in response.text
    assert "Unblock" in response.text
    assert "Awaiting approval: 1" in response.text


def test_missing_job_returns_404(web) -> None:
    response = web.get("/jobs/j_does_not_exist")

    assert response.status_code == 404
    assert "job not found" in response.text


@pytest.mark.parametrize("path", ["/jobs", "/fragments/jobs", "/jobs/j_disabled"])
def test_job_pages_explain_when_tasks_are_disabled(client, tokens, path) -> None:
    response = client.post("/login", data={"token": tokens.admin})
    assert response.status_code == 200

    response = client.get(path)

    assert response.status_code == 200
    assert "Jobs are disabled" in response.text
    assert "dispatcher" in response.text


def test_jobs_fragment_uses_same_awaiting_first_order(web, rig) -> None:
    waiting = rig.add_job("awaiting_approval")
    queued = rig.add_job("queued")

    response = web.get("/fragments/jobs")

    assert response.status_code == 200
    assert 'id="jobs"' in response.text
    assert response.text.index(waiting.id) < response.text.index(queued.id)


def test_jobs_script_refreshes_fragment_every_five_seconds(client) -> None:
    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert (
        "setInterval(() => refresh('/fragments/jobs' + location.search, 'jobs'), 5000)"
        in response.text
    )


# --- job page: error, live fragment, details, rating --------------------------------------

LOCAL_TZ = timezone(timedelta(hours=3))


def test_job_page_shows_error_before_any_attempt(web, rig) -> None:
    job = rig.add_job(
        "failed", error_code="no_route", error_message="no matching route: x/y: route disabled"
    )

    response = web.get(f"/jobs/{job.id}")

    assert response.status_code == 200
    assert "no_route" in response.text
    assert "no matching route: x/y: route disabled" in response.text
    assert response.text.index("no_route") < response.text.index("Prompt")


def test_job_page_shows_approval_route_and_deadline(
    home, config, store, jobstore, clock, tokens
) -> None:
    rig = WebRig(home, config, store, jobstore, clock, tokens, tz=LOCAL_TZ)
    job = rig.add_job("awaiting_approval")

    text = classifier_page(rig, f"/jobs/{job.id}")

    assert format_local(job.approval_deadline, LOCAL_TZ) in text
    assert "kimi-cli/kimi-for-coding" in text


@pytest.mark.parametrize(("status", "terminal"), [("running", "false"), ("completed", "true")])
def test_job_fragment_marks_terminal(web, rig, status, terminal) -> None:
    job = rig.add_job(status)

    response = web.get(f"/fragments/jobs/{job.id}")

    assert response.status_code == 200
    assert 'id="job"' in response.text
    assert f'data-terminal="{terminal}"' in response.text
    assert "<html" not in response.text.lower()


def test_job_fragment_needs_session(client) -> None:
    response = client.get("/fragments/jobs/j_web_1", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_job_page_wraps_fragment_and_script_refreshes_it(web, rig, client) -> None:
    job = rig.add_job("running")

    page = web.get(f"/jobs/{job.id}")
    script = client.get("/static/app.js")

    assert '<div id="job"' in page.text and 'data-terminal="false"' in page.text
    assert "/fragments/jobs/" in script.text
    assert "data-terminal" in script.text or "dataset.terminal" in script.text


def test_job_page_formats_attempt_times_locally(
    home, config, store, jobstore, clock, tokens
) -> None:
    rig = WebRig(home, config, store, jobstore, clock, tokens, tz=LOCAL_TZ)
    job = rig.add_job("completed")
    rig.add_attempt(job)

    text = classifier_page(rig, f"/jobs/{job.id}")

    assert format_local(clock.now(), LOCAL_TZ) in text
    assert "+00:00" not in text


def test_job_page_shows_duration_tokens_cost(web, rig) -> None:
    job = rig.add_job("completed")
    rig.add_attempt(job, duration_s=12.4, tokens_in=1500, tokens_out=320, cost_usd=0.01234)

    text = web.get(f"/jobs/{job.id}").text

    for expected in ("Duration", "Tokens", "Cost", "12", "1500", "320", "$0.0123"):
        assert expected in text


def test_job_page_leaves_unknown_cost_empty(web, rig) -> None:
    job = rig.add_job("completed")
    rig.add_attempt(job)

    assert "$" not in web.get(f"/jobs/{job.id}").text


def test_job_page_marks_truncated_prompt(web, rig) -> None:
    job = rig.add_job(prompt="B" * 2500, prompt_chars=2500)

    text = web.get(f"/jobs/{job.id}").text

    assert "Showing the first 2000 of 2500 characters." in text


def test_job_page_does_not_mark_short_prompt(web, rig) -> None:
    job = rig.add_job(prompt="short", prompt_chars=5)

    assert "Showing the first" not in web.get(f"/jobs/{job.id}").text


def test_job_page_changed_files_none_vs_empty(web, rig) -> None:
    untracked = rig.add_job("completed", changed_files=None)
    clean = rig.add_job("completed", changed_files=())

    assert "Not tracked: the directory is outside git." in web.get(f"/jobs/{untracked.id}").text
    assert "No files changed." in web.get(f"/jobs/{clean.id}").text


def test_job_page_changes_not_measured_yet(web, rig) -> None:
    job = rig.add_job("running", repo_root=rig.home, changed_files=None)

    text = web.get(f"/jobs/{job.id}").text

    assert "Not measured yet." in text
    assert "outside git" not in text


def test_job_page_changes_unknown_is_not_clean(web, rig) -> None:
    job = rig.add_job(
        "partial", repo_root=rig.home, changed_files=(), violations=("changes_unknown",)
    )

    text = web.get(f"/jobs/{job.id}").text

    assert "Could not measure the changed files." in text
    assert "No files changed." not in text


def test_job_page_shows_each_known_token_count(web, rig) -> None:
    job = rig.add_job("completed")
    rig.add_attempt(job, tokens_in=1500, tokens_out=None)

    assert "1500 / —" in web.get(f"/jobs/{job.id}").text


def test_job_script_polls_one_request_at_a_time(client) -> None:
    script = client.get("/static/app.js").text
    poll = script[script.index("function setupJobRefresh") :]
    poll = poll[: poll.index("\n}\n") + 3]

    assert "setInterval(" not in poll
    assert "setTimeout(" in poll
    assert "await refresh(" in poll


def test_rate_from_dashboard(web, rig) -> None:
    job = rig.add_job("completed", text="done")
    csrf = csrf_from(web.get(f"/jobs/{job.id}"))

    response = web.post(
        f"/jobs/{job.id}/rate",
        data={"csrf": csrf, "value": "good", "should_tier": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{job.id}"
    rating = rig.jobs.last_rating(job.id)
    assert (rating.value, rating.should_tier, rating.by) == ("good", None, "dashboard")
    assert "good" in web.get(f"/jobs/{job.id}").text


def test_rate_form_only_for_finished_job(web, rig) -> None:
    running = rig.add_job("running")
    done = rig.add_job("completed")

    assert f'action="/jobs/{running.id}/rate"' not in web.get(f"/jobs/{running.id}").text
    assert f'action="/jobs/{done.id}/rate"' in web.get(f"/jobs/{done.id}").text


def test_rate_non_terminal_job_shows_message(web, rig) -> None:
    job = rig.add_job("running")
    csrf = csrf_from(web.get("/jobs"))

    response = web.post(f"/jobs/{job.id}/rate", data={"csrf": csrf, "value": "good"})

    assert response.status_code == 409
    assert "not finished" in response.text
    assert f'href="/jobs/{job.id}"' in response.text
    assert "<html" in response.text.lower()


def test_rate_bad_value_shows_message(web, rig) -> None:
    job = rig.add_job("completed")
    csrf = csrf_from(web.get("/jobs"))

    response = web.post(f"/jobs/{job.id}/rate", data={"csrf": csrf, "value": "great"})

    assert response.status_code == 422
    assert "unknown rating: great" in response.text
    assert rig.jobs.last_rating(job.id) is None


def test_rate_without_csrf_rejected(web, rig) -> None:
    job = rig.add_job("completed")

    response = web.post(f"/jobs/{job.id}/rate", data={"value": "good"})

    assert response.status_code == 403
    assert 'href="/"' in response.text
    assert rig.jobs.last_rating(job.id) is None


def test_rate_without_session_redirects_to_login(client) -> None:
    response = client.post("/jobs/j_web/rate", data={"value": "good"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_action_error_page_links_back(web, rig) -> None:
    job = rig.add_job("completed")
    csrf = csrf_from(web.get("/jobs"))

    response = web.post(f"/jobs/{job.id}/approve", data={"csrf": csrf})

    assert response.status_code == 409
    assert "job is not awaiting approval: completed" in response.text
    assert f'href="/jobs/{job.id}"' in response.text
    assert "Back to the job" in response.text


def test_action_on_missing_job_shows_message(web) -> None:
    csrf = csrf_from(web.get("/jobs"))

    response = web.post("/jobs/j_missing/cancel", data={"csrf": csrf})

    assert response.status_code == 404
    assert "job not found" in response.text
    assert "<html" in response.text.lower()


def test_index_links_awaiting_approval(web, rig) -> None:
    rig.add_job("awaiting_approval")

    text = web.get("/").text

    assert re.search(r'<a href="/jobs"[^>]*>\s*Awaiting approval: 1\s*</a>', text)


def test_api_rate_function_matches_endpoint(rig) -> None:
    first = rig.add_job("completed")
    second = rig.add_job("completed")
    gateway = rig.app.state.gateway

    direct = api_module.rate(gateway, first.id, RatingBody(value="bad", should_tier="strong"))
    with pytest.raises(api_module.ApiError) as exc:
        api_module.rate(gateway, "j_missing", RatingBody(value="good"))
    assert exc.value.code == "job_not_found"
    with TestClient(rig.app, base_url=BASE_URL) as client:
        response = client.post(
            f"/api/v1/jobs/{second.id}/rating",
            json={"value": "bad", "should_tier": "strong"},
            headers={"Authorization": f"Bearer {rig.tokens.client}"},
        )

    assert response.status_code == 200
    via_http = response.json()
    assert direct["rating"]["value"] == via_http["rating"]["value"] == "bad"
    assert set(direct) == set(via_http)


def test_advice_shows_route_error_streak(home, store, jobstore, clock, tokens) -> None:
    # an existing executable keeps the route available whatever is installed on the machine
    kimi = f'[backends.kimi]\nexecutable = "{Path(sys.executable).as_posix()}"'
    config = write_config(
        home, full_config_text().replace('[backends.kimi]\nexecutable = ""', kimi)
    )
    rig = WebRig(home, config, store, jobstore, clock, tokens)
    for n in (1, 2):
        job = rig.add_job("failed")
        rig.jobs.add_attempt(
            Attempt(
                job_id=job.id,
                n=n,
                route="kimi-cli/k3.1-pro",
                pool="kimi-code",
                started_at=rig.clock.now(),
                finished_at=rig.clock.now(),
                outcome="error",
            )
        )
    with TestClient(rig.app, base_url=BASE_URL) as client:
        response = client.get(
            "/api/v1/advice?tier=standard",
            headers={"Authorization": f"Bearer {rig.tokens.client}"},
        )

    assert response.status_code == 200
    excluded = {item["route"]: item["reason"] for item in response.json()["excluded"]}
    assert excluded["kimi-cli/k3.1-pro"].startswith(
        "harness unavailable: 2 errors in a row, retry after "
    )
