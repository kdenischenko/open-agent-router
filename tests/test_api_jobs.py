"""Job API: submit, read, wait, actions, worker loop."""

from __future__ import annotations

import asyncio
import hashlib
import math
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from agent_router import api as api_module
from agent_router.adapters import RunOutcome
from agent_router.api import create_app
from agent_router.events import EventBus
from agent_router.jobs import TERMINAL, Job, JobRequest
from agent_router.jobstore import open_jobstore
from agent_router.policy import PoolBlock
from agent_router.runner import Invocation, ProcessResult
from agent_router.service import AsyncWaiters, build_dispatcher, worker_loop
from helpers import (
    BASE_URL,
    STORE_FACTORIES,
    bearer,
    make_git_repo,
    pid_alive,
    write_config,
)

HARNESS = Path(__file__).parent / "stubs" / "fake_harness.py"
PREFIX = "fake:"


class FakeAdapter:
    """The fake harness scenario comes from the prompt line `fake: <scenario> [args]`."""

    name = "fake"

    reports_progress = True

    def build(self, text, mode, model, exe, prompt_file) -> Invocation:
        line = next((ln for ln in text.splitlines() if ln.startswith(PREFIX)), PREFIX + " exit 0")
        scenario = line[len(PREFIX) :].split()
        if scenario == ["echo"]:
            scenario = ["echo-file", str(prompt_file)]
        return Invocation((str(exe.path), str(HARNESS), "--scenario", *scenario))

    def parse(self, stdout: str, stderr: str, exit_code: int | None) -> RunOutcome:
        if exit_code == 0:
            return RunOutcome("ok", text=stdout.strip(), tokens_in=3, tokens_out=2)
        return RunOutcome("error", error=f"exit code {exit_code}")

    def is_final_line(self, line: str) -> bool:
        return False


def config_text(
    root: Path, *, confirm: bool = False, max_queued: int = 200, classifier_kind: str = "none"
) -> str:
    return "\n".join(
        [
            "[tasks]",
            f"allowed_roots = ['{root.as_posix()}']",
            "max_parallel = 2",
            f"max_queued = {max_queued}",
            "max_attempts = 1",
            "",
            "[callers]",
            'agent-a = "pool-a"',
            "",
            "[backends.fake]",
            f"executable = '{Path(sys.executable).as_posix()}'",
            "",
            "[classifier]",
            f'kind = "{classifier_kind}"',
            "",
            "[[pools]]",
            'id = "pool-a"',
            'title = "pool-a"',
            'unit = "percent"',
            "  [[pools.windows]]",
            '  id = "5h"',
            '  duration = "5h"',
            '  kind = "rolling"',
            '  role = "guard"',
            "  [[pools.windows]]",
            '  id = "week"',
            '  duration = "7d"',
            '  kind = "fixed"',
            '  role = "pace"',
            "",
            "[[routes]]",
            'id = "fake/main"',
            'pool = "pool-a"',
            'backend = "fake"',
            'model = "model-x"',
            'tiers = ["cheap", "standard", "strong"]',
            "quality = 4",
            'modes = ["read", "write"]',
            f"confirm = {'true' if confirm else 'false'}",
            "",
        ]
    )


class Rig:
    """Gateway with a real dispatcher and runner plus a fake harness."""

    def __init__(self, tmp_path, home, store, clock, tokens, background=False, **opts) -> None:
        self.root = tmp_path / "рабочие каталоги"  # non-ascii on purpose
        self.root.mkdir()
        self.config = write_config(home, config_text(self.root, **opts))
        self.tokens = tokens
        self.store = store
        self.bus = EventBus(clock)
        self.jobs = open_jobstore(self.config.storage, home, store)
        self.dispatcher = build_dispatcher(
            self.config, store, self.jobs, self.bus, clock, home, UTC
        )
        self.app = create_app(
            self.config,
            store,
            clock,
            tokens,
            home=home,
            bus=self.bus,
            dispatcher=self.dispatcher,
            background=background,
        )

    def plain(self, name: str = "каталог задачи") -> Path:  # non-ascii on purpose
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def repo(self, name: str = "репозиторий") -> Path:  # non-ascii on purpose
        return make_git_repo(self.root / name)

    def body(self, scenario: str = "exit 0", cwd: Path | None = None, **fields) -> dict:
        prompt = f"test job\n{PREFIX} {scenario}\n"
        return {"prompt": prompt, "cwd": str(cwd or self.plain()), **fields}

    def drive(self, job_id: str, seconds: float = 30) -> dict[str, Any]:
        """Ticks the dispatcher manually until the job finishes."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.dispatcher.tick()
            job = self.jobs.get(job_id)
            assert job is not None
            if job.status in TERMINAL:
                return {"status": job.status}
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish in {seconds} s")


@pytest.fixture
def fake_backend(register_harness) -> None:
    from agent_router.registry import HarnessSpec

    register_harness("fake", HarnessSpec("fake", FakeAdapter(), ("fake",), "journal"))


@pytest.fixture
def make_rig(tmp_path, home, store, clock, tokens, fake_backend):
    def build(**opts) -> Rig:
        return Rig(tmp_path, home, store, clock, tokens, **opts)

    return build


@pytest.fixture
def rig(make_rig) -> Rig:
    return make_rig()


@pytest.fixture
def api(rig: Rig) -> Iterator[TestClient]:
    with TestClient(rig.app, base_url=BASE_URL) as instance:
        yield instance


def cl(rig: Rig) -> dict[str, str]:
    return bearer(rig.tokens.client)


def adm(rig: Rig) -> dict[str, str]:
    return bearer(rig.tokens.admin)


def post(api: TestClient, rig: Rig, **body) -> str:
    response = api.post("/api/v1/jobs", json=rig.body(**body), headers=cl(rig))
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "queued"
    return response.json()["job_id"]


def run_job(api: TestClient, rig: Rig, **body) -> dict[str, Any]:
    job_id = post(api, rig, **body)
    rig.drive(job_id)
    response = api.get(f"/api/v1/jobs/{job_id}", headers=cl(rig))
    assert response.status_code == 200
    return response.json()


JOB_FIELDS = {
    "id",
    "status",
    "mode",
    "tier",
    "tier_source",
    "classifier_tier",
    "classifier_confidence",
    "prompt_sha256",
    "prompt_chars",
    "kind",
    "caller",
    "route",
    "reason",
    "approval",
    "attempts",
    "text",
    "changed_files",
    "violations",
    "error",
    "log_tail",
    "created_at",
    "finished_at",
    "rating",
    "cancel_requested",
}


# --- submit and read ----------------------------------------------------------------------


def test_job_runs_to_success_and_has_fields(api, rig) -> None:
    job = run_job(api, rig, scenario="exit 0")
    assert set(job) == JOB_FIELDS
    assert job["status"] == "completed"
    assert job["text"] == "exiting"
    assert job["route"] == "fake/main" and job["mode"] == "read"
    assert job["tier"] == "standard" and job["tier_source"] == "default"
    assert job["error"] is None and job["approval"] is None
    [attempt] = job["attempts"]
    assert attempt["route"] == "fake/main" and attempt["pool"] == "pool-a"
    assert attempt["tokens_in"] == 3 and attempt["tokens_out"] == 2
    assert job["created_at"] and job["finished_at"]


def test_job_json_has_null_classifier_fields_by_default(api, rig) -> None:
    job_id = post(api, rig)
    job = api.get(f"/api/v1/jobs/{job_id}", headers=cl(rig)).json()
    assert job["classifier_tier"] is None
    assert job["classifier_confidence"] is None


def test_job_json_has_prompt_hash_and_chars(api, rig) -> None:
    body = rig.body()
    job_id = post(api, rig, **body)
    job = api.get(f"/api/v1/jobs/{job_id}", headers=cl(rig)).json()
    assert job["prompt_chars"] == len(body["prompt"])
    assert job["prompt_sha256"] == hashlib.sha256(body["prompt"].encode("utf-8")).hexdigest()


def test_failed_job_reports_error(api, rig) -> None:
    job = run_job(api, rig, scenario="exit 3")
    assert job["status"] == "failed"
    assert job["error"] is not None and job["error"]["code"]


def test_job_json_has_no_prompt_and_no_tokens(api, rig) -> None:
    job_id = post(api, rig, scenario="echo")
    rig.drive(job_id)
    for path in (f"/api/v1/jobs/{job_id}", "/api/v1/jobs", f"/api/v1/jobs/{job_id}/wait?timeout=0"):
        text = api.get(path, headers=cl(rig)).text
        assert rig.tokens.client not in text and rig.tokens.admin not in text
        assert '"prompt"' not in text


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"cwd": "."}, "bad_request"),  # no prompt
        ({"prompt": 1}, "bad_request"),
        ({"extra": "x"}, "bad_request"),
        ({"mode": "delete"}, "bad_mode"),
        ({"tier": "giant"}, "bad_tier"),
        ({"route": "no/such"}, "bad_route"),
        ({"caller": "stranger"}, "bad_caller"),
        ({"timeout_s": 1.5}, "bad_timeout"),
        ({"timeout_s": True}, "bad_timeout"),
        ({"prompt": "   "}, "bad_prompt"),
        ({"prompt": "x" * 200_001}, "bad_prompt"),  # default max_prompt_chars
        ({"kind": "nonsense"}, "bad_kind"),
    ],
)
def test_bad_request_is_422_with_code(api, rig, body, code) -> None:
    payload = rig.body()
    payload.update(body)
    if "prompt" not in body and body.get("cwd") == ".":
        del payload["prompt"]
    response = api.post("/api/v1/jobs", json=payload, headers=cl(rig))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code


def test_cwd_outside_roots_is_rejected(api, rig, tmp_path) -> None:
    outside = tmp_path / "outside dir"
    outside.mkdir()
    response = api.post("/api/v1/jobs", json=rig.body(cwd=outside), headers=cl(rig))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "cwd_not_allowed"


def test_known_caller_is_accepted(api, rig) -> None:
    job_id = post(api, rig, caller="agent-a")
    assert api.get(f"/api/v1/jobs/{job_id}", headers=cl(rig)).json()["caller"] == "agent-a"


def test_queue_full_is_429(make_rig) -> None:
    rig = make_rig(max_queued=1)
    with TestClient(rig.app, base_url=BASE_URL) as api:
        post(api, rig)
        response = api.post("/api/v1/jobs", json=rig.body(), headers=cl(rig))
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "queue_full"


JOB_ROUTES = (
    ("POST", "/api/v1/jobs"),
    ("GET", "/api/v1/jobs"),
    ("GET", "/api/v1/jobs/j-1"),
    ("GET", "/api/v1/jobs/j-1/wait?timeout=0"),
    ("POST", "/api/v1/jobs/j-1/cancel"),
    ("POST", "/api/v1/jobs/j-1/approve"),
    ("POST", "/api/v1/jobs/j-1/deny"),
    ("POST", "/api/v1/pools/pool-a/unblock"),
)
ADMIN_ROUTES = {"/api/v1/jobs/j-1/approve", "/api/v1/jobs/j-1/deny", "/api/v1/pools/pool-a/unblock"}


@pytest.mark.parametrize(("method", "path"), JOB_ROUTES)
def test_jobs_require_token(api, rig, method, path) -> None:
    assert api.request(method, path).status_code == 401
    if path in ADMIN_ROUTES:
        assert api.request(method, path, headers=cl(rig)).status_code == 403


def test_unknown_job_is_404(api, rig) -> None:
    for method, path in (
        ("get", "/api/v1/jobs/j-missing"),
        ("get", "/api/v1/jobs/j-missing/wait?timeout=0"),
        ("post", "/api/v1/jobs/j-missing/cancel"),
    ):
        response = api.request(method.upper(), path, headers=cl(rig))
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found"
    for path in ("/api/v1/jobs/j-missing/approve", "/api/v1/jobs/j-missing/deny"):
        assert api.post(path, headers=adm(rig)).status_code == 404


def test_cancel_queued_then_finished_is_409(api, rig) -> None:
    job_id = post(api, rig)
    first = api.post(f"/api/v1/jobs/{job_id}/cancel", headers=cl(rig))
    assert first.status_code == 200 and first.json()["status"] == "cancelled"
    again = api.post(f"/api/v1/jobs/{job_id}/cancel", headers=cl(rig))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "wrong_state"


def test_cancel_reports_whether_a_cancel_was_requested(api, rig) -> None:
    queued = post(api, rig)
    running = post(api, rig)
    job = rig.jobs.get(running)
    assert job is not None
    job.status = "running"
    rig.jobs.update(job)

    first = api.post(f"/api/v1/jobs/{queued}/cancel", headers=cl(rig)).json()
    second = api.post(f"/api/v1/jobs/{running}/cancel", headers=cl(rig)).json()

    assert first["cancel_requested"] is False
    assert second["cancel_requested"] is True
    fetched = api.get(f"/api/v1/jobs/{running}", headers=cl(rig)).json()
    assert fetched["cancel_requested"] is True


def test_list_filters_by_status_and_checks_params(api, rig) -> None:
    done = run_job(api, rig)["id"]
    queued = post(api, rig)
    everything = api.get("/api/v1/jobs", headers=cl(rig)).json()["jobs"]
    assert [job["id"] for job in everything] == [queued, done]  # newest first
    only = api.get("/api/v1/jobs?status=queued", headers=cl(rig)).json()["jobs"]
    assert [job["id"] for job in only] == [queued]
    assert len(api.get("/api/v1/jobs?limit=1", headers=cl(rig)).json()["jobs"]) == 1
    for query in ("status=nonsense", "limit=0", "limit=201", "limit=x"):
        response = api.get(f"/api/v1/jobs?{query}", headers=cl(rig))
        assert response.status_code == 422, query
        assert response.json()["error"]["code"] == "bad_request"


# --- ratings ------------------------------------------------------------------------------


def rate(api: TestClient, rig: Rig, job_id: str, **body) -> Any:
    return api.post(f"/api/v1/jobs/{job_id}/rating", json=body, headers=cl(rig))


def test_job_json_has_null_rating_by_default(api, rig) -> None:
    job_id = post(api, rig)
    job = api.get(f"/api/v1/jobs/{job_id}", headers=cl(rig)).json()
    assert job["rating"] is None


def test_rate_returns_job_with_rating(api, rig) -> None:
    job_id = run_job(api, rig)["id"]
    secret = "заметка про C:/Users/<user>/секрет"  # non-ascii on purpose
    response = rate(api, rig, job_id, value="good", should_tier="cheap", by="agent-a", note=secret)

    assert response.status_code == 200, response.text
    rating = response.json()["rating"]
    assert rating["job_id"] == job_id
    assert rating["value"] == "good"
    assert rating["should_tier"] == "cheap"
    assert rating["by"] == "agent-a"
    assert rating["note_sha256"] == hashlib.sha256(secret.encode("utf-8")).hexdigest()
    assert rating["note_chars"] == len(secret)
    assert rating["at"]
    assert "note" not in rating
    assert secret not in response.text
    again = api.get(f"/api/v1/jobs/{job_id}", headers=cl(rig)).json()
    assert again["rating"] == rating


def test_rate_twice_last_wins_including_empty_should_tier(api, rig) -> None:
    job_id = run_job(api, rig)["id"]
    assert rate(api, rig, job_id, value="good", should_tier="strong").status_code == 200

    response = rate(api, rig, job_id, value="bad")

    assert response.status_code == 200
    rating = response.json()["rating"]
    assert rating["value"] == "bad"
    assert rating["should_tier"] is None
    stored = rig.jobs.last_rating(job_id)
    assert stored is not None
    assert stored.value == "bad" and stored.should_tier is None


def test_rate_unknown_job_is_404(api, rig) -> None:
    response = rate(api, rig, "j-missing", value="good")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


def test_rate_running_job_is_409(api, rig) -> None:
    job_id = post(api, rig)
    job = rig.jobs.get(job_id)
    assert job is not None
    job.status = "running"
    rig.jobs.update(job)

    response = rate(api, rig, job_id, value="good")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_finished"


def test_rate_bad_value_is_422(api, rig) -> None:
    job_id = run_job(api, rig)["id"]

    response = rate(api, rig, job_id, value="ok")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "bad_rating"


def test_rate_note_not_in_db_and_event_has_no_note(api, rig) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    rig.bus.subscribe("test-rating", lambda event: seen.append((event.type, dict(event.data))))

    job_id = run_job(api, rig)["id"]
    secret = "заметка про C:/Users/<user>/секрет"  # non-ascii on purpose
    response = rate(api, rig, job_id, value="partial", should_tier="standard", note=secret)

    assert response.status_code == 200
    rows = (
        rig.store.connection()
        .execute("SELECT job_id, value, should_tier, by, note_sha256, note_chars, at FROM ratings")
        .fetchall()
    )
    assert rows
    assert secret not in " ".join(str(part) for row in rows for part in row)
    rated = [data for kind, data in seen if kind == "job.rated"]
    assert rated == [{"job_id": job_id, "value": "partial", "should_tier": "standard"}]
    assert all("note" not in data for _, data in seen)


# --- confirmations and pools ----------------------------------------------------------------


def awaiting(api: TestClient, rig: Rig) -> str:
    job_id = post(api, rig)
    rig.dispatcher.tick()
    job = api.get(f"/api/v1/jobs/{job_id}", headers=cl(rig)).json()
    assert job["status"] == "awaiting_approval"
    assert job["approval"]["route"] == "fake/main" and job["approval"]["deadline"]
    return job_id


def test_approve_needs_admin_and_requeues(make_rig) -> None:
    rig = make_rig(confirm=True)
    with TestClient(rig.app, base_url=BASE_URL) as api:
        job_id = awaiting(api, rig)
        assert api.post(f"/api/v1/jobs/{job_id}/approve", headers=cl(rig)).status_code == 403
        response = api.post(f"/api/v1/jobs/{job_id}/approve", headers=adm(rig))
        assert response.status_code == 200
        assert response.json()["status"] == "queued" and response.json()["approval"] is None
        rig.drive(job_id)
        assert api.get(f"/api/v1/jobs/{job_id}", headers=cl(rig)).json()["status"] == "completed"
        again = api.post(f"/api/v1/jobs/{job_id}/approve", headers=adm(rig))
        assert again.status_code == 409


def test_deny_needs_admin(make_rig) -> None:
    rig = make_rig(confirm=True)
    with TestClient(rig.app, base_url=BASE_URL) as api:
        job_id = awaiting(api, rig)
        assert api.post(f"/api/v1/jobs/{job_id}/deny", headers=cl(rig)).status_code == 403
        response = api.post(f"/api/v1/jobs/{job_id}/deny", headers=adm(rig))
        assert response.status_code == 200
        assert response.json()["approval"] is None
        again = api.post(f"/api/v1/jobs/{job_id}/deny", headers=adm(rig))
        assert again.status_code == 409  # job no longer awaits approval
        assert again.json()["error"]["code"] == "wrong_state"


def test_approve_and_deny_of_queued_job_are_409(api, rig) -> None:
    job_id = post(api, rig)
    for action in ("approve", "deny"):
        response = api.post(f"/api/v1/jobs/{job_id}/{action}", headers=adm(rig))
        assert response.status_code == 409, action
        assert response.json()["error"]["code"] == "wrong_state"


def test_approve_after_deadline_is_409_and_declines(make_rig, clock) -> None:
    rig = make_rig(confirm=True)
    with TestClient(rig.app, base_url=BASE_URL) as api:
        job_id = awaiting(api, rig)
        clock.advance(timedelta(seconds=600))
        response = api.post(f"/api/v1/jobs/{job_id}/approve", headers=adm(rig))
        assert response.status_code == 409
        assert response.json()["error"] == {
            "code": "wrong_state",
            "message": "approval deadline expired",
        }
        job = api.get(f"/api/v1/jobs/{job_id}", headers=cl(rig)).json()
        assert job["status"] == "declined" and job["error"]["code"] == "approval_expired"
        assert job["approval"] is None


def test_unblock_pool(api, rig, clock) -> None:
    rig.jobs.add_block(PoolBlock("pool-a", clock.now() + timedelta(hours=1), "test"), None)
    calibration = {"pool": "pool-a", "window": "5h", "percent": 10}
    assert api.post("/api/v1/observations", json=calibration, headers=adm(rig)).status_code == 200
    assert "pool-a" in rig.jobs.active_blocks(clock.now())  # calibration does not clear the block
    assert api.post("/api/v1/pools/pool-a/unblock", headers=cl(rig)).status_code == 403
    response = api.post("/api/v1/pools/pool-a/unblock", headers=adm(rig))
    assert response.status_code == 200
    assert response.json() == {
        "pool": "pool-a",
        "blocked": False,
        "blocked_by": None,
        "blocked_until": None,
    }
    assert "pool-a" not in rig.jobs.active_blocks(clock.now())
    missing = api.post("/api/v1/pools/no-such/unblock", headers=adm(rig))
    assert missing.status_code == 404


def test_unblock_reports_window_that_still_blocks_the_pool(api, rig, clock) -> None:
    rig.jobs.add_block(PoolBlock("pool-a", clock.now() + timedelta(hours=1), "test"), None)
    exhausted = {"pool": "pool-a", "window": "5h", "percent": 100}
    assert api.post("/api/v1/observations", json=exhausted, headers=adm(rig)).status_code == 200

    response = api.post("/api/v1/pools/pool-a/unblock", headers=adm(rig))

    assert response.status_code == 200
    body = response.json()
    assert body["pool"] == "pool-a"
    assert body["blocked"] is True
    assert body["blocked_by"] == "5h"
    assert "blocked_until" in body
    assert "pool-a" not in rig.jobs.active_blocks(clock.now())


def test_snapshot_counts_gateway_jobs(api, rig) -> None:
    run_job(api, rig)
    body = api.get("/api/v1/snapshot", headers=cl(rig)).json()
    [pool] = body["pools"]
    assert pool["via_gateway"] is not None
    assert pool["via_gateway"]["jobs"] == 1
    assert pool["via_gateway"]["tokens"] == 5


def add_classified_job(rig: Rig, job_id: str, tier: str, classifier_tier: str) -> None:
    rig.jobs.create(
        Job(
            id=job_id,
            request=JobRequest("job", rig.plain(), tier=tier),  # type: ignore[arg-type]
            status="completed",
            tier=tier,  # type: ignore[arg-type]
            tier_source="caller",
            repo_root=None,
            classifier_tier=classifier_tier,  # type: ignore[arg-type]
            classifier_confidence=0.8,
            created_at=rig.dispatcher.clock.now(),
        )
    )


def test_snapshot_has_classifier_agreement_for_systemone(make_rig) -> None:
    rig = make_rig(classifier_kind="systemone")
    add_classified_job(rig, "j_match", "cheap", "cheap")
    add_classified_job(rig, "j_mismatch", "strong", "cheap")
    with TestClient(rig.app, base_url=BASE_URL) as api:
        body = api.get("/api/v1/snapshot", headers=cl(rig)).json()
    assert body["classifier"] == {"kind": "systemone", "matched": 1, "compared": 2}


def test_snapshot_has_no_classifier_for_none(api, rig) -> None:
    body = api.get("/api/v1/snapshot", headers=cl(rig)).json()
    assert "classifier" not in body


@pytest.mark.parametrize(("kind", "enabled"), [("systemone", True), ("none", False)])
def test_build_dispatcher_wires_classifier_executor(make_rig, monkeypatch, kind, enabled) -> None:
    import agent_router.classifier as classifier_module

    monkeypatch.setattr(classifier_module, "classify", lambda prompt, settings: ("cheap", 0.8))
    rig = make_rig(classifier_kind=kind)
    with TestClient(rig.app, base_url=BASE_URL):
        assert (rig.dispatcher.classify_executor is not None) is enabled


# --- write jobs in git --------------------------------------------------------------------


def test_write_job_reports_changed_files(api, rig) -> None:
    repo = rig.repo()
    new_file = "новый.txt"  # non-ascii on purpose
    job = run_job(api, rig, scenario=f"write {new_file}", cwd=repo, mode="write")
    assert job["status"] == "completed", job
    assert job["changed_files"] == [new_file]
    assert job["violations"] == []


def test_commit_by_harness_is_a_violation(api, rig) -> None:
    repo = rig.repo()
    job = run_job(api, rig, scenario="commit", cwd=repo, mode="write")
    assert "head_moved" in job["violations"]


def test_write_outside_git_is_rejected(api, rig) -> None:
    response = api.post("/api/v1/jobs", json=rig.body(mode="write"), headers=cl(rig))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "not_a_git_repo"


@pytest.mark.parametrize("mode", ["read", "write"])
def test_git_failure_is_rejected_with_code(api, rig, monkeypatch, mode) -> None:
    repo = rig.repo()

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(["git"], 30)

    monkeypatch.setattr("agent_router.worktree.subprocess.run", fail)

    response = api.post("/api/v1/jobs", json=rig.body(cwd=repo, mode=mode), headers=cl(rig))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "git_unavailable"
    assert rig.jobs.list(limit=10) == []


# --- no dispatcher ------------------------------------------------------------------------


def test_without_dispatcher_tasks_are_503(client, tokens) -> None:
    for method, path in JOB_ROUTES:
        token = tokens.admin if path in ADMIN_ROUTES else tokens.client
        kwargs: dict[str, Any] = {"headers": bearer(token)}
        if path == "/api/v1/jobs" and method == "POST":
            kwargs["json"] = {"prompt": "x", "cwd": "."}
        response = client.request(method, path, **kwargs)
        assert response.status_code == 503, path
        assert response.json()["error"]["code"] == "tasks_disabled"


# --- wait -----------------------------------------------------------------------------------


def test_wait_with_small_timeout_returns_current_state(api, rig) -> None:
    job_id = post(api, rig)
    started = time.monotonic()
    response = api.get(f"/api/v1/jobs/{job_id}/wait?timeout=0.3", headers=cl(rig))
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert 0.25 <= time.monotonic() - started < 5


def test_wait_timeout_is_capped(api, rig, monkeypatch) -> None:
    monkeypatch.setattr(api_module, "WAIT_MAX_S", 0.3)
    job_id = post(api, rig)
    started = time.monotonic()
    response = api.get(f"/api/v1/jobs/{job_id}/wait?timeout=200", headers=cl(rig))
    assert response.json()["status"] == "queued"
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("timeout", ["-1", "nan", "x"])
def test_wait_rejects_bad_timeout(api, rig, timeout) -> None:
    job_id = post(api, rig)
    response = api.get(f"/api/v1/jobs/{job_id}/wait?timeout={timeout}", headers=cl(rig))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "bad_request"


def test_wait_of_finished_job_returns_at_once(api, rig) -> None:
    job_id = run_job(api, rig)["id"]
    started = time.monotonic()
    response = api.get(f"/api/v1/jobs/{job_id}/wait?timeout=60", headers=cl(rig))
    assert response.json()["status"] == "completed"
    assert time.monotonic() - started < 5


def test_wait_returns_on_awaiting_approval(make_rig, monkeypatch) -> None:
    monkeypatch.setattr(api_module, "WAIT_MAX_S", 10.0)
    rig = make_rig(confirm=True)
    with TestClient(rig.app, base_url=BASE_URL) as api:
        job_id = awaiting(api, rig)
        started = time.monotonic()
        response = api.get(f"/api/v1/jobs/{job_id}/wait?timeout=100", headers=cl(rig))
        assert response.json()["status"] == "awaiting_approval"
        assert time.monotonic() - started < 5


async def _in_app(rig: Rig, body) -> Any:
    """Runs the body(client) coroutine inside the app lifespan."""
    async with rig.app.router.lifespan_context(rig.app):
        transport = httpx.ASGITransport(app=rig.app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            return await body(client)


def test_wait_wakes_on_event_not_on_safety_poll(rig, monkeypatch) -> None:
    monkeypatch.setattr(api_module, "WAIT_SAFETY_S", 30.0)
    job_id = rig.dispatcher.submit(rig_request(rig)).id

    async def body(client: httpx.AsyncClient) -> tuple[dict, float]:
        async def cancel_soon() -> None:
            await asyncio.sleep(0.3)
            await asyncio.to_thread(rig.dispatcher.cancel, job_id)

        started = time.monotonic()
        canceller = asyncio.create_task(cancel_soon())
        response = await client.get(f"/api/v1/jobs/{job_id}/wait?timeout=60", headers=cl(rig))
        await canceller
        return response.json(), time.monotonic() - started

    job, elapsed = asyncio.run(_in_app(rig, body))
    assert job["status"] == "cancelled"
    assert elapsed < 5  # without notification the response would take 30 s


def test_wait_does_not_miss_change_during_read(rig, monkeypatch) -> None:
    """The job changes while wait is reading it from the store: the notification is not lost."""
    monkeypatch.setattr(api_module, "WAIT_SAFETY_S", 30.0)
    job_id = rig.dispatcher.submit(rig_request(rig)).id
    real_get = rig.jobs.get
    calls = {"n": 0}

    def get_and_cancel(wanted: str):
        found = real_get(wanted)
        calls["n"] += 1
        if calls["n"] == 1:
            rig.dispatcher.cancel(wanted)  # read queued while the job is already cancelled
            time.sleep(0.2)  # the notification arrives before the wait starts
        return found

    monkeypatch.setattr(rig.jobs, "get", get_and_cancel)

    async def body(client: httpx.AsyncClient) -> tuple[dict, float]:
        started = time.monotonic()
        response = await client.get(f"/api/v1/jobs/{job_id}/wait?timeout=60", headers=cl(rig))
        return response.json(), time.monotonic() - started

    job, elapsed = asyncio.run(_in_app(rig, body))
    assert job["status"] == "cancelled"
    assert elapsed < 5


def test_wait_falls_back_to_safety_poll(make_rig, monkeypatch) -> None:
    """If notifications never arrive, the safety interval re-reads the job."""
    monkeypatch.setattr(AsyncWaiters, "notify", lambda self, event: None)
    monkeypatch.setattr(api_module, "WAIT_SAFETY_S", 0.2)
    rig = make_rig()
    job_id = rig.dispatcher.submit(rig_request(rig)).id

    async def body(client: httpx.AsyncClient) -> dict:
        async def cancel_soon() -> None:
            await asyncio.sleep(0.3)
            await asyncio.to_thread(rig.dispatcher.cancel, job_id)

        canceller = asyncio.create_task(cancel_soon())
        response = await client.get(f"/api/v1/jobs/{job_id}/wait?timeout=20", headers=cl(rig))
        await canceller
        return response.json()

    started = time.monotonic()
    assert asyncio.run(_in_app(rig, body))["status"] == "cancelled"
    assert time.monotonic() - started < 5


def test_client_disconnect_removes_waiter(rig) -> None:
    job_id = rig.dispatcher.submit(rig_request(rig)).id
    waiters = rig.app.state.waiters

    async def body(client: httpx.AsyncClient) -> tuple[int, int]:
        request = asyncio.create_task(
            client.get(f"/api/v1/jobs/{job_id}/wait?timeout=60", headers=cl(rig))
        )
        for _ in range(100):
            await asyncio.sleep(0.02)
            if waiters.watching():
                break
        during = waiters.watching()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await asyncio.sleep(0.05)
        return during, waiters.watching()

    during, after = asyncio.run(_in_app(rig, body))
    assert during == 1
    assert after == 0


def rig_request(rig: Rig, scenario: str = "exit 0"):
    from agent_router.jobs import JobRequest

    return JobRequest(f"{PREFIX} {scenario}", rig.plain(), "read")


# --- worker loop --------------------------------------------------------------------------


class TickStub:
    def __init__(
        self,
        stop: threading.Event,
        fail_first: int = 0,
        stop_after: int = 3,
        bus: EventBus | None = None,
    ) -> None:
        self.stop = stop
        self.calls = 0
        self.fail_first = fail_first
        self.stop_after = stop_after
        self.bus = bus

    def tick(self) -> None:
        self.calls += 1
        if self.bus is not None and self.calls == 1:
            self.bus.publish("job.queued", {"job": "j-1"})  # event published during a tick
        if self.calls >= self.stop_after:
            self.stop.set()
        if self.calls <= self.fail_first:
            raise RuntimeError("tick failure")


def test_worker_loop_survives_tick_errors(clock, caplog) -> None:
    bus = EventBus(clock)
    stop = threading.Event()
    stub = TickStub(stop, fail_first=2, stop_after=3)
    try:
        worker_loop(stub, bus, stop, interval=0.01)  # type: ignore[arg-type]
    finally:
        bus.close()
    assert stub.calls == 3
    assert "tick failure" in caplog.text


def test_worker_loop_wakes_on_bus_event(clock) -> None:
    bus = EventBus(clock)
    stop = threading.Event()
    stub = TickStub(stop, stop_after=10**9)
    thread = threading.Thread(target=worker_loop, args=(stub, bus, stop, 30.0))
    thread.start()
    try:
        time.sleep(0.2)
        assert stub.calls == 1
        started = time.monotonic()
        bus.publish("job.queued", {"job": "j-1"})
        while stub.calls < 2 and time.monotonic() - started < 5:
            time.sleep(0.02)
        assert stub.calls >= 2
        assert time.monotonic() - started < 5  # did not wait for the 30 s interval
    finally:
        stop.set()
        bus.publish("stop")
        thread.join(10)
        bus.close()
    assert not thread.is_alive()


def test_worker_loop_does_not_lose_event_published_during_tick(clock) -> None:
    bus = EventBus(clock)
    stop = threading.Event()
    stub = TickStub(stop, stop_after=10**9, bus=bus)
    thread = threading.Thread(target=worker_loop, args=(stub, bus, stop, 30.0))
    started = time.monotonic()
    thread.start()
    try:
        while stub.calls < 2 and time.monotonic() - started < 5:
            time.sleep(0.02)
        assert stub.calls >= 2  # second tick without waiting for the 30 s interval
    finally:
        stop.set()
        bus.publish("stop")
        thread.join(10)
        bus.close()
    assert not thread.is_alive()


def test_worker_loop_stops_on_event(clock) -> None:
    bus = EventBus(clock)
    stop = threading.Event()
    stub = TickStub(stop, stop_after=10**9)
    thread = threading.Thread(target=worker_loop, args=(stub, bus, stop, 30.0))
    thread.start()
    time.sleep(0.2)
    stop.set()
    bus.publish("tick")  # wakes the bus wait
    thread.join(10)
    bus.close()
    assert not thread.is_alive()


# --- live server --------------------------------------------------------------------------


class LiveGateway:
    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.server = uvicorn.Server(
            uvicorn.Config(rig.app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, name="live-gateway", daemon=True)
        self.stopped = False

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.05)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{self.url}/healthz", timeout=10).status_code == 200

    def stop(self, timeout: float = 60) -> float:
        if self.stopped:
            return 0.0
        self.stopped = True
        started = time.monotonic()
        self.server.should_exit = True
        self.thread.join(timeout)
        return time.monotonic() - started


@pytest.fixture
def live_gateway(make_rig) -> Iterator[LiveGateway]:
    rig = make_rig(background=True)
    gateway = LiveGateway(rig)
    gateway.start()
    try:
        yield gateway
    finally:
        gateway.stop()


def live_post(gateway: LiveGateway, scenario: str) -> str:
    rig = gateway.rig
    response = httpx.post(
        f"{gateway.url}/api/v1/jobs",
        json=rig.body(scenario=scenario),
        headers=cl(rig),
        timeout=10,
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def test_fifty_parallel_waits_on_live_server(live_gateway) -> None:
    rig = live_gateway.rig
    job_id = live_post(live_gateway, "sleep 1")

    async def wait_all() -> tuple[float, list[httpx.Response]]:
        limits = httpx.Limits(max_connections=60)
        async with httpx.AsyncClient(timeout=60, limits=limits) as client:
            url = f"{live_gateway.url}/api/v1/jobs/{job_id}/wait?timeout=60"

            async def health() -> float:
                await asyncio.sleep(0.3)  # waits are already pending
                begin = time.monotonic()
                response = await client.get(f"{live_gateway.url}/healthz")
                assert response.status_code == 200
                return time.monotonic() - begin

            waits = [client.get(url, headers=cl(rig)) for _ in range(50)]
            health_s, *responses = await asyncio.gather(health(), *waits)
            return health_s, responses

    started = time.monotonic()
    health_s, responses = asyncio.run(wait_all())
    assert health_s < 5
    assert [r.status_code for r in responses] == [200] * 50
    assert {r.json()["status"] for r in responses} == {"completed"}
    assert time.monotonic() - started < 30
    assert rig.app.state.waiters.watching() == 0


def test_stop_with_running_job_finishes_attempt(live_gateway, tmp_path, home) -> None:
    rig = live_gateway.rig
    job_id = live_post(live_gateway, "sleep 60")
    deadline = time.monotonic() + 30
    pid = None
    while pid is None and time.monotonic() < deadline:
        attempts = rig.jobs.attempts(job_id)
        pid = attempts[0].pid if attempts and attempts[0].pid else None
        time.sleep(0.05)
    assert pid is not None and pid_alive(pid)

    elapsed = live_gateway.stop()

    assert elapsed < 30
    assert not live_gateway.thread.is_alive()
    reopened = STORE_FACTORIES["sqlite"](tmp_path)  # same file as the store fixture
    try:
        job = open_jobstore(rig.config.storage, home, reopened).get(job_id)
    finally:
        reopened.close()
    assert job is not None
    assert (job.status, job.error_code) == ("failed", "gateway_restarted")
    deadline = time.monotonic() + 10
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not pid_alive(pid)


# --- benchmarks ---------------------------------------------------------------------------


@pytest.mark.bench
def test_bench_jobs_api(live_gateway) -> None:
    """Medians: POST to job.running (outside and inside git); attempt.exited to wait
    answer; git write.

    One warmup run comes first; the write job uses a no-change scenario.
    """
    rig = live_gateway.rig
    marks: dict[tuple[str, str], int] = {}
    rig.bus.subscribe(
        "bench",
        lambda event: marks.setdefault(
            (event.type, str(event.data.get("job"))), time.perf_counter_ns()
        ),
    )
    to_running: list[float] = []
    to_running_git: list[float] = []
    exited_to_wait: list[float] = []
    write_total: list[float] = []
    repo = rig.repo()
    with httpx.Client(timeout=60) as client:

        def run(body: dict) -> dict:
            job_id = client.post(
                f"{live_gateway.url}/api/v1/jobs", json=body, headers=cl(rig)
            ).json()["job_id"]
            return client.get(
                f"{live_gateway.url}/api/v1/jobs/{job_id}/wait?timeout=60", headers=cl(rig)
            ).json()

        run(rig.body("exit 0"))  # warmup
        run(rig.body("exit 0", cwd=repo, mode="write"))
        for n in range(10):
            sent = time.perf_counter_ns()
            response = client.post(
                f"{live_gateway.url}/api/v1/jobs", json=rig.body("exit 0"), headers=cl(rig)
            )
            job_id = response.json()["job_id"]
            client.get(f"{live_gateway.url}/api/v1/jobs/{job_id}/wait?timeout=60", headers=cl(rig))
            answered = time.perf_counter_ns()
            to_running.append((marks[("job.running", job_id)] - sent) / 1e6)
            exited_to_wait.append((answered - marks[("attempt.exited", job_id)]) / 1e6)

            sent = time.perf_counter_ns()  # read job in git: rev-parse cost
            job_id = client.post(
                f"{live_gateway.url}/api/v1/jobs",
                json=rig.body("exit 0", cwd=repo),
                headers=cl(rig),
            ).json()["job_id"]
            client.get(f"{live_gateway.url}/api/v1/jobs/{job_id}/wait?timeout=60", headers=cl(rig))
            to_running_git.append((marks[("job.running", job_id)] - sent) / 1e6)

            if n < 5:  # write job in git with no changes: tree-check cost
                started = time.perf_counter_ns()
                done = run(rig.body("exit 0", cwd=repo, mode="write"))
                assert done["status"] == "completed", done
                write_total.append((time.perf_counter_ns() - started) / 1e6)
    for name, values in (
        ("post_to_running_ms", to_running),
        ("post_to_running_git_ms", to_running_git),
        ("exited_to_wait_ms", exited_to_wait),
        ("write_job_total_ms", write_total),
    ):
        assert all(math.isfinite(value) for value in values)
        print(f"bench {name} median={statistics.median(values):.1f}")


# --- serve command ------------------------------------------------------------------------


def test_serve_wires_dispatcher(home, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from agent_router import cli
    from agent_router.auth import ensure_tokens

    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    captured: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw))
    runner = CliRunner()
    assert runner.invoke(cli.app, ["init"]).exit_code == 0
    assert runner.invoke(cli.app, ["serve", "--port", "8799"]).exit_code == 0
    assert captured["port"] == 8799
    tokens = ensure_tokens(home)
    with TestClient(captured["app"], base_url=BASE_URL) as api:
        response = api.get("/api/v1/jobs", headers=bearer(tokens.client))
    assert response.status_code == 200
    assert response.json() == {"jobs": []}


def test_serve_passes_no_fixed_tz(home, monkeypatch: pytest.MonkeyPatch) -> None:
    """None means the system zone at each moment, so a DST switch shows the right offset."""
    from typer.testing import CliRunner

    from agent_router import api, cli, service

    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    seen: dict[str, Any] = {}
    real_app, real_dispatcher = api.create_app, service.build_dispatcher

    def app_spy(*args, **kwargs):
        seen["app"] = kwargs.get("tz", "missing")
        return real_app(*args, **kwargs)

    def dispatcher_spy(*args, **kwargs):
        seen["dispatcher"] = args[6] if len(args) > 6 else kwargs.get("tz", "missing")
        return real_dispatcher(*args, **kwargs)

    monkeypatch.setattr(api, "create_app", app_spy)
    monkeypatch.setattr(service, "build_dispatcher", dispatcher_spy)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["init"]).exit_code == 0
    assert runner.invoke(cli.app, ["serve"]).exit_code == 0
    assert seen == {"app": None, "dispatcher": None}


def test_serve_closes_store_when_jobstore_fails(home, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from agent_router import cli, cli_gateway

    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    closed: list[bool] = []

    class Store:
        def close(self) -> None:
            closed.append(True)

    def broken(*args: Any) -> None:
        raise OSError("migration failed")

    monkeypatch.setattr(cli_gateway, "open_store", lambda storage, home: Store())
    monkeypatch.setattr(cli_gateway, "open_jobstore", broken)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["init"]).exit_code == 0
    result = runner.invoke(cli.app, ["serve"])
    assert result.exit_code == 1
    assert "failed to open the store" in result.output
    assert closed == [True]


def test_wait_releases_waiter_when_client_drops_connection(live_gateway) -> None:
    """A real TCP disconnect: uvicorn does not cancel the handler,
    so the wait must go away on its own.
    """
    import socket

    rig = live_gateway.rig
    job_id = live_post(live_gateway, "sleep 60")
    waiters = rig.app.state.waiters
    host, port = live_gateway.url.removeprefix("http://").split(":")
    token = cl(rig)["Authorization"]
    request = (
        f"GET /api/v1/jobs/{job_id}/wait?timeout=60 HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\nAuthorization: {token}\r\n\r\n"
    )
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(request.encode())
        deadline = time.monotonic() + 5
        while waiters.watching() == 0:
            assert time.monotonic() < deadline, "wait was not registered"
            time.sleep(0.02)
    deadline = time.monotonic() + 5
    while waiters.watching():
        assert time.monotonic() < deadline, "wait remained after the disconnect"
        time.sleep(0.05)
    rig.dispatcher.cancel(job_id)


def test_failed_startup_still_releases_resources(make_rig, monkeypatch) -> None:
    rig = make_rig(background=True)
    closed: list[str] = []

    def broken_recover() -> None:
        raise RuntimeError("recover failed")

    real_close = rig.jobs.close

    def recording_close() -> None:
        closed.append("jobs")
        real_close()

    monkeypatch.setattr(rig.dispatcher, "recover", broken_recover)
    monkeypatch.setattr(rig.jobs, "close", recording_close)
    with pytest.raises(RuntimeError), TestClient(rig.app, base_url=BASE_URL):
        pass
    assert closed == ["jobs"]
    alive = [x for x in threading.enumerate() if x.name == "counter-sampler" and x.is_alive()]
    assert not alive


def test_app_stop_is_bounded_with_stuck_attempt(make_rig) -> None:
    rig = make_rig()
    started = threading.Event()
    release = threading.Event()

    class StuckRunner:
        def run(
            self,
            inv,
            cwd,
            timeout_s,
            log_dir,
            cancel,
            is_final_line=None,
            is_progress_line=None,
            exit_grace_s=60,
            on_start=None,
            idle_timeout_s=None,
            quiet_while_working=False,
        ) -> ProcessResult:
            log_dir.mkdir(parents=True, exist_ok=True)
            stdout = log_dir / "stdout.log"
            stderr = log_dir / "stderr.log"
            stdout.write_text("", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            started.set()
            if not release.wait(30):
                raise TimeoutError("stub runner was not released")
            return ProcessResult(None, stdout, stderr, 0, cancelled=True)

    rig.dispatcher.runner = StuckRunner()
    rig.dispatcher.stop_wait_s = 0.2
    future = None
    try:
        with TestClient(rig.app, base_url=BASE_URL) as api:
            job_id = post(api, rig)
            rig.dispatcher.tick()
            assert started.wait(5)
            future = rig.dispatcher._active[job_id].future
            assert future is not None
            stopped = time.monotonic()
        assert time.monotonic() - stopped < 5
    finally:
        release.set()
    assert future is not None
    future.result(timeout=5)


@pytest.mark.parametrize(("args", "hint"), [(["--port", "8799"], True), ([], False)])
def test_serve_names_the_client_url_for_another_port(
    home, monkeypatch: pytest.MonkeyPatch, args: list[str], hint: bool
) -> None:
    from typer.testing import CliRunner

    from agent_router import cli

    monkeypatch.setenv("AGENT_ROUTER_HOME", str(home))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["init"]).exit_code == 0

    result = runner.invoke(cli.app, ["serve", *args])

    assert result.exit_code == 0
    assert ("AGENT_ROUTER_URL=http://127.0.0.1:8799" in result.output) is hint
