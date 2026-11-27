"""Job page: framed console sections, header badge, totals, attempt timeline, copy buttons."""

from __future__ import annotations

import re
from datetime import UTC, timedelta

import pytest
from fastapi.testclient import TestClient

from agent_router.clock import format_age, format_local
from agent_router.jobs import Attempt
from helpers import BASE_URL
from test_web_jobs import WebRig, csrf_from


@pytest.fixture
def rig(home, config, store, jobstore, clock, tokens) -> WebRig:
    return WebRig(home, config, store, jobstore, clock, tokens)


@pytest.fixture
def web(rig) -> TestClient:
    with TestClient(rig.app, base_url=BASE_URL) as client:
        response = client.post("/login", data={"token": rig.tokens.admin})
        assert response.status_code == 200
        yield client


def job_page(web, job) -> str:
    response = web.get(f"/jobs/{job.id}")
    assert response.status_code == 200
    return response.text


def status_badge(text: str, kind: str, status: str) -> re.Match[str] | None:
    return re.search(
        r'<span\b(?=[^>]*class="status-badge status-badge--'
        + kind
        + r'")[^>]*>\s*'
        + re.escape(status)
        + r"\s*</span>",
        text,
    )


def add_attempt(rig: WebRig, job, n: int, **fields) -> None:
    # WebRig.add_attempt hardcodes n=1; numbered attempts go straight to the store
    rig.jobs.add_attempt(
        Attempt(
            job_id=job.id,
            n=n,
            route="kimi-cli/kimi-for-coding",
            pool="kimi-code",
            started_at=rig.clock.now(),
            finished_at=rig.clock.now(),
            outcome="ok",
            **fields,
        )
    )


# --- header and status badge -----------------------------------------------------------------


def test_running_job_has_header_id_badge_and_live_wrapper(web, rig) -> None:
    job = rig.add_job("running", caller="nightly", tier="cheap")

    text = job_page(web, job)

    headers = re.findall(r"<h2[^>]*>(.*?)</h2>", text, re.S)
    assert any(f"<code>{job.id}</code>" in header for header in headers)
    assert status_badge(text, "info", "running") is not None
    assert re.search(
        r'<div id="job"(?=[^>]*data-job-id="' + job.id + r'")[^>]*data-terminal="false"', text
    )
    header = re.search(r'<header\b(?=[^>]*class="job-header")[^>]*>(.*?)</header>', text, re.S)
    assert header is not None, "no header.job-header"
    assert re.search(r"<li>\s*Caller: nightly\s*</li>", header.group(1))
    assert re.search(r"<li>\s*Tier: cheap\s*</li>", header.group(1))
    assert f'action="/jobs/{job.id}/rate"' not in text
    assert "job-rating" not in text


def test_failed_job_shows_danger_flash_and_badge(web, rig) -> None:
    job = rig.add_job("failed", error_code="harness_error", error_message="harness exploded")

    text = job_page(web, job)

    assert re.search(r'<p\b(?=[^>]*class="flash flash--danger")(?=[^>]*role="alert")[^>]*>', text)
    assert "harness exploded" in text
    assert status_badge(text, "danger", "failed") is not None


# --- completed job: totals, timeline, rating ---------------------------------------------------


def totals_block(text: str) -> str:
    found = re.search(r'<dl\b(?=[^>]*class="job-totals")[^>]*>(.*?)</dl>', text, re.S)
    assert found is not None, "no dl.job-totals"
    return found.group(1)


def test_each_totals_pair_is_wrapped_in_its_own_div(web, rig) -> None:
    job = rig.add_job("completed", text="done")
    add_attempt(rig, job, 1, tokens_in=1000, tokens_out=200, cost_usd=0.01)

    text = job_page(web, job)

    pairs = re.findall(
        r"<div>\s*<dt>\s*([^<]+?)\s*</dt>\s*<dd>(.*?)</dd>\s*</div>",
        totals_block(text),
        re.S,
    )
    assert [label for label, _ in pairs] == ["Attempts", "Tokens", "Cost", "Duration"]


def test_totals_show_dashes_when_no_attempt_has_values(web, rig) -> None:
    job = rig.add_job("running")
    add_attempt(rig, job, 1)

    text = job_page(web, job)

    body = totals_block(text)
    assert re.search(r"<dt>\s*Attempts\s*</dt>\s*<dd>\s*1\s*</dd>", body)
    for label in ("Tokens", "Cost", "Duration"):
        assert re.search(r"<dt>\s*" + label + r"\s*</dt>\s*<dd>\s*—\s*</dd>", body), (
            f"no dash for {label}"
        )


def test_duration_falls_back_to_attempt_durations(web, rig) -> None:
    job = rig.add_job("running")
    add_attempt(rig, job, 1, duration_s=30.0)
    add_attempt(rig, job, 2, duration_s=90.0)

    text = job_page(web, job)

    assert format_age(120) in totals_block(text)


def test_attempts_without_log_tail_say_no_output(web, rig) -> None:
    job = rig.add_job("failed")
    add_attempt(rig, job, 1, log_tail=None)
    add_attempt(rig, job, 2, log_tail="")

    text = job_page(web, job)

    assert "No output." in text
    assert "attempt-1-tail" not in text
    assert "attempt-2-tail" not in text


def test_completed_job_shows_totals_timeline_and_rating(web, rig) -> None:
    created = rig.clock.now() + timedelta(seconds=1)
    job = rig.add_job(
        "completed",
        text="done",
        finished_at=created + timedelta(hours=1, minutes=2, seconds=3),
    )
    add_attempt(rig, job, 1, tokens_in=1000, tokens_out=200, cost_usd=0.01)
    add_attempt(rig, job, 2, tokens_in=500, tokens_out=100, cost_usd=0.0025)

    text = job_page(web, job)

    totals = re.search(r'<dl\b(?=[^>]*class="job-totals")[^>]*>(.*?)</dl>', text, re.S)
    assert totals is not None, "no dl.job-totals"
    assert re.search(r"<dt>\s*Attempts\s*</dt>\s*<dd>\s*2\s*</dd>", totals.group(1))
    tokens = re.search(r"<dt>\s*Tokens\s*</dt>\s*<dd>\s*(.*?)\s*</dd>", totals.group(1), re.S)
    assert tokens is not None and "1500" in tokens.group(1) and "300" in tokens.group(1)
    assert "$0.0125" in totals.group(1)
    assert format_age(3723) in totals.group(1)

    timeline = re.search(r'<ol\b(?=[^>]*class="attempt-timeline")[^>]*>(.*?)</ol>', text, re.S)
    assert timeline is not None, "no ol.attempt-timeline"
    assert timeline.group(1).count("<li") == 2

    assert f'action="/jobs/{job.id}/rate"' in text
    rating = re.search(r'<section\b(?=[^>]*class="panel job-rating")', text)
    assert rating is not None, "no section.panel.job-rating"
    header_end = text.index(f"<code>{job.id}</code>")
    between = text[header_end : rating.start()]
    assert "<section" not in between and "<dl" not in between and "<details" not in between


# --- the console framing ------------------------------------------------------------------------


def test_job_page_frames_its_sections(web, rig) -> None:
    job = rig.add_job("completed", text="done")
    add_attempt(rig, job, 1, log_tail="tail")

    text = job_page(web, job)

    assert text.count('<details open class="panel">') == 2
    assert text.count('<section class="panel">') == 3
    assert '<section class="panel job-rating">' in text


# --- awaiting approval: actions survive the redesign --------------------------------------------


def test_awaiting_approval_keeps_deadline_and_actions(web, rig) -> None:
    job = rig.add_job("awaiting_approval")

    response = web.get(f"/jobs/{job.id}")
    assert response.status_code == 200
    text = response.text

    assert format_local(job.approval_deadline, UTC) in text
    assert f'action="/jobs/{job.id}/approve"' in text
    assert csrf_from(response)
    for action in ("deny", "cancel"):
        link = re.search(
            r'<a\b(?=[^>]*href="/jobs/' + job.id + r"/" + action + r'")'
            r'(?=[^>]*data-confirm-action="' + action + r'")'
            r'(?=[^>]*aria-label="[^"]+")[^>]*>',
            text,
        )
        assert link is not None, f"no {action} link with data-confirm-action and aria-label"


# --- disclosures and copy buttons ---------------------------------------------------------------


def test_prompt_and_text_open_details_and_attempt_tails_closed(web, rig) -> None:
    job = rig.add_job("completed", prompt="the prompt", text="the answer")
    add_attempt(rig, job, 1, log_tail="first tail")
    add_attempt(rig, job, 2, log_tail="second tail")

    text = job_page(web, job)

    for target in ("job-prompt", "job-text"):
        found = re.search(
            r"<details\b(?=[^>]*\bopen\b)[^>]*>(?:(?!</details>).)*"
            r'<pre\b(?=[^>]*class="wrap")(?=[^>]*id="' + target + r'")',
            text,
            re.S,
        )
        assert found is not None, f"no open details with pre.wrap#{target}"
    for n in (1, 2):
        found = re.search(
            r"<details\b(?![^>]*\bopen\b)[^>]*>(?:(?!</details>).)*"
            r'<pre\b(?=[^>]*class="wrap")(?=[^>]*id="attempt-' + str(n) + r'-tail")',
            text,
            re.S,
        )
        assert found is not None, f"no closed details with pre.wrap#attempt-{n}-tail"


def test_copy_buttons_hidden_and_target_existing_unique_ids(web, rig) -> None:
    job = rig.add_job("completed", prompt="the prompt", text="the answer")
    rig.add_attempt(job, log_tail="tail")

    text = job_page(web, job)

    tags = [match.group(0) for match in re.finditer(r"<button\b[^>]*>", text)]
    copy = [tag for tag in tags if "data-copy-target" in tag]
    assert copy, "no copy buttons"
    targets = []
    for tag in copy:
        assert "hidden" in tag, f"copy button without hidden: {tag}"
        target = re.search(r'data-copy-target="([^"]+)"', tag)
        assert target is not None
        targets.append(target.group(1))
        assert text.count(f'id="{target.group(1)}"') == 1, f"no unique id {target.group(1)}"
    assert len(set(targets)) == len(targets), "copy targets repeat"


def test_app_js_sets_up_copy_buttons(client) -> None:
    script = client.get("/static/app.js").text

    assert "setupCopyButtons" in script
    assert "navigator.clipboard" in script


def test_refresh_keeps_closed_disclosures_closed(client) -> None:
    script = client.get("/static/app.js").text
    refresh = script[script.index("async function refresh") : script.index("setInterval(")]

    # prompt and final text render open: a refresh restores what the visitor chose, both ways
    assert "item.open = open[index]" in refresh


# --- attempt diffs --------------------------------------------------------------------------


SAMPLE_PATCH = (
    "diff --git a/src/файл.py b/src/файл.py\n"  # non-ascii on purpose
    "index 9daeafb..2a6de3f 100644\n"
    "--- a/src/файл.py\n"  # non-ascii on purpose
    "+++ b/src/файл.py\n"  # non-ascii on purpose
    "@@ -1,2 +1,2 @@\n"
    "-старая строка\n"  # non-ascii on purpose
    "+новая строка\n"  # non-ascii on purpose
)


def test_attempt_diff_is_a_closed_section_with_coloured_lines(web, rig) -> None:
    job = rig.add_job("completed", text="done")
    add_attempt(rig, job, 1, patch=SAMPLE_PATCH)

    text = job_page(web, job)

    found = re.search(
        r"<details\b(?![^>]*\bopen\b)[^>]*>(?:(?!</details>).)*"
        r'<pre\b(?=[^>]*id="attempt-1-patch")',
        text,
        re.S,
    )
    assert found is not None, "no closed diff details with pre#attempt-1-patch"
    add = re.search(
        r'<span class="diff-line diff-line--add">\+новая строка\n</span>',  # non-ascii on purpose
        text,
    )
    deleted = re.search(
        r'<span class="diff-line diff-line--del">-старая строка\n</span>',  # non-ascii on purpose
        text,
    )
    assert add is not None and deleted is not None
    assert 'data-copy-target="attempt-1-patch"' in text


def test_truncated_patch_says_so(web, rig) -> None:
    job = rig.add_job("completed", text="done")
    add_attempt(rig, job, 1, patch="diff --git a/x b/x\n", patch_truncated=True)

    text = job_page(web, job)

    assert "diff truncated at 256 KiB" in text


def test_attempt_without_patch_has_no_diff_section(web, rig) -> None:
    job = rig.add_job("completed", text="done")
    add_attempt(rig, job, 1, log_tail="tail")

    text = job_page(web, job)

    assert ">Diff<" not in text
    assert "attempt-1-patch" not in text
    assert "diff truncated at" not in text


def test_diff_line_colours_come_from_the_theme_tokens(client) -> None:
    css = client.get("/static/app.css").text

    for rule, colour in (
        (r"\.diff-line--add\s*\{[^}]*\}", ("var(--color-ok)", "var(--color-ok-bg)")),
        (r"\.diff-line--del\s*\{[^}]*\}", ("var(--color-danger)", "var(--color-danger-bg)")),
    ):
        found = re.search(rule, css)
        assert found is not None, f"no {rule} rule"
        for token in colour:
            assert token in found.group(0)
