"""Browser smoke tests: the dashboard in headless Chromium against an in-process gateway."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from test_web_jobs import WebRig

pytestmark = pytest.mark.smoke
sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def rig(home, config, store, jobstore, clock, tokens):
    return WebRig(home, config, store, jobstore, clock, tokens)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def context(browser):
    with browser.new_context() as instance:
        yield instance


@pytest.fixture
def live_server(rig):
    server = uvicorn.Server(uvicorn.Config(rig.app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive():
                raise RuntimeError("uvicorn stopped before startup completed")
            if time.monotonic() >= deadline:
                raise TimeoutError("uvicorn did not start within 10 seconds")
            time.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "uvicorn did not stop within 10 seconds"


def log_in(context, base_url: str, token: str):
    page = context.new_page()
    page.goto(f"{base_url}/")
    page.get_by_label("Admin token").fill(token)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{base_url}/")
    sync_api.expect(page.get_by_role("heading", name="Limits", exact=True)).to_be_visible()
    return page


def test_login(context, live_server, rig) -> None:
    page = context.new_page()

    page.goto(f"{live_server}/")

    sync_api.expect(page.get_by_role("heading", name="Log in", exact=True)).to_be_visible()
    page.get_by_label("Admin token").fill(rig.tokens.admin)
    page.get_by_role("button", name="Sign in").click()

    page.wait_for_url(f"{live_server}/")
    sync_api.expect(page.get_by_role("heading", name="Limits", exact=True)).to_be_visible()


def test_home_approves_job(context, live_server, rig) -> None:
    job = rig.add_job("awaiting_approval")
    page = log_in(context, live_server, rig.tokens.admin)
    approvals = page.locator("#approvals")

    sync_api.expect(approvals).to_contain_text(job.id)
    approvals.get_by_role("button", name="Approve", exact=True).click()

    page.wait_for_url(f"{live_server}/jobs/{job.id}")
    assert rig.jobs.get(job.id).status == "queued"
    page.goto(f"{live_server}/")
    sync_api.expect(page.locator("#approvals")).not_to_contain_text(job.id)


def test_deny_confirmation(context, live_server, rig) -> None:
    job = rig.add_job("awaiting_approval")
    page = log_in(context, live_server, rig.tokens.admin)

    page.get_by_role("link", name=f"Deny {job.id}", exact=True).click()
    dialog = page.locator("dialog.confirm-dialog")
    sync_api.expect(dialog).to_be_visible()
    sync_api.expect(dialog).to_contain_text(f"Deny the route for {job.id}?")
    dialog.get_by_role("button", name="Deny", exact=True).click()

    page.wait_for_url(f"{live_server}/jobs/{job.id}")
    assert rig.jobs.get(job.id).status == "queued"
    page.goto(f"{live_server}/")
    sync_api.expect(page.locator("#approvals")).not_to_contain_text(job.id)


def test_job_list_status_filter(context, live_server, rig) -> None:
    jobs = [rig.add_job(status) for status in ("queued", "completed", "failed")]
    page = log_in(context, live_server, rig.tokens.admin)
    page.goto(f"{live_server}/jobs")

    page.locator('select[name="status"]').select_option("completed")
    page.get_by_role("button", name="Filter", exact=True).click()

    page.wait_for_url("**/jobs?status=completed*")
    rows = page.locator("li.job-row")
    sync_api.expect(rows).to_have_count(1)
    sync_api.expect(rows).to_have_attribute("data-status", "completed")
    sync_api.expect(rows).to_contain_text(jobs[1].id)

    page.get_by_role("link", name="Clear filters", exact=True).click()

    page.wait_for_url(f"{live_server}/jobs")
    sync_api.expect(rows).to_have_count(len(jobs))
    for job in jobs:
        sync_api.expect(rows.filter(has_text=job.id)).to_have_count(1)


def test_open_job_from_list(context, live_server, rig) -> None:
    job = rig.add_job("completed")
    page = log_in(context, live_server, rig.tokens.admin)
    page.goto(f"{live_server}/jobs")

    page.get_by_role("link", name=job.id, exact=True).click()

    page.wait_for_url(f"{live_server}/jobs/{job.id}")
    sync_api.expect(page.get_by_role("heading", name=f"Job {job.id}", exact=True)).to_be_visible()
    sync_api.expect(page.locator("#job")).to_have_attribute("data-job-id", job.id)


def test_job_list_refreshes_live(context, live_server, rig) -> None:
    page = log_in(context, live_server, rig.tokens.admin)
    page.goto(f"{live_server}/jobs")
    sync_api.expect(page.locator("#jobs")).to_be_visible()

    job = rig.add_job("queued", prompt="added after the page loaded")

    sync_api.expect(page.get_by_role("link", name=job.id, exact=True)).to_be_visible(timeout=10_000)


def test_mobile_layout_and_unique_ids(context, live_server, rig, config) -> None:
    assert len(config.pools) > 1
    job = rig.add_job("completed")
    page = log_in(context, live_server, rig.tokens.admin)
    page.set_viewport_size({"width": 375, "height": 812})

    for path in ("/", "/jobs", f"/jobs/{job.id}", "/calibrate"):
        page.goto(f"{live_server}{path}")
        fits_viewport = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        duplicate_ids = page.evaluate(
            """() => {
                const seen = new Set();
                const duplicates = new Set();
                document.querySelectorAll('[id]').forEach((element) => {
                    if (seen.has(element.id)) duplicates.add(element.id);
                    seen.add(element.id);
                });
                return Array.from(duplicates);
            }"""
        )
        assert fits_viewport, f"horizontal overflow on {path}"
        assert duplicate_ids == [], f"duplicate ids on {path}: {duplicate_ids}"


def test_theme_screenshots(context, live_server, rig, tmp_path) -> None:
    rig.add_job("completed")
    page = log_in(context, live_server, rig.tokens.admin)
    output = (
        Path(os.environ["AGENT_ROUTER_SMOKE_SHOTS"])
        if os.environ.get("AGENT_ROUTER_SMOKE_SHOTS")
        else tmp_path
    )
    output.mkdir(parents=True, exist_ok=True)

    def capture(name: str) -> None:
        target = output / f"{name}.png"
        page.screenshot(path=target, full_page=True)
        assert target.is_file(), name
        assert target.stat().st_size > 0, name

    page.set_viewport_size({"width": 1280, "height": 900})
    for color_scheme in ("light", "dark"):
        page.emulate_media(color_scheme=color_scheme)
        for name, path in (("home", "/"), ("jobs", "/jobs")):
            page.goto(f"{live_server}{path}")
            capture(f"{name}-{color_scheme}")

    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{live_server}/")
    capture("home-mobile")
