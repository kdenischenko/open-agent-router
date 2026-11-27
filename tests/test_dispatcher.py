"""Dispatcher: tick() step, attempts, confirmations, limits, cancel, restart."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import hashlib
import json
import logging
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest

from agent_router import dispatcher as dispatcher_module
from agent_router import worktree
from agent_router.adapters import RunOutcome
from agent_router.clock import format_local
from agent_router.config import ClassifierModel, ClassifierSettings
from agent_router.dispatcher import Dispatcher
from agent_router.events import Event, EventBus
from agent_router.jobs import Attempt, Job, JobRequest, RequestRejected, Vote, WrongState
from agent_router.policy import PoolBlock
from agent_router.registry import HarnessSpec
from agent_router.runner import Invocation, ProcessQueryError, ProcessResult, ResolvedExecutable
from agent_router.store import Observation
from helpers import break_index, git, make_empty_git_repo, make_git_repo, requires_git, write_config

# --- fakes ----------------------------------------------------------------------------


@dataclass
class Step:
    """One run of the fake runner: what the harness "printed" and what happened in the directory."""

    stdout: str = "ok:готово"  # non-ascii on purpose
    exit_code: int | None = 0
    timed_out: bool = False
    idle: bool = False
    action: Callable[[Path], None] | None = None
    wait_cancel: bool = False
    raises: Exception | None = None
    raises_after: Exception | None = None  # runner failure after launch and action
    cancels: bool = False  # cancel arrived mid-run (issued by gateway shutdown)
    survived: bool = False  # process outlived the gateway shutdown
    release: threading.Event | None = None  # ignore cancel until the test releases it
    pid: int = 4242
    started: threading.Event = field(default_factory=threading.Event)
    last_signal: str | None = None  # the signal that last touched the liveness
    last_signal_age_s: float | None = None  # how long ago that was


@dataclass
class Call:
    inv: Invocation
    cwd: Path
    timeout_s: float
    cancel: threading.Event
    idle_timeout_s: float | None = None
    quiet_while_working: bool = False


class ScriptRunner:
    id = "fake"

    def __init__(self) -> None:
        self.steps: deque[Step] = deque()
        self.calls: list[Call] = []

    def add(self, *steps: Step) -> None:
        self.steps.extend(steps)

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
        on_liveness=None,
    ) -> ProcessResult:
        step = self.steps.popleft() if self.steps else Step()
        self.calls.append(Call(inv, cwd, timeout_s, cancel, idle_timeout_s, quiet_while_working))
        log_dir.mkdir(parents=True, exist_ok=True)
        if cancel.is_set():
            (log_dir / "stdout.log").write_text("", encoding="utf-8")
            (log_dir / "stderr.log").write_text("", encoding="utf-8")
            return ProcessResult(
                exit_code=None,
                stdout_path=log_dir / "stdout.log",
                stderr_path=log_dir / "stderr.log",
                duration_s=0,
                cancelled=True,
            )
        if step.raises is not None:
            raise step.raises
        if on_start is not None:
            on_start(step.pid)
        step.started.set()
        if on_liveness is not None:
            from agent_router.liveness import Liveness

            liveness = Liveness(time.monotonic() - 1.0, max(0.0, float(timeout_s)), idle_timeout_s)
            if step.last_signal is not None:
                liveness.touch(time.monotonic(), step.last_signal)
            on_liveness(liveness)
        if step.action is not None:
            step.action(cwd)
        if step.release is not None and not step.release.wait(30):
            raise TimeoutError("fake runner was not released")
        if step.raises_after is not None:
            raise step.raises_after
        if step.cancels:
            cancel.set()
        if step.wait_cancel:
            cancel.wait(30)
        (log_dir / "stdout.log").write_text(step.stdout, encoding="utf-8")
        (log_dir / "stderr.log").write_text("", encoding="utf-8")
        return ProcessResult(
            exit_code=step.exit_code,
            stdout_path=log_dir / "stdout.log",
            stderr_path=log_dir / "stderr.log",
            duration_s=1.5,
            timed_out=step.timed_out,
            idle=step.idle,
            cancelled=cancel.is_set(),
            survived=step.survived,
            last_signal=step.last_signal,
            last_signal_age_s=step.last_signal_age_s,
        )


class ScriptAdapter:
    """Harness output: `ok:<text>`, `limit`, `limit@<ISO time>`, `error:<text>`, `nomodel`,
    `crash`.

    `nomodel` — the route's model is unavailable (`provider.no-route`); `crash` — the adapter
    could not parse the output and raised an exception.
    """

    name = "fake"

    reports_progress = True

    def build(self, text, mode, model, exe, prompt_file) -> Invocation:
        return Invocation((str(exe.path), mode, model))

    def parse(self, stdout: str, stderr: str, exit_code: int | None) -> RunOutcome:
        if stdout.startswith("crash"):
            raise ValueError("output not parsed")
        if stdout.startswith("ok:"):
            return RunOutcome("ok", text=stdout[3:], tokens_in=10, tokens_out=5)
        if stdout.startswith("limit"):
            from datetime import datetime

            stamp = stdout.partition("@")[2]
            reset = datetime.fromisoformat(stamp) if stamp else None
            return RunOutcome("limit", limit_reset_at=reset, error="usage limit reached")
        if stdout.startswith("nomodel"):
            return RunOutcome(
                "error",
                error="provider.no-route: Model unavailable: fake/model-x",
                model_unavailable=True,
            )
        return RunOutcome("error", error=stdout.partition(":")[2] or "harness failure")

    def is_final_line(self, line: str) -> bool:
        return False


class ImmediateExecutor(concurrent.futures.Executor):
    """Runs work immediately in the calling thread."""

    def submit(self, fn, /, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return future


class ManualExecutor(concurrent.futures.Executor):
    """Queues work; runs it when the test asks."""

    def __init__(self) -> None:
        self.pending: deque = deque()

    def submit(self, fn, /, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()
        self.pending.append((future, fn, args, kwargs))
        return future

    def run_all(self) -> None:
        while self.pending:
            future, fn, args, kwargs = self.pending.popleft()
            try:
                future.set_result(fn(*args, **kwargs))
            except Exception as exc:
                future.set_exception(exc)


class RecordingBus(EventBus):
    """Bus that remembers published events and calls a hook on the publisher's thread."""

    def __init__(self, clock) -> None:
        super().__init__(clock)
        self.seen: list[tuple[str, dict]] = []
        self.hook: Callable[[str, dict], None] | None = None

    def publish(self, type, data=None) -> Event:
        event = super().publish(type, data)
        self.seen.append((type, dict(data or {})))
        if self.hook is not None:
            self.hook(type, dict(data or {}))
        return event

    def types(self, prefix: str = "") -> list[str]:
        return [kind for kind, _ in self.seen if kind.startswith(prefix)]

    def data(self, kind: str) -> list[dict]:
        return [data for seen, data in self.seen if seen == kind]


def route(
    id: str,
    pool: str,
    quality: int,
    tiers=("standard",),
    modes=("read", "write"),
    confirm: bool = False,
    backend: str = "fake",
    model_key: str | None = None,
) -> dict:
    return {
        "id": id,
        "pool": pool,
        "quality": quality,
        "tiers": list(tiers),
        "modes": list(modes),
        "confirm": confirm,
        "backend": backend,
        "model_key": model_key,
    }


ROUTES = (
    route("a/main", "pool-a", 5, tiers=("standard", "strong")),
    route("b/main", "pool-b", 4, tiers=("cheap", "standard")),
    route("c/read", "pool-c", 3, modes=("read",)),
)
CONFIRM_ROUTES = (
    route("a/main", "pool-a", 5, confirm=True),
    route("b/main", "pool-b", 4),
)


def config_text(root: Path, routes, tasks: dict) -> str:
    settings = {"max_parallel": 3, "max_queued": 200, "max_attempts": 3, **tasks}
    lines = ["[tasks]", f"allowed_roots = ['{root.as_posix()}']"]
    lines += [f"{key} = {value}" for key, value in settings.items()]
    pools = sorted({item["pool"] for item in routes})
    lines += ["", "[callers]"]
    lines += [f'agent-{pool[-1]} = "{pool}"' for pool in pools if pool in ("pool-a", "pool-b")]
    lines.append("")
    for pool in pools:
        lines += ["[[pools]]", f'id = "{pool}"', f'title = "{pool}"', 'unit = "percent"']
        if pool == "pool-a":
            lines += ["reserve = 0.15", 'reserve_tier = "strong"']
        lines += [
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
        ]
    for item in routes:
        lines += [
            "[[routes]]",
            f'id = "{item["id"]}"',
            f'pool = "{item["pool"]}"',
            f'backend = "{item["backend"]}"',
            'model = "model-x"',
        ]
        if item.get("model_key") is not None:
            lines.append(f'model_key = "{item["model_key"]}"')
        lines += [
            f"tiers = {json.dumps(item['tiers'])}",
            f"quality = {item['quality']}",
            f"modes = {json.dumps(item['modes'])}",
            f"confirm = {'true' if item['confirm'] else 'false'}",
            "enabled = true",
            "",
        ]
    return "\n".join(lines)


class Rig:
    """Dispatcher with a fake runner, adapter, and executor."""

    def __init__(
        self,
        tmp_path,
        home,
        store,
        jobstore,
        clock,
        executor,
        routes,
        *,
        votes=None,
        **tasks,
    ) -> None:
        self.root = tmp_path / "рабочие каталоги"  # non-ascii on purpose
        self.root.mkdir(exist_ok=True)
        self.home = home
        self.config = write_config(home, config_text(self.root, routes, tasks))
        self.store = store
        self.jobs = jobstore
        self.clock = clock
        self.bus = RecordingBus(clock)
        self.runner = ScriptRunner()
        self.executor = executor
        self.votes = votes
        self.missing: set[str] = set()
        self.images: dict[int, str] = {}
        self.image_failures: set[int] = set()  # querying these PIDs raises ProcessQueryError
        self.starts: dict[int, float] = {}
        self.killed: list[int] = []
        self.on_kill: Callable[[int], None] | None = None
        self.dispatcher = self._dispatcher()

    def _dispatcher(self) -> Dispatcher:
        return Dispatcher(
            self.config,
            self.store,
            self.jobs,
            self.bus,
            self.clock,
            self.runner,
            {
                "fake": HarnessSpec("fake", ScriptAdapter(), ("fake",), "journal"),
                "fake2": HarnessSpec("fake2", ScriptAdapter(), ("fake2",), "journal"),
            },
            self.executor,
            self.home,
            resolve=self.resolve,
            kill=self.kill,
            image=self.image,
            started=self.starts.get,
            votes=self.votes,
        )

    def restart(self) -> Dispatcher:
        """Gateway crashed and came back: a new dispatcher on the same stores, bus, and clock."""
        self.dispatcher = self._dispatcher()
        return self.dispatcher

    def resolve(self, name: str) -> ResolvedExecutable:
        if name in self.missing:
            return ResolvedExecutable(None, False)
        return ResolvedExecutable(Path("/opt/harness") / name, False)

    def image(self, pid: int) -> str | None:
        if pid in self.image_failures:
            raise ProcessQueryError("query failed")
        return self.images.get(pid)

    def kill(self, pid: int) -> None:
        self.killed.append(pid)
        if self.on_kill is not None:
            self.on_kill(pid)

    def plain(self, name: str = "каталог задачи") -> Path:  # non-ascii on purpose
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def repo(self, name: str = "репозиторий") -> Path:  # non-ascii on purpose
        return make_git_repo(self.root / name)

    def submit(self, cwd: Path, mode: str = "read", **fields) -> Job:
        prompt = "секретный промпт задачи"  # non-ascii on purpose
        request = JobRequest(prompt, cwd, mode, **fields)  # type: ignore[arg-type]
        return self.dispatcher.submit(request)

    def tick(self, times: int = 1) -> None:
        for _ in range(times):
            self.dispatcher.tick()

    def job(self, job: Job) -> Job:
        found = self.jobs.get(job.id)
        assert found is not None
        return found

    def status(self, job: Job) -> str:
        return self.job(job).status

    def attempts(self, job: Job) -> list[Attempt]:
        return self.jobs.attempts(job.id)

    def block_until(self, pool: str):
        return self.jobs.active_blocks(self.clock.now())[pool].until

    def close(self) -> None:
        self.bus.close()


class FlakyJobs:
    """Wrapper over the job store: named methods fail for named jobs.

    `fail("update", job_id)` — the method raises `sqlite3.OperationalError` for that job until
    the failure is cleared (`heal()`). `fail_all = True` — any call fails. `calls` — method
    names invoked while `fail_all` was on; `failures` — (method, job id) pairs of failed calls.
    """

    def __init__(self, real) -> None:
        self._real = real
        self._failing: dict[str, set[str]] = {}
        self.fail_all = False
        self.calls: list[str] = []
        self.failures: list[tuple[str, str]] = []

    def fail(self, method: str, *job_ids: str) -> None:
        self._failing.setdefault(method, set()).update(job_ids)

    def heal(self) -> None:
        self._failing.clear()

    def __getattr__(self, name: str):
        target = getattr(self._real, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            if self.fail_all:
                self.calls.append(name)
                raise sqlite3.OperationalError("database is locked")
            subject = args[0] if args else None
            job_id = getattr(subject, "job_id", None) or getattr(subject, "id", None) or subject
            if isinstance(job_id, str) and job_id in self._failing.get(name, ()):
                self.failures.append((name, job_id))
                raise sqlite3.OperationalError("database is locked")
            return target(*args, **kwargs)

        return call


def flaky_jobs(rig: Rig) -> FlakyJobs:
    """Inserts the wrapper between the dispatcher and the job store (survives `rig.restart()`)."""
    flaky = FlakyJobs(rig.jobs)
    rig.jobs = flaky
    rig.dispatcher.jobs = flaky
    return flaky


@pytest.fixture
def make_rig(tmp_path, home, store, jobstore, clock, register_harness):
    register_harness("fake", HarnessSpec("fake", ScriptAdapter(), ("fake",), "journal"))
    register_harness("fake2", HarnessSpec("fake2", ScriptAdapter(), ("fake2",), "journal"))
    rigs: list[Rig] = []

    def build(executor=None, routes=ROUTES, **tasks) -> Rig:
        rig = Rig(
            tmp_path, home, store, jobstore, clock, executor or ImmediateExecutor(), routes, **tasks
        )
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        rig.close()


class FakeVotes:
    """Vote queue for the test: accumulates jobs, returns votes on the test's command."""

    primary = "systemone"

    def __init__(self, tier: str | None = "cheap", confidence: float | None = 0.8) -> None:
        self.tier = tier
        self.confidence = confidence
        self.submitted: list[tuple[str, str]] = []
        self.ready: list[tuple[str, Vote]] = []
        self.error: str | None = None

    def submit(self, job_id: str, prompt: str, created_at) -> None:
        self.submitted.append((job_id, prompt))

    def run_all(self) -> None:
        for job_id, _ in self.submitted:
            tier, confidence = self.tier, self.confidence
            primary = Vote("systemone", "systemone-model", tier, confidence, 120, self.error)  # type: ignore[arg-type]
            self.ready.append((job_id, primary))
            self.ready.append((job_id, Vote("lfm3", "lfm3-model", "strong", 0.9, 300, None)))
        self.submitted.clear()

    def drain(self) -> list[tuple[str, Vote]]:
        ready, self.ready = self.ready, []
        return ready

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        pass


@requires_git
def test_classifier_result_is_stored_on_next_tick(make_rig) -> None:
    votes = FakeVotes()
    rig = make_rig(votes=votes)
    job = rig.submit(rig.plain())
    stored = rig.job(job)
    assert stored.classifier_tier is None and stored.classifier_confidence is None
    prompt = "секретный промпт задачи"  # non-ascii on purpose
    assert votes.submitted == [(job.id, prompt)]

    votes.run_all()
    stored = rig.job(job)
    assert stored.classifier_tier is None and stored.classifier_confidence is None
    rig.tick()

    stored = rig.job(job)
    assert stored.classifier_tier == "cheap" and stored.classifier_confidence == 0.8
    assert [v.classifier_id for v in rig.jobs.votes(job.id)] == ["systemone", "lfm3"]
    voted = [data for kind, data in rig.bus.seen if kind == "classifier.voted"]
    assert voted[0] == {
        "job_id": job.id,
        "classifier_id": "systemone",
        "tier": "cheap",
        "confidence": 0.8,
        "latency_ms": 120,
        "error": None,
    }
    assert len(voted) == 2
    baseline = make_rig()
    baseline.submit(baseline.plain())
    baseline.tick()
    assert rig.bus.types("job.") == baseline.bus.types("job.")


@requires_git
def test_classifier_does_not_delay_or_change_route(make_rig) -> None:
    votes = FakeVotes(tier="strong", confidence=0.9)
    rig = make_rig(votes=votes)
    job = rig.submit(rig.plain(), tier="cheap")
    baseline = make_rig()
    baseline_job = baseline.submit(baseline.plain(), tier="cheap")

    rig.tick()
    baseline.tick()
    assert rig.status(job) == "running" == baseline.status(baseline_job)
    assert rig.job(job).route == baseline.job(baseline_job).route == "b/main"
    assert rig.job(job).classifier_tier is None

    votes.run_all()
    rig.tick()
    baseline.tick()
    current = rig.job(job)
    assert current.tier == "cheap" and current.tier_source == "caller"
    assert current.route == baseline.job(baseline_job).route == "b/main"
    assert current.classifier_tier == "strong" and current.classifier_confidence == 0.9


@requires_git
def test_classifier_failure_leaves_fields_empty_and_keeps_vote(make_rig) -> None:
    votes = FakeVotes(tier=None, confidence=None)
    votes.error = "timeout"
    rig = make_rig(votes=votes)
    job = rig.submit(rig.plain())
    votes.run_all()
    rig.tick()
    current = rig.job(job)
    assert current.classifier_tier is None and current.classifier_confidence is None
    assert rig.jobs.votes(job.id)[0].error == "timeout"


@requires_git
def test_secondary_vote_does_not_touch_job_fields(make_rig) -> None:
    votes = FakeVotes()
    rig = make_rig(votes=votes)
    job = rig.submit(rig.plain())
    votes.run_all()
    votes.ready = [item for item in votes.ready if item[1].classifier_id == "lfm3"]
    rig.tick()
    current = rig.job(job)
    assert current.classifier_tier is None
    assert [v.classifier_id for v in rig.jobs.votes(job.id)] == ["lfm3"]


@requires_git
def test_classifier_result_for_finished_job_keeps_status(make_rig) -> None:
    votes = FakeVotes()
    rig = make_rig(votes=votes)
    job = rig.submit(rig.plain())
    rig.tick(2)
    assert rig.status(job) == "completed"
    events = rig.bus.types("job.")

    votes.run_all()
    rig.tick()

    current = rig.job(job)
    assert current.status == "completed"
    assert current.classifier_tier == "cheap" and current.classifier_confidence == 0.8
    assert rig.bus.types("job.") == events


@requires_git
def test_classifier_store_failure_does_not_break_tick(make_rig, monkeypatch) -> None:
    votes = FakeVotes()
    rig = make_rig(votes=votes, max_parallel=1)
    first = rig.submit(rig.plain("первая"))  # non-ascii on purpose
    rig.tick(2)
    assert rig.status(first) == "completed"
    second = rig.submit(rig.plain("вторая"))  # non-ascii on purpose
    votes.run_all()
    real_update = rig.jobs.update

    def broken(job: Job) -> None:
        if job.classifier_tier is not None:
            raise RuntimeError("store unavailable")
        real_update(job)

    def broken_vote(*args, **kwargs) -> None:
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(rig.jobs, "update", broken)
    monkeypatch.setattr(rig.jobs, "add_vote", broken_vote)
    rig.tick()
    assert rig.status(second) == "running"


@requires_git
def test_classifier_off_by_default(make_rig) -> None:
    rig = make_rig()
    job = rig.submit(rig.plain())
    rig.tick()
    assert rig.status(job) == "running"
    assert rig.dispatcher.classify_executor is None


@requires_git
def test_classifier_submit_failure_does_not_reject_job(make_rig) -> None:
    class Broken(FakeVotes):
        def submit(self, job_id: str, prompt: str, created_at) -> None:
            raise RuntimeError("queue broken")

    rig = make_rig(votes=Broken())
    job = rig.submit(rig.plain())
    rig.tick()
    assert rig.status(job) == "running"


# --- active mode -----------------------------------------------------------------------


def active_rig(make_rig, votes, *, routes=ROUTES, mode: str = "active", **classifier) -> Rig:
    """Rig with the systemone classifier (timeout_s = 3, default wait_s 3.5 s) in the given mode."""
    rig = make_rig(votes=votes, routes=routes)
    settings = ClassifierSettings(kind="systemone", timeout_s=3, mode=mode, **classifier)  # type: ignore[arg-type]
    rig.config = dataclasses.replace(rig.config, classifier=settings)
    rig.restart()
    return rig


@requires_git
def test_active_uses_classifier_tier(make_rig) -> None:
    votes = FakeVotes(tier="cheap", confidence=0.8)
    rig = active_rig(make_rig, votes)
    job = rig.submit(rig.plain())
    pending = rig.job(job)
    assert pending.tier_pending is True
    assert (pending.tier, pending.tier_source) == ("standard", "default")

    rig.tick()
    assert rig.status(job) == "queued"  # no primary vote yet, deadline not reached
    assert rig.attempts(job) == []

    votes.run_all()
    rig.tick()
    current = rig.job(job)
    assert current.status == "running"
    assert (current.tier, current.tier_source) == ("cheap", "classifier")
    assert current.tier_pending is False
    assert current.route == "b/main"  # only b/main has cheap
    [attempt] = rig.attempts(job)
    assert attempt.decision is not None
    assert (attempt.decision["tier"], attempt.decision["tier_source"]) == ("cheap", "classifier")


@requires_git
def test_active_keeps_caller_tier(make_rig) -> None:
    votes = FakeVotes(tier="cheap", confidence=0.95)
    rig = active_rig(make_rig, votes)
    job = rig.submit(rig.plain(), tier="strong")
    assert rig.job(job).tier_pending is False
    rig.tick()  # no vote needed: the caller set the tier
    current = rig.job(job)
    assert current.status == "running"
    assert (current.tier, current.tier_source) == ("strong", "caller")
    prompt = "секретный промпт задачи"  # non-ascii on purpose
    assert votes.submitted == [(job.id, prompt)]  # shadow vote still collected

    votes.run_all()
    rig.tick()
    current = rig.job(job)
    assert (current.tier, current.tier_source) == ("strong", "caller")
    assert current.classifier_tier == "cheap"


@requires_git
def test_active_low_confidence_default(make_rig) -> None:
    votes = FakeVotes(tier="cheap", confidence=0.59)  # secondary lfm3 is confident, not decisive
    rig = active_rig(make_rig, votes)
    job = rig.submit(rig.plain())
    votes.run_all()
    rig.tick()
    current = rig.job(job)
    assert current.status == "running"  # primary vote arrived: nothing left to wait for
    assert (current.tier, current.tier_source) == ("standard", "default")
    assert current.tier_pending is False
    assert current.classifier_tier == "cheap" and current.classifier_confidence == 0.59


@requires_git
def test_active_primary_error_starts_at_once_default(make_rig) -> None:
    votes = FakeVotes(tier=None, confidence=None)
    votes.error = "timeout"
    rig = active_rig(make_rig, votes)
    job = rig.submit(rig.plain())
    rig.tick()
    assert rig.status(job) == "queued"
    votes.run_all()
    rig.tick()
    current = rig.job(job)
    assert current.status == "running"  # primary error came in: no deadline wait
    assert (current.tier, current.tier_source) == ("standard", "default")
    assert current.tier_pending is False


@requires_git
def test_active_model_threshold_overrides_global(make_rig) -> None:
    votes = FakeVotes(tier="cheap", confidence=0.8)
    model = ClassifierModel(
        id="systemone",
        kind="systemone",
        url="http://127.0.0.1:11434",
        model="systemone-model",
        timeout_s=3,
        min_confidence=0.9,
    )
    rig = active_rig(make_rig, votes, primary="systemone", models=(model,))
    job = rig.submit(rig.plain())
    rig.tick()
    assert rig.status(job) == "queued"
    votes.run_all()
    rig.tick()
    current = rig.job(job)
    assert current.status == "running"
    assert (current.tier, current.tier_source) == ("standard", "default")
    assert current.tier_pending is False


@requires_git
def test_active_write_floor(make_rig) -> None:
    votes = FakeVotes(tier="cheap", confidence=0.9)
    rig = active_rig(make_rig, votes)
    job = rig.submit(rig.repo(), mode="write")
    votes.run_all()
    rig.tick()
    current = rig.job(job)
    assert current.status == "running"
    assert (current.tier, current.tier_source) == ("standard", "classifier")


@requires_git
def test_active_ollama_down_starts_after_wait_s(make_rig) -> None:
    votes = FakeVotes()  # votes never arrive: run_all is not called
    rig = active_rig(make_rig, votes)
    job = rig.submit(rig.plain("ждёт голос"))  # non-ascii on purpose
    other = rig.submit(rig.plain("уровень задан"), tier="cheap")  # non-ascii on purpose

    rig.tick()
    assert rig.status(job) == "queued"
    assert rig.status(other) == "running"  # waiting for a vote does not hold the queue

    rig.clock.advance(timedelta(seconds=3))
    rig.tick()
    assert rig.status(job) == "queued"  # 3 s < wait_s = primary timeout_s + 0.5

    rig.clock.advance(timedelta(seconds=0.6))
    rig.tick()
    current = rig.job(job)
    assert current.status == "running"
    assert (current.tier, current.tier_source) == ("standard", "default")
    assert current.tier_pending is False

    late = Vote("systemone", "systemone-model", "strong", 0.99, 5000, None)
    votes.ready.append((job.id, late))  # a late vote no longer changes the tier
    rig.tick()
    current = rig.job(job)
    assert (current.tier, current.tier_source) == ("standard", "default")
    assert current.classifier_tier == "strong"


@requires_git
def test_active_explicit_wait_s(make_rig) -> None:
    rig = active_rig(make_rig, FakeVotes(), wait_s=10)
    job = rig.submit(rig.plain())
    rig.clock.advance(timedelta(seconds=9))
    rig.tick()
    assert rig.status(job) == "queued"
    rig.clock.advance(timedelta(seconds=1))
    rig.tick()
    assert rig.status(job) == "running"


@requires_git
def test_active_pending_does_not_block_on_future(make_rig, monkeypatch) -> None:
    """Vote waiting is a loop-pass check: tick neither waits nor sleeps."""
    votes = FakeVotes()
    rig = active_rig(make_rig, votes)
    job = rig.submit(rig.plain())

    def no_sleep(seconds: float) -> None:
        raise AssertionError("tick must not sleep")

    monkeypatch.setattr(dispatcher_module.time, "sleep", no_sleep, raising=False)
    started = time.monotonic()
    rig.tick(5)
    assert time.monotonic() - started < 2.0
    assert rig.status(job) == "queued"


@requires_git
def test_retry_after_limit_keeps_tier(make_rig) -> None:
    routes = (
        route("a/main", "pool-a", 5, tiers=("strong",)),
        route("b/main", "pool-b", 4, tiers=("strong",)),
    )
    votes = FakeVotes(tier="strong", confidence=0.9)
    rig = active_rig(make_rig, votes, routes=routes)
    rig.runner.add(Step("limit"))
    job = rig.submit(rig.plain())
    votes.run_all()
    rig.tick()
    assert rig.job(job).route == "a/main"

    late = Vote("systemone", "systemone-model", "cheap", 0.99, 100, None)
    votes.ready.append((job.id, late))  # a repeated vote does not re-review the tier
    rig.tick()
    current = rig.job(job)
    assert current.status == "running"
    assert (current.tier, current.tier_source) == ("strong", "classifier")
    first, second = rig.attempts(job)
    assert (first.route, second.route) == ("a/main", "b/main")
    for attempt in (first, second):
        assert attempt.decision is not None
        assert attempt.decision["tier"] == "strong"
        assert attempt.decision["tier_source"] == "classifier"


@requires_git
def test_pending_tier_after_restart_resolved(make_rig) -> None:
    rig = active_rig(make_rig, FakeVotes())
    job = rig.submit(rig.plain())
    rig.tick()
    assert rig.status(job) == "queued"

    rig.votes = FakeVotes()  # old queue's votes died with the process
    rig.restart()
    rig.dispatcher.recover()
    rig.tick()
    current = rig.job(job)
    assert current.status == "queued" and current.tier_pending is True

    rig.clock.advance(timedelta(seconds=3.6))
    rig.tick()
    current = rig.job(job)
    assert current.status == "running"
    assert (current.tier, current.tier_source) == ("standard", "default")
    assert current.tier_pending is False


@requires_git
def test_shadow_mode_unchanged(make_rig) -> None:
    votes = FakeVotes(tier="strong", confidence=0.99)
    rig = active_rig(make_rig, votes, mode="shadow")
    job = rig.submit(rig.plain())
    assert rig.job(job).tier_pending is False
    rig.tick()
    current = rig.job(job)
    assert current.status == "running"  # in shadow mode no vote is awaited
    assert (current.tier, current.tier_source) == ("standard", "default")

    votes.run_all()
    rig.tick()
    current = rig.job(job)
    assert (current.tier, current.tier_source) == ("standard", "default")
    assert current.classifier_tier == "strong"


def new_file(name: str = "новый файл.txt") -> Callable[[Path], None]:  # non-ascii on purpose
    def action(cwd: Path) -> None:
        (cwd / name).write_text("изменение\n", encoding="utf-8")  # non-ascii on purpose

    return action


def commit_file(name: str = "закоммичен.txt") -> Callable[[Path], None]:  # non-ascii on purpose
    def action(cwd: Path) -> None:
        (cwd / name).write_text("коммит\n", encoding="utf-8")  # non-ascii on purpose
        git(cwd, "add", name)
        git(cwd, "commit", "-q", "-m", "work")

    return action


# --- simple path ------------------------------------------------------------------------


@requires_git
def test_queued_running_completed(make_rig) -> None:
    rig = make_rig()
    job = rig.submit(rig.plain())
    assert job.status == "queued" and rig.status(job) == "queued"
    rig.tick()
    assert rig.status(job) == "running"
    rig.tick()
    done = rig.job(job)
    assert done.status == "completed" and done.text == "готово"  # non-ascii on purpose
    assert done.route == "a/main" and done.reason
    assert done.finished_at == rig.clock.now() and done.changed_files is None
    (attempt,) = rig.attempts(job)
    assert (attempt.n, attempt.route, attempt.pool, attempt.outcome) == (
        1,
        "a/main",
        "pool-a",
        "ok",
    )
    assert attempt.duration_s == 1.5 and attempt.finished_at is not None
    assert (attempt.tokens_in, attempt.tokens_out) == (10, 5)
    assert (attempt.pid, attempt.log_tail) == (4242, "ok:готово")  # non-ascii on purpose
    prompt = (rig.home / "jobs" / job.id / "prompt.md").read_text(encoding="utf-8")
    secret = "секретный промпт задачи"  # non-ascii on purpose
    assert prompt.startswith("[agent-router]") and prompt.endswith(secret)
    assert "read-only" in prompt
    call = rig.runner.calls[0]
    assert call.timeout_s == 14400
    assert call.inv.argv == (str(Path("/opt/harness/fake")), "read", "model-x")


@requires_git
def test_timeout_comes_from_request(make_rig) -> None:
    rig = make_rig()
    rig.submit(rig.plain(), timeout_s=90)
    rig.tick()
    assert rig.runner.calls[0].timeout_s == 90


@requires_git
def test_runner_gets_idle_limit(make_rig) -> None:
    rig = make_rig()
    rig.submit(rig.plain())
    rig.tick()
    assert rig.runner.calls[0].idle_timeout_s == 600


@requires_git
def test_claude_route_has_idle_limit(make_rig) -> None:
    from agent_router.adapters.claude import ClaudeAdapter

    rig = make_rig(routes=(route("claude/main", "pool-a", 5, backend="claude"),))
    rig.dispatcher.harnesses["claude"] = HarnessSpec(
        "claude", ClaudeAdapter(), ("claude",), "journal"
    )
    rig.submit(rig.plain())
    rig.tick()
    assert rig.runner.calls[0].idle_timeout_s == 600


@requires_git
def test_idle_zero_disables(make_rig) -> None:
    rig = make_rig(idle_timeout_s=0)
    rig.submit(rig.plain())
    rig.tick()
    assert rig.runner.calls[0].idle_timeout_s is None


@requires_git
def test_explicit_timeout_keeps_idle(make_rig) -> None:
    rig = make_rig()
    rig.submit(rig.plain(), timeout_s=90)
    rig.tick()
    call = rig.runner.calls[0]
    assert call.timeout_s == 90 and call.idle_timeout_s == 600


# --- event log: prompt hash and decision snapshot ---------------------------------------


@requires_git
def test_submit_records_prompt_hash_and_length(make_rig) -> None:
    rig = make_rig()
    prompt = "привет «мир»"  # non-ascii on purpose
    job = rig.dispatcher.submit(JobRequest(prompt, rig.plain()))
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert job.prompt_chars == len(prompt)
    assert job.prompt_sha256 == digest
    stored = rig.job(job)
    assert stored.prompt_chars == len(prompt)
    assert stored.prompt_sha256 == digest


@requires_git
def test_each_attempt_has_own_decision(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step("limit"))
    job = rig.submit(rig.plain())
    rig.tick(2)
    assert rig.status(job) == "running"
    first, second = rig.attempts(job)
    for attempt in (first, second):
        assert attempt.decision is not None
        assert set(attempt.decision) == {
            "at",
            "tier",
            "tier_source",
            "mode",
            "caller",
            "chosen",
            "candidates",
            "excluded",
            "note",
        }
        assert attempt.decision["tier"] == "standard"
        assert attempt.decision["tier_source"] == "default"
        assert attempt.decision["mode"] == "read"
        assert attempt.decision["caller"] is None
        assert attempt.decision["at"] == rig.clock.now().isoformat()
        json.dumps(attempt.decision)  # snapshot is JSON
    assert first.decision["chosen"]["route"] == "a/main"
    assert first.decision["chosen"]["model"] == "model-x"
    assert first.decision["candidates"][0]["route"] == "a/main"
    assert second.decision["chosen"]["route"] == "b/main"
    assert second.decision["candidates"][0]["route"] == "b/main"
    assert {"route": "a/main", "reason": "excluded for this job"} in second.decision["excluded"]
    assert first.decision != second.decision


# --- route choice ------------------------------------------------------------------------


@requires_git
def test_stale_fixed_observation_is_noted_in_stored_decision(make_rig) -> None:
    rig = make_rig()
    now = rig.clock.now()
    rig.store.add_observation(
        Observation(
            "pool-a", "week", 0.0, now - timedelta(days=4), now + timedelta(hours=1), "manual"
        )
    )
    rig.store.add_observation(
        Observation("pool-b", "week", 0.8, now, now + timedelta(days=1), "manual")
    )
    job = rig.submit(rig.plain())
    rig.tick()

    attempt = rig.attempts(job)[0]
    assert attempt.decision is not None
    assert attempt.decision["chosen"]["route"] == "b/main"
    candidates = {item["route"]: item for item in attempt.decision["candidates"]}
    assert candidates["a/main"]["note"] == "no data: observation older than max age"
    assert candidates["b/main"]["note"] is None


@requires_git
def test_explicit_route_skips_tier_reserve_and_caller_pool(make_rig) -> None:
    rig = make_rig()
    now = rig.clock.now()
    for window, fraction in (("5h", 0.5), ("week", 0.9)):  # pool in reserve
        rig.store.add_observation(
            Observation("pool-a", window, fraction, now, now + timedelta(hours=3), "manual")
        )
    assert rig.dispatcher.snapshot().pools["pool-a"].reserve_hit
    job = rig.submit(rig.plain(), tier="cheap", caller="agent-a", route="a/main")
    rig.tick(2)
    done = rig.job(job)
    assert (done.status, done.route) == ("completed", "a/main")


@requires_git
def test_explicit_route_reason(make_rig) -> None:
    rig = make_rig()
    job = rig.submit(rig.plain(), route="b/main")
    rig.tick(2)
    done = rig.job(job)
    assert (done.status, done.route) == ("completed", "b/main")
    assert done.reason == "route set by caller"


@requires_git
def test_explicit_route_without_mode_or_with_blocked_pool_fails(make_rig) -> None:
    rig = make_rig()
    rig.jobs.add_block(PoolBlock("pool-b", rig.clock.now() + timedelta(hours=1), "test"), None)
    no_mode = rig.submit(rig.repo(), "write", route="c/read")
    blocked = rig.submit(rig.plain(), route="b/main")
    rig.tick()
    for job, reason in ((no_mode, "mode write not supported"), (blocked, "pool closed until")):
        done = rig.job(job)
        assert (done.status, done.error_code) == ("failed", "no_route")
        assert reason in (done.error_message or "")
        assert rig.attempts(job) == []


@requires_git
def test_no_routes_fails_with_reasons(make_rig) -> None:
    rig = make_rig()
    job = rig.submit(rig.plain(), tier="cheap", caller="agent-b")
    rig.tick()
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "no_route")
    message = done.error_message or ""
    assert "b/main: caller pool" in message
    assert "a/main: does not serve tier cheap" in message


@requires_git
def test_caller_pool_excluded_and_default_tier(make_rig) -> None:
    rig = make_rig()
    job = rig.submit(rig.plain(), caller="agent-a")
    assert (job.tier, job.tier_source) == ("standard", "default")
    hinted = rig.submit(rig.plain(), tier="strong")
    assert (hinted.tier, hinted.tier_source) == ("strong", "caller")
    rig.tick()
    assert rig.job(job).route == "b/main"
    assert rig.job(hinted).route == "a/main"


@requires_git
def test_missing_executable_excludes_route(make_rig) -> None:
    routes = (route("a/main", "pool-a", 5, backend="fake2"), route("b/main", "pool-b", 4))
    rig = make_rig(routes=routes)
    rig.missing.add("fake2")
    job = rig.submit(rig.plain())
    explicit = rig.submit(rig.plain(), route="a/main")
    rig.tick()
    assert rig.job(job).route == "b/main"
    failed = rig.job(explicit)
    assert (failed.status, failed.error_code) == ("failed", "no_route")
    assert "harness unavailable: executable not found: fake2" in (failed.error_message or "")
    views = {view.route.id: view for view in rig.dispatcher.views()}
    assert not views["a/main"].available and views["b/main"].available


# --- writers and parallelism -------------------------------------------------------------


@requires_git
def test_writers_of_one_repo_run_in_turn(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    repo, other = rig.repo("первый"), rig.repo("второй")  # non-ascii on purpose
    first = rig.submit(repo, "write")
    second = rig.submit(repo, "write")
    reader = rig.submit(repo, "read")
    third = rig.submit(other, "write")
    rig.tick()
    statuses = [rig.status(job) for job in (first, second, reader, third)]
    assert statuses == ["running", "queued", "running", "running"]
    executor.run_all()
    rig.tick()  # first writer done — the second starts on the same tick
    assert rig.status(first) == "completed" and rig.status(second) == "running"


@requires_git
def test_writers_of_one_repo_in_other_case_run_in_turn(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    repo = rig.repo("Repository")
    other_case = repo.parent / "rEPOSITORY"
    if not other_case.exists():
        pytest.skip("the temporary directory is on a case-sensitive filesystem")
    first = rig.submit(repo, "write")
    second = rig.submit(other_case, "write")
    rig.tick()
    assert [rig.status(job) for job in (first, second)] == ["running", "queued"]


@requires_git
def test_max_parallel_is_respected(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor, max_parallel=2)
    jobs = [rig.submit(rig.plain()) for _ in range(5)]
    rig.tick(3)
    assert [rig.status(job) for job in jobs] == ["running"] * 2 + ["queued"] * 3
    assert len(executor.pending) == 2
    executor.run_all()
    rig.tick()
    assert [rig.status(job) for job in jobs] == ["completed"] * 2 + ["running"] * 2 + ["queued"]


@requires_git
def test_reader_behind_sixty_blocked_writers_starts_same_tick(make_rig) -> None:
    rig = make_rig(ManualExecutor())
    repo = rig.repo()
    holder = rig.submit(repo, "write")
    rig.tick()
    waiting = [rig.submit(repo, "write") for _ in range(60)]
    reader = rig.submit(repo, "read")
    rig.tick()
    assert rig.status(holder) == "running" and rig.status(reader) == "running"
    assert {rig.status(job) for job in waiting} == {"queued"}


# --- confirmation -------------------------------------------------------------------------


@requires_git
def test_confirm_then_approve_runs_on_approved_route(make_rig) -> None:
    rig = make_rig(routes=CONFIRM_ROUTES)
    job = rig.submit(rig.plain())
    rig.tick()
    waiting = rig.job(job)
    assert waiting.status == "awaiting_approval" and waiting.approval_route == "a/main"
    assert waiting.approval_deadline == rig.clock.now() + timedelta(seconds=600)
    assert rig.attempts(job) == []
    rig.tick()  # waiting does not start on its own
    assert rig.status(job) == "awaiting_approval"
    approved = rig.dispatcher.approve(job.id)
    assert (approved.status, approved.approved_route) == ("queued", "a/main")
    with pytest.raises(WrongState):
        rig.dispatcher.approve(job.id)  # second approval
    rig.tick(2)
    done = rig.job(job)
    assert (done.status, done.route) == ("completed", "a/main")


@requires_git
def test_approved_route_with_blocked_pool_is_dropped(make_rig) -> None:
    rig = make_rig(routes=CONFIRM_ROUTES)
    job = rig.submit(rig.plain())
    rig.tick()
    rig.jobs.add_block(PoolBlock("pool-a", rig.clock.now() + timedelta(hours=1), "test"), None)
    rig.dispatcher.approve(job.id)
    rig.tick()
    current = rig.job(job)
    assert current.status == "running" and current.route == "b/main"
    assert current.approved_route is None and "a/main" in current.excluded_routes


@requires_git
def test_deny_goes_to_next_route(make_rig) -> None:
    rig = make_rig(routes=CONFIRM_ROUTES)
    denied = rig.submit(rig.plain())
    rig.tick()
    back = rig.dispatcher.deny(denied.id)
    assert back.status == "queued" and back.had_refusal and "a/main" in back.excluded_routes
    rig.tick()
    current = rig.job(denied)
    assert (current.status, current.route) == ("running", "b/main")
    assert current.had_refusal and current.approval_deadline is None


BOTH_CONFIRM = (
    route("a/main", "pool-a", 5, confirm=True),
    route("b/main", "pool-b", 4, confirm=True),
)


@requires_git
def test_expired_approval_declines_with_approval_expired(make_rig) -> None:
    rig = make_rig(routes=BOTH_CONFIRM)
    job = rig.submit(rig.plain())
    rig.tick()
    assert rig.job(job).approval_route == "a/main"
    rig.clock.advance(timedelta(seconds=601))
    rig.tick()
    current = rig.job(job)
    assert current.status == "declined" and current.error_code == "approval_expired"
    assert "route a/main was not approved within 600 s" in (current.error_message or "")
    assert current.excluded_routes == frozenset()
    assert current.approval_route is None and current.approval_deadline is None
    rig.tick()
    assert rig.job(job).status == "declined"  # the second confirm route is not offered
    assert rig.attempts(job) == []


@requires_git
def test_approve_at_deadline_declines_at_once(make_rig) -> None:
    rig = make_rig(routes=BOTH_CONFIRM)
    job = rig.submit(rig.plain())
    rig.tick()
    rig.clock.advance(timedelta(seconds=600))
    with pytest.raises(WrongState, match="approval deadline expired"):
        rig.dispatcher.approve(job.id)
    current = rig.job(job)  # no tick: the expiry is recorded by the approval itself
    assert current.status == "declined" and current.error_code == "approval_expired"


@requires_git
def test_approve_after_expiry_says_expired(make_rig) -> None:
    rig = make_rig(routes=BOTH_CONFIRM)
    job = rig.submit(rig.plain())
    rig.tick()
    rig.clock.advance(timedelta(seconds=601))
    rig.tick()
    for action in (rig.dispatcher.approve, rig.dispatcher.deny):
        with pytest.raises(WrongState, match="^approval deadline expired$"):
            action(job.id)


@requires_git
def test_deny_still_rotates(make_rig) -> None:
    rig = make_rig(routes=BOTH_CONFIRM)
    job = rig.submit(rig.plain())
    rig.tick()
    back = rig.dispatcher.deny(job.id)
    assert back.status == "queued" and back.had_refusal and "a/main" in back.excluded_routes
    rig.clock.advance(timedelta(seconds=30))
    rig.tick()
    current = rig.job(job)
    assert (current.status, current.approval_route) == ("awaiting_approval", "b/main")
    assert current.approval_deadline == rig.clock.now() + timedelta(seconds=600)


@requires_git
def test_deny_without_alternatives_declines(make_rig) -> None:
    rig = make_rig(routes=(route("a/main", "pool-a", 5, confirm=True),))
    job = rig.submit(rig.plain())
    rig.tick()
    rig.dispatcher.deny(job.id)
    rig.tick()
    done = rig.job(job)
    assert done.status == "declined" and done.error_code is None
    assert rig.attempts(job) == []
    with pytest.raises(WrongState):
        rig.dispatcher.deny(job.id)


# --- limits -------------------------------------------------------------------------------


@requires_git
def test_limit_blocks_pool_and_reader_retries_elsewhere(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step("limit"))
    job = rig.submit(rig.plain())
    rig.tick(2)
    assert rig.block_until("pool-a") == rig.clock.now() + timedelta(seconds=1800)
    current = rig.job(job)
    assert current.status == "running" and current.route == "b/main"
    assert "pool-a" in current.excluded_pools
    rig.tick()
    assert rig.status(job) == "completed"
    assert [(a.n, a.pool, a.outcome) for a in rig.attempts(job)] == [
        (1, "pool-a", "limit"),
        (2, "pool-b", "ok"),
    ]


@requires_git
def test_limit_with_recognized_reset(make_rig) -> None:
    rig = make_rig()
    reset = rig.clock.now() + timedelta(minutes=47)
    rig.runner.add(Step(f"limit@{reset.isoformat()}"))
    rig.submit(rig.plain())
    rig.tick(2)
    assert rig.block_until("pool-a") == reset


@requires_git
def test_limit_backoff_doubles_until_success(make_rig) -> None:
    rig = make_rig(limit_backoff_s=1000, limit_backoff_max_s=3500)

    def hit(stdout: str) -> None:
        rig.runner.add(Step(stdout))
        rig.submit(rig.plain(), route="a/main")
        rig.tick(2)

    expected = []
    for seconds in (1000, 2000, 3500):  # third deadline hits the cap
        hit("limit")
        expected.append(rig.block_until("pool-a") - rig.clock.now())
        rig.dispatcher.unblock("pool-a")
        assert seconds == expected[-1].total_seconds()
    hit("ok:готово")  # non-ascii on purpose; success resets the doubling
    hit("limit")
    assert rig.block_until("pool-a") == rig.clock.now() + timedelta(seconds=1000)


@requires_git
def test_backoff_does_not_shorten_recognized_reset(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor, max_parallel=2)
    reset = rig.clock.now() + timedelta(hours=5)
    rig.runner.add(Step(f"limit@{reset.isoformat()}"), Step("limit"))
    rig.submit(rig.plain("первая"), route="a/main")  # non-ascii on purpose
    rig.submit(rig.plain("вторая"), route="a/main")  # non-ascii on purpose
    rig.tick()
    executor.run_all()
    rig.tick()
    assert len(rig.bus.data("pool.blocked")) == 2
    assert rig.block_until("pool-a") == reset  # second limit's backoff is shorter; reset kept
    assert rig.bus.data("pool.blocked")[-1] == {"pool": "pool-a", "until": reset.isoformat()}


@requires_git
def test_limit_in_write_mode(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step("limit", action=new_file()), Step("limit"))
    changed = rig.submit(rig.repo("первый"), "write")  # non-ascii on purpose
    rig.tick(2)
    done = rig.job(changed)
    assert (done.status, done.error_code) == ("partial", "limit")
    assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose
    rig.dispatcher.unblock("pool-a")
    clean = rig.submit(rig.repo("второй"), "write")  # non-ascii on purpose
    rig.tick(2)  # limit without changes → retry in another pool
    retried = rig.job(clean)
    assert (retried.status, retried.route) == ("running", "b/main")
    rig.tick()
    assert rig.status(clean) == "completed" and rig.job(clean).changed_files == ()


@requires_git
def test_three_limits_exhaust_attempts(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step("limit"), Step("limit"), Step("limit"))
    job = rig.submit(rig.plain())
    rig.tick(4)
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "attempts_exhausted")
    assert [a.pool for a in rig.attempts(job)] == ["pool-a", "pool-b", "pool-c"]


# --- timeout and error --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("step", "code"),
    [
        (Step("", exit_code=None, timed_out=True), "timeout"),
        (Step("error:harness crashed", exit_code=2), "executor_error"),
    ],
    ids=["timeout", "error"],
)
@requires_git
def test_emergency_outcomes(make_rig, step: Step, code: str) -> None:
    rig = make_rig()
    cases = {
        "write-changed": ("write", new_file(), "partial"),
        "write-committed": ("write", commit_file(), "partial"),
        "write-clean": ("write", None, "failed"),
        "read": ("read", None, "failed"),
    }
    for name, (mode, action, status) in cases.items():
        rig.runner.add(Step(step.stdout, step.exit_code, step.timed_out, action=action))
        job = rig.submit(rig.repo(name), mode, route="a/main")
        rig.tick(2)
        done = rig.job(job)
        assert (done.status, done.error_code) == (status, code), name
        assert done.error_message
        if name == "write-changed":
            assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose
        elif name == "write-committed":
            assert done.changed_files == ("закоммичен.txt",)  # non-ascii on purpose
            assert done.violations == ("head_moved",)
        else:
            assert done.changed_files == ()
        assert rig.attempts(job)[0].outcome == ("timeout" if code == "timeout" else "error")


@requires_git
def test_timeout_keeps_tokens(make_rig) -> None:
    # the harness printed its final line with tokens and then hung: usage is still recorded
    rig = make_rig()
    rig.runner.add(Step("ok:готово", exit_code=None, timed_out=True))  # non-ascii on purpose
    job = rig.submit(rig.plain())
    rig.tick(2)
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "timeout")
    assert done.error_message == "executor exceeded the allotted time"
    (attempt,) = rig.attempts(job)
    assert attempt.outcome == "timeout"
    assert (attempt.tokens_in, attempt.tokens_out) == (10, 5)
    assert attempt.log_tail == "ok:готово"  # non-ascii on purpose


@requires_git
def test_idle_stop_names_the_last_activity_signal(make_rig) -> None:
    step = Step(
        "ok:done",
        exit_code=None,
        timed_out=True,
        idle=True,
        last_signal="files",
        last_signal_age_s=640.0,
    )
    rig = make_rig()
    rig.runner.add(step)
    job = rig.submit(rig.plain())
    rig.tick(2)
    done = rig.job(job)
    assert done.error_message == (
        "executor showed no progress for 600 s; last activity: files 640 s ago"
    )


@requires_git
def test_idle_stop_without_a_signal_keeps_the_plain_message(make_rig) -> None:
    step = Step("ok:done", exit_code=None, timed_out=True, idle=True)
    rig = make_rig()
    rig.runner.add(step)
    job = rig.submit(rig.plain())
    rig.tick(2)
    done = rig.job(job)
    assert done.error_message == "executor showed no progress for 600 s"


@requires_git
def test_kimi_route_asks_for_the_quiet_signals(make_rig) -> None:
    from agent_router.adapters.kimi import KimiAdapter

    rig = make_rig(routes=(route("kimi/main", "pool-a", 5, backend="kimi"),))
    rig.dispatcher.harnesses["kimi"] = HarnessSpec("kimi", KimiAdapter(), ("kimi",), "journal")
    rig.submit(rig.plain())
    rig.tick()
    assert rig.runner.calls[0].quiet_while_working is True


@pytest.mark.parametrize("backend", ["opencode", "claude"])
@requires_git
def test_streaming_routes_do_not_use_the_quiet_signals(make_rig, backend: str) -> None:
    from agent_router.adapters.claude import ClaudeAdapter
    from agent_router.adapters.opencode import OpenCodeAdapter

    adapter = OpenCodeAdapter() if backend == "opencode" else ClaudeAdapter()
    rig = make_rig(routes=(route(f"{backend}/main", "pool-a", 5, backend=backend),))
    rig.dispatcher.harnesses[backend] = HarnessSpec(backend, adapter, (backend,), "journal")
    rig.submit(rig.plain())
    rig.tick()
    assert rig.runner.calls[0].quiet_while_working is False


@requires_git
def test_a_route_without_the_flag_keeps_the_old_behavior(make_rig) -> None:
    rig = make_rig()
    rig.submit(rig.plain())
    rig.tick()
    assert rig.runner.calls[0].quiet_while_working is False


@requires_git
def test_idle_stop_reports_no_progress(make_rig) -> None:
    # the harness printed its usage line and then went silent: idle stop, usage is still recorded
    rig = make_rig()
    step = Step("ok:готово", exit_code=None, timed_out=True, idle=True)  # non-ascii on purpose
    rig.runner.add(step)
    job = rig.submit(rig.plain())
    rig.tick(2)
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "timeout")
    assert done.error_message == "executor showed no progress for 600 s"
    (attempt,) = rig.attempts(job)
    assert attempt.outcome == "timeout"
    assert (attempt.tokens_in, attempt.tokens_out) == (10, 5)
    assert attempt.log_tail == "ok:готово"  # non-ascii on purpose


@requires_git
def test_timeout_with_truncated_output_keeps_timeout(make_rig) -> None:
    # truncated output the adapter cannot parse: the timeout outcome stands, tokens stay None
    rig = make_rig()
    rig.runner.add(Step("crash", exit_code=None, timed_out=True))
    job = rig.submit(rig.plain())
    rig.tick(2)
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "timeout")
    (attempt,) = rig.attempts(job)
    assert attempt.outcome == "timeout"
    assert (attempt.tokens_in, attempt.tokens_out, attempt.cost_usd) == (None, None, None)


@requires_git
def test_runner_exception_is_executor_error(make_rig) -> None:
    from agent_router.runner import ShimUnsafeArgs

    rig = make_rig()
    rig.runner.add(Step(raises=ShimUnsafeArgs("symbol '&'")), Step(raises=RuntimeError("boom")))
    shim = rig.submit(rig.plain(), route="a/main")
    rig.tick(2)
    other = rig.submit(rig.plain(), route="a/main")
    rig.tick(2)
    done = rig.job(shim)
    assert (done.status, done.error_code) == ("failed", "executor_error")
    assert "shim" in (done.error_message or "")
    assert rig.job(other).error_code == "executor_error"
    assert rig.attempts(other)[0].outcome == "error"


@requires_git
def test_survivor_adds_orphan_violation(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step("", exit_code=None, timed_out=True, survived=True))
    job = rig.submit(rig.repo(), "write")
    rig.tick(2)
    done = rig.job(job)
    assert (done.status, done.error_code) == ("partial", "timeout")
    assert done.violations == ("orphan_process",)
    assert rig.attempts(job)[0].outcome == "timeout"


@pytest.mark.parametrize(("mode", "status"), [("read", "failed"), ("write", "partial")])
@requires_git
def test_survivor_is_never_completed(make_rig, mode: str, status: str) -> None:
    rig = make_rig()
    rig.runner.add(Step("ok:ответ", survived=True))  # non-ascii on purpose
    job = rig.submit(rig.repo(), mode)
    rig.tick(2)
    done = rig.job(job)
    assert (done.status, done.error_code) == (status, "executor_error")
    assert done.violations == ("orphan_process",)
    assert done.text == "ответ"  # non-ascii on purpose
    assert rig.attempts(job)[0].outcome == "error"


@pytest.mark.parametrize("reaped", [False, True], ids=["survivor", "reaped"])
@requires_git
def test_runner_failure_with_live_pid_adds_orphan_violation(make_rig, reaped: bool) -> None:
    rig = make_rig()
    rig.dispatcher.orphan_wait_s = 0.2
    rig.images[4242] = "harness.exe"
    rig.starts[4242] = 1000.0
    if reaped:
        rig.on_kill = lambda pid: rig.images.pop(pid)
    rig.runner.add(Step(raises_after=RuntimeError("crash")))
    job = rig.submit(rig.plain())
    rig.tick(2)
    done = rig.job(job)
    expected = () if reaped else ("orphan_process",)
    assert done.violations == expected
    assert rig.killed == [4242]


# --- cancel and violations ---------------------------------------------------------------


@requires_git
def test_cancel_queued_and_running(make_rig) -> None:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    rig = make_rig(executor, max_parallel=1)
    step = Step(wait_cancel=True)
    rig.runner.add(step)
    running = rig.submit(rig.plain())
    try:
        rig.tick()
        assert step.started.wait(5)
        queued = rig.submit(rig.plain())
        assert rig.dispatcher.cancel(queued.id).status == "cancelled"
        assert rig.attempts(queued) == []
        asked = rig.dispatcher.cancel(running.id)
        assert asked.status == "running" and asked.cancel_requested
        future = rig.dispatcher._active[running.id].future
        assert future is not None
        future.result(timeout=5)
        assert rig.runner.calls[0].cancel.is_set()
        rig.tick()
        done = rig.job(running)
        assert done.status == "cancelled" and rig.attempts(running)[0].outcome == "cancelled"
        with pytest.raises(WrongState):
            rig.dispatcher.cancel(running.id)
        with pytest.raises(KeyError):
            rig.dispatcher.cancel("j_нет такой")  # non-ascii on purpose
    finally:
        for call in rig.runner.calls:
            call.cancel.set()
        executor.shutdown(wait=True, cancel_futures=True)


@requires_git
def test_violations_do_not_change_completed(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step(action=commit_file()), Step(action=new_file()))
    writer = rig.submit(rig.repo("первый"), "write")  # non-ascii on purpose
    rig.tick(2)
    reader = rig.submit(rig.repo("второй"), "read")  # non-ascii on purpose
    rig.tick(2)
    done = rig.job(writer)
    assert done.status == "completed" and done.violations == ("head_moved",)
    assert done.changed_files == ("закоммичен.txt",)  # non-ascii on purpose
    done = rig.job(reader)
    assert done.status == "completed" and done.violations == ("read_mode_files_changed",)
    assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose


# --- tree measurement on failures -------------------------------------------------------


@requires_git
def test_parse_failure_still_measures_tree(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step("crash", action=new_file()))
    job = rig.submit(rig.repo(), "write")
    rig.tick(2)
    done = rig.job(job)
    assert (done.status, done.error_code) == ("partial", "executor_error")
    assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose
    assert (done.error_message or "").startswith("attempt failed: ValueError")


@requires_git
def test_runner_failure_after_start_still_measures_tree(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step(raises_after=RuntimeError("crash"), action=new_file()))
    job = rig.submit(rig.repo(), "write")
    rig.tick(2)
    done = rig.job(job)
    assert (done.status, done.error_code) == ("partial", "executor_error")
    assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose
    assert rig.attempts(job)[0].outcome == "error"


@requires_git
def test_before_snapshot_failure_does_not_launch(make_rig) -> None:
    rig = make_rig()
    repo = rig.repo()
    break_index(repo)
    job = rig.submit(repo, route="a/main")
    rig.tick(2)
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "executor_error")
    assert rig.runner.calls == []
    assert (done.error_message or "").startswith("working tree snapshot failed")
    assert str(repo) not in (done.error_message or "")
    assert done.changed_files is None and done.violations == ()


@requires_git
def test_after_snapshot_failure_marks_changes_unknown(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step(action=break_index))
    job = rig.submit(rig.repo())
    rig.tick(2)
    done = rig.job(job)
    assert done.status == "completed" and done.text == "готово"  # non-ascii on purpose
    assert done.violations == ("changes_unknown",)
    assert done.changed_files == ()


@requires_git
def test_limit_with_unknown_changes_is_not_retried(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step("limit", action=break_index))
    job = rig.submit(rig.repo(), "write")
    rig.tick(3)
    done = rig.job(job)
    assert (done.status, done.error_code) == ("partial", "limit")
    assert done.violations == ("changes_unknown",)
    assert len(rig.attempts(job)) == 1


@requires_git
def test_first_commit_in_empty_repo_is_reported(make_rig) -> None:
    rig = make_rig()
    repo = make_empty_git_repo(rig.root / "без коммитов")  # non-ascii on purpose
    rig.runner.add(Step(action=commit_file()))
    job = rig.submit(repo, "write")
    rig.tick(2)
    done = rig.job(job)
    assert done.status == "completed"
    assert done.changed_files == ("закоммичен.txt",)  # non-ascii on purpose
    assert done.violations == ("head_moved",)


# --- recovery after restart -------------------------------------------------------------


def orphan(
    rig: Rig,
    cwd: Path,
    mode: str,
    pid: int | None = None,
    exe: str | None = None,
    started: float | None = 1000.0,
) -> Job:
    """A running job with an unfinished attempt, as after a gateway crash.

    `started` — process creation time: written to the attempt row and to `rig.starts`
    (the same process still occupies the PID).
    """
    recorded = started if pid is not None else None
    if pid is not None and started is not None:
        rig.starts[pid] = started
    prompt = "задание"  # non-ascii on purpose
    job = Job(
        id=f"j_orphan{len(rig.jobs.list(limit=100))}",
        request=JobRequest(prompt, cwd, mode),  # type: ignore[arg-type]
        status="running",
        tier="standard",
        tier_source="default",
        repo_root=worktree.repo_root(cwd),
        route="a/main",
        created_at=rig.clock.now(),
    )
    rig.jobs.create(job)
    attempt = Attempt(
        job.id, 1, "a/main", "pool-a", rig.clock.now(), pid=pid, exe_name=exe, proc_started=recorded
    )
    attempt.head_before = worktree.head(cwd)
    attempt.tree_before = worktree.tree_snapshot(cwd)
    rig.jobs.add_attempt(attempt)
    return job


@requires_git
def test_recover_marks_running_jobs(make_rig) -> None:
    rig = make_rig(routes=CONFIRM_ROUTES)
    clean = orphan(rig, rig.repo("первый"), "read")  # non-ascii on purpose
    repo = rig.repo("второй")  # non-ascii on purpose
    dirty = orphan(rig, repo, "write")
    new_file()(repo)
    queued = rig.submit(rig.plain("третий"), route="b/main")  # non-ascii on purpose
    rig.runner.add(Step(wait_cancel=True))
    waiting = rig.submit(rig.plain("четвёртый"))  # non-ascii on purpose
    rig.dispatcher.recover()
    done = rig.job(clean)
    assert (done.status, done.error_code, done.changed_files) == ("failed", "gateway_restarted", ())
    done = rig.job(dirty)
    assert (done.status, done.error_code) == ("partial", "gateway_restarted")
    assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose
    attempt = rig.attempts(dirty)[0]
    assert attempt.outcome == "gateway_restarted" and attempt.finished_at is not None
    assert rig.status(queued) == "queued" and rig.status(waiting) == "queued"
    assert rig.killed == []


@requires_git
def test_recover_kills_orphan_before_snapshot(make_rig) -> None:
    rig = make_rig()
    repo = rig.repo()
    job = orphan(rig, repo, "write", pid=777, exe="Harness.EXE")
    rig.images[777] = "harness.exe"

    def on_kill(pid: int) -> None:
        new_file("след процесса.txt")(repo)  # non-ascii on purpose
        del rig.images[pid]

    rig.on_kill = on_kill
    rig.dispatcher.recover()
    done = rig.job(job)
    assert rig.killed == [777]
    files = ("след процесса.txt",)  # non-ascii on purpose
    assert (done.status, done.changed_files) == ("partial", files)
    assert done.violations == ()


@requires_git
def test_recover_leaves_foreign_pid_and_reports_survivor(make_rig) -> None:
    rig = make_rig()
    rig.dispatcher.orphan_wait_s = 0.2
    first_repo = rig.repo("первый")  # non-ascii on purpose
    second_repo = rig.repo("второй")  # non-ascii on purpose
    foreign = orphan(rig, first_repo, "read", pid=555, exe="harness.exe")
    survivor = orphan(rig, second_repo, "read", pid=556, exe="harness.exe")
    rig.images.update({555: "notepad.exe", 556: "harness.exe"})  # 556 does not die
    rig.dispatcher.recover()
    assert rig.killed == [556]
    assert rig.job(foreign).violations == ()
    done = rig.job(survivor)
    assert done.violations == ("orphan_process",)
    assert (done.status, done.error_code) == ("failed", "gateway_restarted")


@requires_git
def test_recover_survivor_makes_writer_partial(make_rig) -> None:
    # process has not exited: working tree is clean, but a writing job is not "untouched"
    rig = make_rig()
    rig.dispatcher.orphan_wait_s = 0.2
    job = orphan(rig, rig.repo(), "write", pid=556, exe="harness.exe")
    rig.images[556] = "harness.exe"  # does not die
    rig.dispatcher.recover()
    done = rig.job(job)
    assert done.violations == ("orphan_process",)
    assert (done.status, done.error_code) == ("partial", "gateway_restarted")
    assert done.changed_files == ()


@requires_git
def test_recover_ignores_waiting_jobs(make_rig) -> None:
    rig = make_rig(routes=CONFIRM_ROUTES)
    waiting = rig.submit(rig.plain())
    rig.tick()
    queued = rig.submit(rig.plain())
    rig.dispatcher.recover()
    assert rig.status(waiting) == "awaiting_approval" and rig.status(queued) == "queued"


@requires_git
def test_recover_snapshot_failure_marks_changes_unknown(make_rig) -> None:
    rig = make_rig()
    repo = rig.repo()
    job = orphan(rig, repo, "write")
    break_index(repo)
    rig.dispatcher.recover()
    done = rig.job(job)
    assert (done.status, done.error_code) == ("partial", "gateway_restarted")
    assert done.violations == ("changes_unknown",)
    assert done.changed_files == ()


# --- recorded attempt outcome and recovery from it --------------------------------------


def crash_after_work(rig: Rig, executor: ManualExecutor) -> None:
    """Worker stored the outcome, but the dispatcher step never collected it; gateway restarted."""
    executor.run_all()
    rig.restart().recover()


def fail_once(monkeypatch, store, name: str) -> None:
    """The first call to the store method raises; subsequent calls proceed normally."""
    real = getattr(store, name)
    left = [1]

    def wrapper(*args, **kwargs):
        if left[0]:
            left[0] -= 1
            raise RuntimeError("store unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, name, wrapper)


@requires_git
def test_worker_stores_result(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    job = rig.submit(rig.plain())
    rig.tick()
    assert rig.attempts(job)[0].result is None
    executor.run_all()
    (attempt,) = rig.attempts(job)
    assert attempt.result is not None
    result = attempt.result
    assert result is not None
    assert (result.outcome, result.text) == ("ok", "готово")  # non-ascii on purpose
    assert attempt.result.changed is None  # directory outside git
    assert rig.status(job) == "running"  # only the dispatcher step changes the status


@requires_git
def test_attempt_and_running_status_are_one_write(make_rig, monkeypatch) -> None:
    rig = make_rig()

    def forbidden(attempt) -> None:
        raise AssertionError("attempt recorded separately from the job status")

    real = rig.jobs.start_attempt
    calls: list[int] = []

    def start(job, attempt) -> None:
        calls.append(attempt.n)
        if len(calls) == 1:
            raise RuntimeError("store unavailable")
        real(job, attempt)

    monkeypatch.setattr(rig.jobs, "add_attempt", forbidden)
    monkeypatch.setattr(rig.jobs, "start_attempt", start)
    job = rig.submit(rig.plain())
    rig.tick()  # the write failure does not crash the step: the job stays queued
    assert rig.status(job) == "queued" and rig.attempts(job) == []
    assert rig.runner.calls == [] and rig.bus.types("job.") == ["job.queued"]
    rig.tick()
    assert rig.status(job) == "running" and [a.n for a in rig.attempts(job)] == [1]
    assert calls == [1, 1] and len(rig.runner.calls) == 1


@requires_git
def test_recover_applies_recorded_ok(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    job = rig.submit(rig.plain())
    rig.tick()
    crash_after_work(rig, executor)
    done = rig.job(job)
    assert done.text == "готово"  # non-ascii on purpose
    assert (done.status, done.error_code) == ("completed", None)
    assert rig.bus.types("job.") == ["job.queued", "job.running", "job.completed"]


@requires_git
def test_recover_applies_recorded_ok_with_changes(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    rig.runner.add(Step(action=new_file()))
    job = rig.submit(rig.repo(), "write")
    rig.tick()
    crash_after_work(rig, executor)
    done = rig.job(job)
    assert done.status == "completed"
    assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose
    assert done.violations == ()


@requires_git
def test_recover_applies_recorded_limit(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    reset = rig.clock.now() + timedelta(hours=2)
    rig.runner.add(Step(f"limit@{reset.isoformat()}"))
    job = rig.submit(rig.plain())
    rig.tick()
    crash_after_work(rig, executor)
    done = rig.job(job)
    assert done.status == "queued" and "pool-a" in done.excluded_pools
    assert rig.block_until("pool-a") == reset
    assert rig.bus.data("pool.blocked") == [{"pool": "pool-a", "until": reset.isoformat()}]


@requires_git
def test_recover_recorded_error_in_write_mode_is_partial(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    rig.runner.add(Step("error:broke", action=new_file()))
    job = rig.submit(rig.repo(), "write")
    rig.tick()
    crash_after_work(rig, executor)
    done = rig.job(job)
    assert (done.status, done.error_code) == ("partial", "executor_error")
    assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose


@requires_git
def test_recover_recorded_cancel_without_request_is_gateway_restart(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    rig.runner.add(Step(cancels=True))  # cancel came from gateway shutdown before collection
    job = rig.submit(rig.plain())
    rig.tick()
    crash_after_work(rig, executor)
    (attempt,) = rig.attempts(job)
    assert attempt.result is not None and attempt.result.outcome == "cancelled"
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "gateway_restarted")


@requires_git
def test_recover_cancel_requested_wins(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    rig.runner.add(Step(wait_cancel=True))
    job = rig.submit(rig.plain())
    rig.tick()
    rig.dispatcher.cancel(job.id)
    crash_after_work(rig, executor)
    done = rig.job(job)
    assert (done.status, done.error_code) == ("cancelled", None)


@requires_git
def test_recover_finished_without_result_recomputes_changes(make_rig) -> None:
    rig = make_rig()
    repo = rig.repo()
    job = orphan(rig, repo, "write")
    (attempt,) = rig.attempts(job)
    attempt.finished_at = rig.clock.now()
    attempt.outcome = "error"  # legacy row shape or an interrupted outcome write
    rig.jobs.update_attempt(attempt)
    new_file()(repo)
    rig.dispatcher.recover()
    done = rig.job(job)
    assert (done.status, done.error_code) == ("partial", "gateway_restarted")
    assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose
    assert rig.attempts(job)[0].outcome == "error"  # finished attempt's outcome not rewritten


def test_recover_running_job_without_attempts(make_rig) -> None:
    rig = make_rig()
    job = Job(
        id="j_no_attempts",
        request=JobRequest("задание", rig.plain()),  # non-ascii on purpose
        status="running",
        tier="standard",
        tier_source="default",
        repo_root=None,
        created_at=rig.clock.now(),
    )
    rig.jobs.create(job)
    rig.dispatcher.recover()
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "gateway_restarted")


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("ok:готово", ("completed", frozenset())),  # non-ascii on purpose
        ("limit", ("queued", frozenset({"pool-a"}))),
        ("error:broke", ("failed", frozenset())),
    ],
)
@requires_git
def test_recover_interrupted_then_repeated(
    make_rig, monkeypatch, stdout: str, expected: tuple
) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    rig.runner.add(Step(stdout))
    job = rig.submit(rig.plain(), route="a/main")
    rig.tick()
    executor.run_all()

    def state() -> tuple:
        blocks = rig.jobs.active_blocks(rig.clock.now())
        return rig.job(job), rig.attempts(job), blocks, rig.bus.types()

    with monkeypatch.context() as patch:
        fail_once(patch, rig.jobs, "update")  # attempt outcome recorded, job outcome not
        with pytest.raises(dispatcher_module.RecoverFailed):
            rig.restart().recover()
    assert rig.status(job) == "running"
    interrupted_types = rig.bus.types()

    rig.restart().recover()
    after_second = state()
    rig.clock.advance(timedelta(minutes=1))
    rig.dispatcher.recover()
    rig.restart().recover()
    assert state() == after_second

    # outcome matches a single uninterrupted recover(): one attempt, one job transition
    done, attempts, blocks, types = after_second
    assert (done.status, done.excluded_pools) == expected
    assert len(attempts) == 1 and attempts[0].finished_at is not None
    assert ("pool-a" in blocks) == (stdout == "limit")
    clean_events = (["pool.blocked"] if stdout == "limit" else []) + [f"job.{done.status}"]
    assert types[len(interrupted_types) :] == clean_events


@requires_git
def test_recover_interrupted_after_attempt_mark(make_rig, monkeypatch) -> None:
    rig = make_rig()
    repo = rig.repo()
    job = orphan(rig, repo, "write")
    new_file()(repo)
    fail_once(monkeypatch, rig.jobs, "update")
    with pytest.raises(dispatcher_module.RecoverFailed):
        rig.dispatcher.recover()
    (attempt,) = rig.attempts(job)
    assert attempt.outcome == "gateway_restarted" and attempt.finished_at is not None
    assert rig.status(job) == "running"  # the status write did not go through
    rig.restart().recover()
    done = rig.job(job)
    assert (done.status, done.error_code) == ("partial", "gateway_restarted")
    assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose
    assert len(rig.attempts(job)) == 1


@requires_git
def test_recover_interrupted_after_pool_block(make_rig, monkeypatch) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    reset = rig.clock.now() + timedelta(hours=2)
    rig.runner.add(Step(f"limit@{reset.isoformat()}"))
    job = rig.submit(rig.plain())
    rig.tick()
    executor.run_all()
    fail_once(monkeypatch, rig.jobs, "update")
    with pytest.raises(dispatcher_module.RecoverFailed):
        rig.restart().recover()
    assert rig.block_until("pool-a") == reset and rig.status(job) == "running"
    rig.restart().recover()
    done = rig.job(job)
    assert done.status == "queued" and "pool-a" in done.excluded_pools
    assert rig.block_until("pool-a") == reset
    assert rig.bus.types("job.").count("job.queued") == 2  # job admission and requeue
    assert len(rig.attempts(job)) == 1


# --- pool closures ------------------------------------------------------------------------


@requires_git
def test_pool_block_events_and_fresh_snapshot(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step("limit"))
    rig.submit(rig.plain(), route="a/main")
    assert "pool-a" not in rig.dispatcher.snapshot().blocks
    rig.tick(2)
    until = rig.block_until("pool-a")
    assert rig.bus.data("pool.blocked") == [{"pool": "pool-a", "until": until.isoformat()}]
    assert rig.dispatcher.snapshot().blocks["pool-a"].until == until  # clock has not moved
    rig.dispatcher.unblock("pool-a")
    assert rig.bus.data("pool.unblocked") == [{"pool": "pool-a", "reason": "manual"}]
    assert "pool-a" not in rig.dispatcher.snapshot().blocks
    job = rig.submit(rig.plain())
    rig.tick()
    assert rig.job(job).route == "a/main"  # the pool is chosen again


@requires_git
def test_expired_block_reopens_pool(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step("limit"))
    rig.submit(rig.plain(), route="a/main")
    rig.tick(2)
    blocked = rig.submit(rig.plain())
    rig.tick()
    assert rig.job(blocked).route == "b/main"
    rig.clock.advance(timedelta(seconds=1801))
    job = rig.submit(rig.plain())
    rig.tick()
    assert rig.bus.data("pool.unblocked") == [{"pool": "pool-a", "reason": "expired"}]
    assert rig.job(job).route == "a/main"


# --- races --------------------------------------------------------------------------------


@requires_git
def test_concurrent_ticks_start_one_attempt(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    job = rig.submit(rig.plain())
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def tick() -> None:
        try:
            barrier.wait(10)
            rig.dispatcher.tick()
        except BaseException as exc:  # a thread error must reach the test
            errors.append(exc)

    threads = [threading.Thread(target=tick) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert len(rig.attempts(job)) == 1 and len(executor.pending) == 1


@requires_git
def test_cancel_after_completion_keeps_terminal_status(make_rig) -> None:
    rig = make_rig()
    job = rig.submit(rig.plain())
    rig.tick(2)
    with pytest.raises(WrongState):
        rig.dispatcher.cancel(job.id)
    rig.tick()
    done = rig.job(job)
    assert done.status == "completed" and not done.cancel_requested


# --- vanished directory and shutdown ------------------------------------------------------


@requires_git
def test_vanished_cwd_fails_without_attempt(make_rig) -> None:
    rig = make_rig()
    cwd = rig.plain("временный")  # non-ascii on purpose
    job = rig.submit(cwd)
    cwd.rmdir()
    rig.tick()
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "cwd_not_allowed")
    assert rig.attempts(job) == []


def git_times_out(monkeypatch) -> None:
    """From this point on git no longer responds in the working tree."""

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(["git"], 30)

    monkeypatch.setattr("agent_router.worktree.subprocess.run", fail)


@requires_git
def test_requeue_git_failure_fails_without_launch(make_rig, monkeypatch) -> None:
    rig = make_rig()
    job = rig.submit(rig.repo())
    root = rig.job(job).repo_root
    assert root is not None
    git_times_out(monkeypatch)
    rig.tick()
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "git_unavailable")
    assert done.repo_root == root  # a git failure does not make it "directory outside git"
    assert rig.runner.calls == [] and rig.attempts(job) == []


@requires_git
def test_requeue_check_keeps_reject_code(make_rig) -> None:
    rig = make_rig()
    job = rig.submit(rig.plain(), route="b/main")
    routes = tuple(
        dataclasses.replace(item, enabled=item.id != "b/main") for item in rig.config.routes
    )
    rig.dispatcher.config = dataclasses.replace(rig.config, routes=routes)  # as after a restart
    rig.tick()
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "bad_route")
    assert rig.runner.calls == []


@requires_git
def test_shutdown_cancels_running_attempts(make_rig) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        rig = make_rig(executor)
        steps = [Step(wait_cancel=True), Step(wait_cancel=True, action=new_file())]
        rig.runner.add(*steps)
        reader = rig.submit(rig.plain())
        writer = rig.submit(rig.repo(), "write")
        rig.tick()
        assert all(step.started.wait(30) for step in steps)
        rig.dispatcher.shutdown()
        assert all(call.cancel.is_set() for call in rig.runner.calls)
        done = rig.job(reader)
        assert (done.status, done.error_code) == ("failed", "gateway_restarted")
        done = rig.job(writer)
        assert (done.status, done.error_code) == ("partial", "gateway_restarted")
        late = rig.submit(rig.plain("после остановки"))  # non-ascii on purpose
        rig.tick(2)
        assert rig.status(late) == "queued" and len(rig.runner.calls) == 2


@requires_git
def test_shutdown_deadline_seals_store(make_rig) -> None:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    step = Step(action=new_file(), release=release)
    rig = make_rig(executor)
    repo = rig.repo()
    rig.runner.add(step)
    job = rig.submit(repo, "write")
    try:
        rig.tick()
        assert step.started.wait(5)
        future = rig.dispatcher._active[job.id].future
        assert future is not None
        started = time.monotonic()
        rig.dispatcher.shutdown(wait_s=0.2)
        assert time.monotonic() - started < 5
        release.set()
        future.result(timeout=5)
        (attempt,) = rig.attempts(job)
        assert attempt.finished_at is None
        assert rig.status(job) == "running"
        rig.restart().recover()
        done = rig.job(job)
        assert (done.status, done.error_code) == ("partial", "gateway_restarted")
        assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


@requires_git
def test_no_launch_after_shutdown(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    repo = rig.repo()
    rig.runner.add(Step(action=new_file()))
    job = rig.submit(repo)
    try:
        rig.tick()
        rig.dispatcher.shutdown(wait_s=0)
        executor.run_all()
        assert rig.runner.calls == []
        assert not (repo / "новый файл.txt").exists()  # non-ascii on purpose
        (attempt,) = rig.attempts(job)
        assert attempt.finished_at is None and attempt.pid is None
        assert rig.status(job) == "running"
        rig.restart().recover()
        done = rig.job(job)
        assert (done.status, done.error_code) == ("failed", "gateway_restarted")
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


@requires_git
def test_shutdown_records_outcome_of_failed_worker(make_rig, monkeypatch) -> None:
    # sealing affects only worker threads: the main thread records the failure outcome on collect
    executor = ManualExecutor()
    rig = make_rig(executor)
    rig.runner.add(Step("ok:ответ"))  # non-ascii on purpose
    job = rig.submit(rig.plain())
    rig.tick()
    real = rig.jobs.update_attempt
    failed: list[int] = []

    def broken(attempt, spend=None) -> bool:
        if attempt.finished_at is not None and not failed:
            failed.append(attempt.n)
            raise RuntimeError("store unavailable")
        return real(attempt, spend)

    monkeypatch.setattr(rig.jobs, "update_attempt", broken)
    executor.run_all()
    assert failed == [1]
    rig.dispatcher.shutdown(wait_s=0)
    (attempt,) = rig.attempts(job)
    assert attempt.finished_at is not None and attempt.outcome == "error"
    assert attempt.result is not None and attempt.result.outcome == "error"


@requires_git
def test_failed_final_write_keeps_measured_changes(make_rig, monkeypatch) -> None:
    # outcome measured but attempt write failed: the writing job's changes are not lost
    rig = make_rig()
    rig.runner.add(Step("ok:ответ", action=new_file()))  # non-ascii on purpose
    job = rig.submit(rig.repo(), "write")
    real = rig.jobs.update_attempt
    failed: list[int] = []

    def broken(attempt, spend=None) -> bool:
        if attempt.finished_at is not None and not failed:
            failed.append(attempt.n)
            raise RuntimeError("store unavailable")
        return real(attempt, spend)

    monkeypatch.setattr(rig.jobs, "update_attempt", broken)
    rig.tick(2)
    assert failed == [1]
    done = rig.job(job)
    assert (done.status, done.error_code) == ("partial", "executor_error")
    assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose
    (attempt,) = rig.attempts(job)
    assert attempt.outcome == "error"
    assert attempt.result is not None
    assert attempt.result.changed == ("новый файл.txt",)  # non-ascii on purpose


@requires_git
def test_worker_finishing_right_after_seal_stays_running(make_rig) -> None:
    # worker finished right after sealing: write skipped, collection left to recover()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    step = Step(action=new_file(), release=release)
    rig = make_rig(executor)
    rig.runner.add(step)
    job = rig.submit(rig.repo(), "write")
    try:
        rig.tick()
        assert step.started.wait(5)
        dispatcher = rig.dispatcher
        future = dispatcher._active[job.id].future
        assert future is not None
        inner = dispatcher._store_lock
        fired: list[bool] = []

        class Gate:
            """Write lock: releases the attempt right after sealing and waits for it to finish."""

            def __enter__(self) -> None:
                inner.acquire()

            def __exit__(self, *exc: object) -> None:
                inner.release()
                if dispatcher._sealed and not fired:
                    fired.append(True)
                    release.set()
                    future.result(timeout=5)

        dispatcher._store_lock = Gate()
        dispatcher.shutdown(wait_s=0.2)
        assert fired == [True]
        (attempt,) = rig.attempts(job)
        assert attempt.finished_at is None
        assert rig.status(job) == "running"
        rig.restart().recover()
        done = rig.job(job)
        assert (done.status, done.error_code) == ("partial", "gateway_restarted")
        assert rig.attempts(job)[0].outcome == "gateway_restarted"
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


@requires_git
def test_failed_apply_is_retried_and_keeps_writer_lock(make_rig, monkeypatch) -> None:
    # status write failure: slot and writer lock stay with the job; outcome applied again
    executor = ManualExecutor()
    rig = make_rig(executor)
    repo = rig.repo()
    first = rig.submit(repo, "write")
    second = rig.submit(repo, "write")
    rig.tick()
    executor.run_all()
    fail_once(monkeypatch, rig.jobs, "update")
    rig.tick()
    assert [rig.status(first), rig.status(second)] == ["running", "queued"]
    rig.tick()  # first outcome applied — only now the second writer starts
    assert [rig.status(first), rig.status(second)] == ["completed", "running"]


def attempt_writes_fail(monkeypatch, rig: Rig, job_ids: set[str]) -> list[bool]:
    """Attempt outcomes for the named jobs are not recorded until the flag is cleared."""
    real = rig.jobs.update_attempt
    broken = [True]

    def failing(attempt, spend=None) -> bool:
        if broken[0] and attempt.finished_at is not None and attempt.job_id in job_ids:
            raise RuntimeError("store unavailable")
        return real(attempt, spend)

    monkeypatch.setattr(rig.jobs, "update_attempt", failing)
    return broken


@requires_git
def test_unrecorded_attempt_keeps_job_running(make_rig, monkeypatch) -> None:
    # neither the worker thread nor the step recorded the attempt outcome: no terminal status
    rig = make_rig()
    rig.runner.add(Step("ok:ответ"))  # non-ascii on purpose
    job = rig.submit(rig.plain(), route="a/main")
    broken = attempt_writes_fail(monkeypatch, rig, {job.id})
    rig.tick()
    rig.tick()
    assert rig.status(job) == "running"
    assert rig.attempts(job)[0].finished_at is None
    broken[0] = False
    rig.tick()  # store is back: outcome recorded and applied
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "executor_error")
    assert rig.attempts(job)[0].finished_at is not None


@requires_git
def test_shutdown_leaves_unrecorded_attempt_to_recover(make_rig, monkeypatch) -> None:
    # on shutdown one failed attempt write must not block collecting the others
    executor = ManualExecutor()
    rig = make_rig(executor)
    rig.runner.add(Step("ok:первый"), Step("ok:второй"))  # non-ascii on purpose
    first = rig.submit(rig.plain("первый"))  # non-ascii on purpose
    second = rig.submit(rig.plain("второй"))  # non-ascii on purpose
    rig.tick()
    attempt_writes_fail(monkeypatch, rig, {first.id})
    executor.run_all()
    rig.dispatcher.shutdown(wait_s=0)
    assert rig.status(first) == "running"
    assert rig.attempts(first)[0].finished_at is None
    assert rig.status(second) == "completed"


@requires_git
def test_shutdown_signals_stop_while_tick_is_busy(make_rig, monkeypatch) -> None:
    # the step is stuck on directory validation; the running attempt's cancel does not wait
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    rig = make_rig(executor)
    step = Step(wait_cancel=True)
    rig.runner.add(step)
    first = rig.submit(rig.plain("первый"))  # non-ascii on purpose
    entered, release = threading.Event(), threading.Event()
    real = dispatcher_module.validate_request_directory
    try:
        rig.tick()
        assert step.started.wait(5)
        second = rig.submit(rig.plain("второй"))  # non-ascii on purpose

        def slow(request, config):
            entered.set()
            release.wait(10)
            return real(request, config)

        monkeypatch.setattr(dispatcher_module, "validate_request_directory", slow)
        ticker = threading.Thread(target=rig.dispatcher.tick)
        ticker.start()
        assert entered.wait(5)
        stopper = threading.Thread(target=rig.dispatcher.shutdown, kwargs={"wait_s": 5})
        stopper.start()
        assert rig.runner.calls[0].cancel.wait(2)
        assert ticker.is_alive()  # cancel arrived while the step still holds the transition lock
        release.set()
        ticker.join(5)
        stopper.join(10)
        assert not ticker.is_alive() and not stopper.is_alive()
        assert rig.status(second) == "queued" and len(rig.runner.calls) == 1
        done = rig.job(first)
        assert (done.status, done.error_code) == ("failed", "gateway_restarted")
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


# --- events --------------------------------------------------------------------------------


@requires_git
def test_event_order_and_no_private_data(make_rig) -> None:
    rig = make_rig()
    cwd = rig.plain("приватный каталог")  # non-ascii on purpose
    job = rig.submit(cwd)
    rig.tick(2)
    assert rig.bus.types() == [
        "job.queued",
        "job.running",
        "spend.recorded",  # the fake harness records spend to the event log
        "attempt.exited",
        "job.completed",
    ]
    first, second = rig.bus.seen[0][1], rig.bus.seen[1][1]
    assert first == {"job": job.id, "from": None, "route": None, "pool": None, "error_code": None}
    assert (second["from"], second["route"], second["pool"]) == ("queued", "a/main", "pool-a")
    assert rig.bus.data("attempt.exited") == [{"job": job.id, "n": 1}]
    dump = json.dumps(rig.bus.seen, ensure_ascii=False)
    assert "секретный промпт" not in dump  # non-ascii on purpose
    assert "приватный каталог" not in dump  # non-ascii on purpose


@requires_git
def test_attempt_is_recorded_before_exit_event(make_rig) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        rig = make_rig(executor)
        seen: list[Attempt] = []
        ready = threading.Event()

        def hook(kind: str, data: dict) -> None:
            if kind == "attempt.exited":
                seen.extend(rig.jobs.attempts(data["job"]))
                ready.set()

        rig.bus.hook = hook
        job = rig.submit(rig.plain())
        rig.tick()
        assert ready.wait(30)
        assert len(seen) == 1 and seen[0].job_id == job.id
        assert seen[0].finished_at is not None and seen[0].outcome == "ok"
        rig.tick()
        assert rig.status(job) == "completed"


# --- queue ---------------------------------------------------------------------------------


@requires_git
def test_queue_limit(make_rig) -> None:
    rig = make_rig(ManualExecutor(), max_queued=2, max_parallel=1)
    rig.submit(rig.plain())
    rig.submit(rig.plain())
    with pytest.raises(RequestRejected) as rejected:
        rig.submit(rig.plain())
    assert rejected.value.code == "queue_full"
    assert len(rig.jobs.list(limit=10)) == 2
    rig.tick()  # one job moved to running
    assert rig.submit(rig.plain()).status == "queued"


def fill_queue(rig: Rig, count: int) -> None:
    cwd = rig.plain("очередь")  # non-ascii on purpose
    for index in range(count):
        rig.jobs.create(
            Job(
                id=f"j_bulk{index:05d}",
                request=JobRequest("задание", cwd),  # non-ascii on purpose
                status="queued",
                tier="standard",
                tier_source="default",
                repo_root=None,
                created_at=rig.clock.now(),
            )
        )


def busy_rig(make_rig) -> tuple[Rig, list[int]]:
    """All slots busy, 1000 jobs in the queue; counts calls to jobs.queued."""
    rig = make_rig(ManualExecutor())
    for _ in range(3):
        rig.submit(rig.plain())
    rig.tick()
    fill_queue(rig, 1000)
    calls: list[int] = []
    original = rig.jobs.queued

    def counted(limit, after=None):
        calls.append(limit)
        return original(limit, after=after)

    rig.jobs.queued = counted  # type: ignore[method-assign]
    return rig, calls


@requires_git
def test_full_slots_do_not_read_queue(make_rig) -> None:
    rig, calls = busy_rig(make_rig)
    rig.tick(20)
    assert calls == []


@pytest.mark.bench
@requires_git
def test_bench_tick_with_full_slots(make_rig) -> None:
    rig, _ = busy_rig(make_rig)
    rounds = 200
    started = time.perf_counter()
    rig.tick(rounds)
    mean_ms = (time.perf_counter() - started) / rounds * 1000
    print(f"\nbench tick: mean {mean_ms:.3f} ms per call (target 50 ms)")


# --- writer lock and changed files ------------------------------------------


def _link_dir(link: Path, target: Path) -> None:
    """Directory link: a junction on Windows, a symlink on POSIX."""
    if sys.platform == "win32":
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        if done.returncode != 0 or not link.exists():
            pytest.skip("failed to create junction")
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("failed to create symbolic link")


def _swap_link(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        link.rmdir()
    else:
        link.unlink()
    _link_dir(link, target)


@requires_git
def test_linked_cwd_swap_before_tick_fails_without_launch(make_rig) -> None:
    rig = make_rig()
    first, second = rig.repo("первый"), rig.repo("второй")  # non-ascii on purpose
    link = rig.root / "связь"  # non-ascii on purpose
    _link_dir(link, first)
    job = rig.submit(link, "write")
    _swap_link(link, second)

    rig.tick()

    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "cwd_changed")
    assert done.repo_root == first.resolve()
    assert rig.runner.calls == [] and rig.attempts(job) == []


@requires_git
def test_linked_cwd_swap_before_worker_does_not_launch(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    first, second = rig.repo("первый"), rig.repo("второй")  # non-ascii on purpose
    link = rig.root / "связь"  # non-ascii on purpose
    _link_dir(link, first)
    job = rig.submit(link, "write", route="a/main")
    rig.tick()
    _swap_link(link, second)

    executor.run_all()
    rig.tick()

    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "executor_error")
    assert done.error_message == "job directory changed after validation"
    assert rig.runner.calls == []
    assert [attempt.outcome for attempt in rig.attempts(job)] == ["error"]


@requires_git
def test_linked_cwd_swap_before_measurement_marks_changes_unknown(make_rig) -> None:
    rig = make_rig()
    first, second = rig.repo("первый"), rig.repo("второй")  # non-ascii on purpose
    link = rig.root / "связь"  # non-ascii on purpose
    _link_dir(link, first)
    rig.runner.add(Step(action=lambda _cwd: _swap_link(link, second)))
    job = rig.submit(link, route="a/main")

    rig.tick(2)

    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "executor_error")
    assert done.error_message == "job directory changed after validation"
    assert done.violations == ("changes_unknown",) and done.changed_files == ()
    assert [call.cwd for call in rig.runner.calls] == [first.resolve()]


@requires_git
def test_writer_lock_rejects_repo_change_after_intake(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    outer = rig.repo("внешний")  # non-ascii on purpose
    inner = outer / "вложенный"  # non-ascii on purpose
    inner.mkdir()
    first = rig.submit(inner, "write")  # at submission the root is the outer repo
    make_git_repo(inner)
    second = rig.submit(inner, "write")  # now the inner repository

    rig.tick()

    assert (rig.status(first), rig.status(second)) == ("failed", "running")
    assert rig.job(first).error_code == "cwd_changed"
    assert rig.job(first).repo_root == outer.resolve()


@requires_git
def test_changed_files_accumulate_across_attempts(make_rig) -> None:
    rig = make_rig()
    repo = rig.repo()
    rig.runner.add(Step("limit", action=new_file()), Step("ok:готово"))  # non-ascii on purpose
    job = rig.submit(repo, "read")
    rig.tick(3)
    done = rig.job(job)
    assert done.status == "completed"
    assert [a.outcome for a in rig.attempts(job)] == ["limit", "ok"]
    assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose
    assert "read_mode_files_changed" in done.violations


@requires_git
def test_recover_linked_cwd_swap_marks_changes_unknown(make_rig) -> None:
    rig = make_rig()
    first, second = rig.repo("первый"), rig.repo("второй")  # non-ascii on purpose
    link = rig.root / "связь"  # non-ascii on purpose
    _link_dir(link, first)
    job = orphan(rig, link, "write")
    rig.dispatcher._checked_cwds[job.id] = first.resolve()
    _swap_link(link, second)

    rig.dispatcher.recover()

    done = rig.job(job)
    assert (done.status, done.error_code) == ("partial", "gateway_restarted")
    assert done.violations == ("changes_unknown",) and done.changed_files == ()


@requires_git
def test_recover_dirty_reader_gets_read_violation(make_rig) -> None:
    rig = make_rig()
    repo = rig.repo()
    job = orphan(rig, repo, "read")
    new_file()(repo)
    rig.dispatcher.recover()
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "gateway_restarted")
    assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose
    assert "read_mode_files_changed" in done.violations


# --- cancel vs completion race ----------------------------------------------------------


@requires_git
def test_cancel_between_exit_and_collect(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    rig.runner.add(Step(action=new_file()))
    job = rig.submit(rig.repo(), "write")
    try:
        rig.tick()
        executor.run_all()
        rig.dispatcher.cancel(job.id)
        rig.tick()
        done = rig.job(job)
        assert done.status == "cancelled"
        assert rig.attempts(job)[0].outcome == "ok"
        assert done.changed_files == ("новый файл.txt",)  # non-ascii on purpose
        assert rig.bus.types("job.").count("job.cancelled") == 1
        assert "job.completed" not in rig.bus.types("job.")
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


# --- one job's failure does not stop the step or recovery --------------------------------


def dispatcher_errors(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "agent_router.dispatcher" and record.levelno >= logging.ERROR
    ]


@requires_git
def test_tick_applies_other_results_when_one_write_fails(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor, max_parallel=2)
    flaky = flaky_jobs(rig)
    first = rig.submit(rig.plain("первая"))  # non-ascii on purpose
    second = rig.submit(rig.plain("вторая"))  # non-ascii on purpose
    rig.tick()
    executor.run_all()
    third = rig.submit(rig.plain("третья"))  # non-ascii on purpose
    fourth = rig.submit(rig.plain("четвёртая"))  # non-ascii on purpose
    flaky.fail("update", first.id)

    rig.tick()  # first outcome not recorded — the step survives and collects the rest
    assert ("update", first.id) in flaky.failures
    assert [rig.status(job) for job in (first, second)] == ["running", "completed"]
    # first job still holds a slot: one of the two waiting jobs starts
    assert sorted(rig.status(job) for job in (third, fourth)) == ["queued", "running"]

    flaky.heal()
    rig.tick()
    assert rig.status(first) == "completed"
    assert rig.job(first).text == "готово"  # non-ascii on purpose
    assert [rig.status(job) for job in (third, fourth)] == ["running", "running"]
    assert len(rig.attempts(first)) == 1


@requires_git
def test_tick_starts_next_job_when_consider_raises(make_rig, monkeypatch) -> None:
    rig = make_rig()
    first = rig.submit(rig.plain("первая"))  # non-ascii on purpose
    second = rig.submit(rig.plain("вторая"))  # non-ascii on purpose
    real = dispatcher_module.validate_request_directory

    def check(req, config):
        if Path(req.cwd).name == "первая":  # non-ascii on purpose
            raise RuntimeError("git is not responding")
        return real(req, config)

    monkeypatch.setattr(dispatcher_module, "validate_request_directory", check)
    rig.tick()
    assert [rig.status(first), rig.status(second)] == ["queued", "running"]
    assert rig.attempts(first) == []
    monkeypatch.setattr(dispatcher_module, "validate_request_directory", real)
    rig.tick()
    assert rig.status(first) == "running"


@requires_git
def test_tick_unblocks_pools_when_collect_fails(make_rig) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    flaky = flaky_jobs(rig)
    job = rig.submit(rig.plain())
    rig.tick()
    executor.run_all()
    rig.jobs.add_block(PoolBlock("pool-b", rig.clock.now() + timedelta(minutes=1), "test"), None)
    rig.clock.advance(timedelta(minutes=2))
    flaky.fail("update", job.id)

    rig.tick()
    assert rig.status(job) == "running"
    assert rig.bus.data("pool.unblocked") == [{"pool": "pool-b", "reason": "expired"}]
    assert "pool-b" not in rig.jobs.active_blocks(rig.clock.now())


@requires_git
def test_tick_expires_other_approvals_when_one_write_fails(make_rig) -> None:
    rig = make_rig(routes=CONFIRM_ROUTES)
    flaky = flaky_jobs(rig)
    stuck = rig.submit(rig.plain("первая"))  # non-ascii on purpose
    other = rig.submit(rig.plain("вторая"))  # non-ascii on purpose
    rig.tick()
    assert [rig.status(stuck), rig.status(other)] == ["awaiting_approval"] * 2
    rig.clock.advance(timedelta(seconds=601))
    flaky.fail("update", stuck.id)

    rig.tick()  # both deadlines expired: the first write fails, the second still expires
    assert rig.status(stuck) == "awaiting_approval"
    current = rig.job(other)
    assert (current.status, current.error_code) == ("declined", "approval_expired")

    flaky.heal()
    rig.tick()
    current = rig.job(stuck)
    assert (current.status, current.error_code) == ("declined", "approval_expired")


@requires_git
def test_retry_after_partial_apply_keeps_attempts_and_block(make_rig) -> None:
    # Limit hit: pool closure recorded, job status write failed. Re-applying the outcome on
    # the next tick matches an unbroken step: one attempt in pool-a, a second in pool-b.
    executor = ManualExecutor()
    rig = make_rig(executor)
    flaky = flaky_jobs(rig)
    rig.runner.add(Step("limit"))
    job = rig.submit(rig.plain())
    rig.tick()
    executor.run_all()
    flaky.fail("update", job.id)

    rig.tick()
    assert rig.status(job) == "running" and len(rig.attempts(job)) == 1
    first_until = rig.block_until("pool-a")

    rig.clock.advance(timedelta(seconds=30))
    flaky.heal()
    rig.tick()
    current = rig.job(job)
    assert current.status == "running" and "pool-a" in current.excluded_pools
    assert [attempt.pool for attempt in rig.attempts(job)] == ["pool-a", "pool-b"]
    assert list(rig.jobs.active_blocks(rig.clock.now())) == ["pool-a"]
    assert rig.block_until("pool-a") >= first_until
    assert len(rig.bus.data("pool.blocked")) in (1, 2)  # the event may be repeated
    assert rig.bus.types("job.").count("job.queued") == 2  # job admission and requeue


@requires_git
def test_repeated_failure_is_logged_once(make_rig, monkeypatch, caplog) -> None:
    caplog.set_level(logging.ERROR, logger="agent_router.dispatcher")
    rig = make_rig(routes=CONFIRM_ROUTES)
    job = rig.submit(rig.plain())
    real = dispatcher_module.validate_request_directory
    broken = [True]

    def check(req, config):
        if broken[0]:
            raise RuntimeError("git is not responding")
        return real(req, config)

    monkeypatch.setattr(dispatcher_module, "validate_request_directory", check)
    rig.tick(3)
    assert rig.status(job) == "queued"
    assert len(dispatcher_errors(caplog)) == 1  # the same failure — one record

    broken[0] = False
    rig.tick()  # success: the failure is forgotten
    assert rig.status(job) == "awaiting_approval"
    rig.dispatcher.deny(job.id)
    broken[0] = True
    rig.tick(2)
    assert rig.status(job) == "queued"
    assert len(dispatcher_errors(caplog)) == 2  # new failure after success — a new record


@requires_git
def test_recover_processes_every_job_then_fails(make_rig, caplog) -> None:
    rig = make_rig()
    flaky = flaky_jobs(rig)
    first = orphan(rig, rig.repo("первая"), "read")  # non-ascii on purpose
    middle = orphan(rig, rig.repo("вторая"), "read")  # non-ascii on purpose
    last = orphan(rig, rig.repo("третья"), "read")  # non-ascii on purpose
    flaky.fail("update", middle.id)

    with pytest.raises(dispatcher_module.RecoverFailed) as failed:
        rig.restart().recover()
    assert failed.value.job_ids == (middle.id,)
    assert str(failed.value) == f"recovery failed for jobs: {middle.id}"
    assert [record.getMessage() for record in dispatcher_errors(caplog)] == [
        f"job recovery failed: {middle.id}"
    ]
    for job in (first, last):
        done = rig.job(job)
        assert (done.status, done.error_code) == ("failed", "gateway_restarted")
    assert rig.status(middle) == "running"

    flaky.heal()
    rig.restart().recover()
    done = rig.job(middle)
    assert (done.status, done.error_code) == ("failed", "gateway_restarted")
    assert len(rig.attempts(middle)) == 1


# --- gateway shutdown while the step waits on git under the lock --------------------------


class HeldStep:
    """The dispatcher step in a separate thread, stuck on job directory validation."""

    def __init__(self, rig: Rig, monkeypatch) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.errors: list[BaseException] = []
        real = dispatcher_module.validate_request_directory

        def slow(request, config):
            self.entered.set()
            self.release.wait(15)
            return real(request, config)

        monkeypatch.setattr(dispatcher_module, "validate_request_directory", slow)
        dispatcher = rig.dispatcher

        def run() -> None:
            try:
                dispatcher.tick()
            except BaseException as exc:
                self.errors.append(exc)

        self.thread = threading.Thread(target=run)
        self.thread.start()
        assert self.entered.wait(5)

    def finish(self) -> None:
        self.release.set()
        self.thread.join(10)
        assert not self.thread.is_alive()


@requires_git
def test_shutdown_returns_while_step_waits_for_git(make_rig, monkeypatch, caplog) -> None:
    caplog.set_level(logging.ERROR, logger="agent_router.dispatcher")
    rig = make_rig()
    job = rig.submit(rig.plain())
    held = HeldStep(rig, monkeypatch)
    try:
        started = time.monotonic()
        rig.dispatcher.shutdown(wait_s=0.5)
        assert time.monotonic() - started < 5  # it does not wait for git to release the step
        assert rig.dispatcher._sealed is True
        assert held.thread.is_alive()
        held.finish()
        assert held.errors == [] and dispatcher_errors(caplog) == []
        assert rig.status(job) == "queued" and rig.attempts(job) == []
        assert rig.runner.calls == []
    finally:
        held.release.set()
        held.thread.join(10)


@requires_git
def test_released_step_does_not_touch_store_after_shutdown(make_rig, monkeypatch) -> None:
    rig = make_rig()
    flaky = flaky_jobs(rig)
    rig.submit(rig.plain("первая"))  # non-ascii on purpose
    rig.submit(rig.plain("вторая"))  # non-ascii on purpose
    held = HeldStep(rig, monkeypatch)
    try:
        rig.dispatcher.shutdown(wait_s=0.2)
        flaky.fail_all = True  # store is "closed": any call raises and is counted
        held.finish()
        assert held.errors == []
        assert flaky.calls == []
        rig.dispatcher.tick()  # a tick after shutdown does not touch the store
        assert flaky.calls == []
        assert rig.runner.calls == []
    finally:
        flaky.fail_all = False
        held.release.set()
        held.thread.join(10)


@requires_git
def test_shutdown_leaves_finished_attempt_to_recover_when_lock_is_held(
    make_rig, monkeypatch
) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    job = rig.submit(rig.plain("первая"))  # non-ascii on purpose
    rig.tick()
    waiting = rig.submit(rig.plain("вторая"))  # non-ascii on purpose
    held = HeldStep(rig, monkeypatch)  # step holds the lock while validating the second job
    try:
        executor.run_all()  # first attempt finished, outcome recorded in its row
        assert rig.attempts(job)[0].result is not None
        started = time.monotonic()
        rig.dispatcher.shutdown(wait_s=0.2)
        assert time.monotonic() - started < 5
        assert rig.status(job) == "running"  # lock not acquired: outcome not applied
        held.finish()
        assert held.errors == []
        assert rig.status(job) == "running" and rig.status(waiting) == "queued"
        rig.restart().recover()
        done = rig.job(job)
        assert (done.status, done.text) == ("completed", "готово")  # non-ascii on purpose
        assert len(rig.attempts(job)) == 1
    finally:
        held.release.set()
        held.thread.join(10)


# --- supervision: query deadline and `_reap` --------------------------------------------


@requires_git
def test_reap_deadline_starts_before_first_query(make_rig, monkeypatch) -> None:
    rig = make_rig()
    rig.dispatcher.orphan_wait_s = 2.0
    job = orphan(rig, rig.repo(), "read", pid=4242, exe="node.exe")
    (attempt,) = rig.attempts(job)
    now = [0.0]
    queries: list[int] = []

    def image(pid: int) -> str:
        queries.append(pid)
        now[0] += 1.0
        return "node.exe"

    monkeypatch.setattr(dispatcher_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(dispatcher_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(rig.dispatcher, "_image", image)

    assert rig.dispatcher._reap(attempt) is False
    assert queries == [4242, 4242]
    assert rig.killed == [4242]


@requires_git
def test_reap_checks_once_after_kill_past_deadline(make_rig, monkeypatch) -> None:
    rig = make_rig()
    rig.dispatcher.orphan_wait_s = 1.0
    job = orphan(rig, rig.repo(), "read", pid=4242, exe="node.exe")
    (attempt,) = rig.attempts(job)
    rig.images[4242] = "node.exe"
    rig.on_kill = lambda pid: rig.images.pop(pid)
    now = [0.0]
    queries: list[int] = []

    def image(pid: int) -> str | None:
        queries.append(pid)
        now[0] += 1.0  # the query consumed the whole deadline
        return rig.images.get(pid)

    monkeypatch.setattr(dispatcher_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(dispatcher_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(rig.dispatcher, "_image", image)

    assert rig.dispatcher._reap(attempt) is True
    assert queries == [4242, 4242]  # identity check and one post-kill check
    assert rig.killed == [4242]


def test_stop_wait_covers_worst_reap_path(make_rig) -> None:
    from agent_router.runner import KILL_WAIT_S, QUERY_WAIT_S, STOP_BUDGET_S

    rig = make_rig()
    extra = rig.dispatcher.stop_wait_s - STOP_BUDGET_S
    # two image queries with a pause, creation time, kill, post-kill check, 5 s margin
    pause = dispatcher_module.REAP_RETRY_PAUSE_S
    assert extra >= 4 * QUERY_WAIT_S + KILL_WAIT_S + pause + 5
    assert extra >= rig.dispatcher.orphan_wait_s + QUERY_WAIT_S + 5


def _reap_answers(rig: Rig, monkeypatch, answers: deque, now: list[float] | None = None) -> list:
    """Stubs the image query: answers pop in order, exceptions propagate, queries are recorded."""
    queries: list[int] = []

    def image(pid: int) -> str | None:
        queries.append(pid)
        answer = answers.popleft()
        if now is not None:
            now[0] += dispatcher_module.QUERY_WAIT_S
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(rig.dispatcher, "_image", image)
    return queries


@requires_git
def test_reap_retries_failed_first_query(make_rig, monkeypatch) -> None:
    rig = make_rig()
    job = orphan(rig, rig.repo(), "read", pid=4242, exe="node.exe", started=1000.0)
    (attempt,) = rig.attempts(job)
    rig.starts[4242] = 1000.0
    answers = deque([ProcessQueryError("query failed"), "node.exe", None])
    queries = _reap_answers(rig, monkeypatch, answers)

    assert rig.dispatcher._reap(attempt) is True
    assert rig.killed == [4242]
    assert queries == [4242, 4242, 4242] and not answers


@requires_git
def test_reap_two_failed_queries_give_up(make_rig, monkeypatch) -> None:
    rig = make_rig()
    job = orphan(rig, rig.repo(), "read", pid=4242, exe="node.exe", started=1000.0)
    (attempt,) = rig.attempts(job)
    rig.starts[4242] = 1000.0
    answers = deque([ProcessQueryError("query failed"), ProcessQueryError("again")])
    queries = _reap_answers(rig, monkeypatch, answers)

    assert rig.dispatcher._reap(attempt) is False
    assert rig.killed == []
    assert queries == [4242, 4242]


@pytest.mark.parametrize(("now_start", "clean"), [(2000.0, True), (1000.0, False)])
@requires_git
def test_reap_without_exe_name_checks_start(make_rig, now_start: float, clean: bool) -> None:
    """No image recorded: foreign start time means a foreign PID; a match leaves it unconfirmed."""
    rig = make_rig()
    job = orphan(rig, rig.repo(), "read", pid=4242, exe=None, started=1000.0)
    (attempt,) = rig.attempts(job)
    rig.images[4242] = "node.exe"
    rig.starts[4242] = now_start

    assert rig.dispatcher._reap(attempt) is clean
    assert rig.killed == []


@requires_git
def test_reap_worst_path_fits_stop_budget(make_rig, monkeypatch) -> None:
    from agent_router.runner import KILL_WAIT_S, STOP_BUDGET_S

    rig = make_rig()
    job = orphan(rig, rig.repo(), "read", pid=4242, exe="node.exe", started=1000.0)
    (attempt,) = rig.attempts(job)
    now = [0.0]

    def started(pid: int) -> float:
        now[0] += dispatcher_module.QUERY_WAIT_S
        return 1000.0

    def kill(pid: int) -> None:
        now[0] += KILL_WAIT_S

    def sleep(seconds: float) -> None:
        now[0] += seconds

    answers = deque([ProcessQueryError("query failed"), "node.exe", "node.exe"])
    _reap_answers(rig, monkeypatch, answers, now)
    monkeypatch.setattr(rig.dispatcher, "_started", started)
    monkeypatch.setattr(rig.dispatcher, "_kill", kill)
    monkeypatch.setattr(dispatcher_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(dispatcher_module.time, "sleep", sleep)

    assert rig.dispatcher._reap(attempt) is False
    assert not answers
    assert now[0] <= rig.dispatcher.stop_wait_s - STOP_BUDGET_S - 5


# --- attempt after the shutdown signal ---------------------------------------------------


def _stop_from_other_thread(rig: Rig, errors: list[BaseException]) -> threading.Thread:
    """Shutdown from another thread; the step holds the lock, so shutdown() gives up at deadline."""

    def stop() -> None:
        try:
            rig.dispatcher.shutdown(wait_s=0.2)
        except BaseException as exc:  # asserted in the test body
            errors.append(exc)

    thread = threading.Thread(target=stop)
    thread.start()
    thread.join(10)
    return thread


@requires_git
def test_shutdown_during_start_cancels_new_attempt(make_rig, monkeypatch) -> None:
    executor = ManualExecutor()
    rig = make_rig(executor)
    rig.submit(rig.plain())
    real = rig.dispatcher.jobs.start_attempt
    errors: list[BaseException] = []
    threads: list[threading.Thread] = []

    def start(job, attempt) -> None:
        real(job, attempt)
        threads.append(_stop_from_other_thread(rig, errors))

    with monkeypatch.context() as patch:
        patch.setattr(rig.dispatcher.jobs, "start_attempt", start)
        rig.tick()
    executor.run_all()
    assert len(threads) == 1 and not threads[0].is_alive()
    assert errors == []
    assert rig.runner.calls == []


@requires_git
def test_shutdown_before_running_keeps_job_queued(make_rig, monkeypatch) -> None:
    rig = make_rig()
    job = rig.submit(rig.plain())
    real = dispatcher_module.with_preamble
    errors: list[BaseException] = []
    threads: list[threading.Thread] = []

    def preamble(prompt, mode):
        threads.append(_stop_from_other_thread(rig, errors))
        return real(prompt, mode)

    with monkeypatch.context() as patch:
        patch.setattr(dispatcher_module, "with_preamble", preamble)
        rig.tick()
    assert len(threads) == 1 and not threads[0].is_alive()
    assert errors == []
    assert rig.status(job) == "queued"
    assert rig.attempts(job) == []
    assert rig.runner.calls == []


# --- process identity during recovery -----------------------------------------------------


@requires_git
def test_reap_spares_foreign_process_with_same_image(make_rig) -> None:
    rig = make_rig()
    job = orphan(rig, rig.repo(), "read", pid=4242, exe="node.exe", started=1000.0)
    rig.images[4242] = "node.exe"
    rig.starts[4242] = 5000.0  # after reboot the PID is a foreign process with the same name
    rig.restart().recover()
    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "gateway_restarted")
    assert rig.killed == [] and done.violations == ()


@requires_git
def test_reap_kills_same_process(make_rig) -> None:
    rig = make_rig()
    job = orphan(rig, rig.repo(), "read", pid=4242, exe="node.exe", started=1000.0)
    rig.images[4242] = "node.exe"
    rig.starts[4242] = 1000.4  # within tolerance: the same process
    rig.on_kill = lambda pid: rig.images.pop(pid)
    rig.restart().recover()
    done = rig.job(job)
    assert rig.killed == [4242]
    assert (done.status, done.violations) == ("failed", ())


@pytest.mark.parametrize("case", ["no_recorded_start", "start_unknown_now", "no_exe_name"])
@requires_git
def test_reap_unconfirmed_identity_does_not_kill(make_rig, case: str) -> None:
    rig = make_rig()
    exe = None if case == "no_exe_name" else "node.exe"
    recorded = None if case == "no_recorded_start" else 1000.0
    job = orphan(rig, rig.repo(), "read", pid=4242, exe=exe, started=recorded)
    rig.images[4242] = "node.exe"  # PID is alive
    if case == "start_unknown_now":
        rig.starts.clear()
    else:
        rig.starts[4242] = 1000.0
    started = time.monotonic()
    rig.restart().recover()
    assert time.monotonic() - started < 4  # an unconfirmed process is not awaited
    done = rig.job(job)
    assert rig.killed == []
    assert (done.status, done.error_code) == ("failed", "gateway_restarted")
    assert done.violations == ("orphan_process",)


@requires_git
def test_reap_unconfirmed_identity_and_dead_pid_is_clean(make_rig) -> None:
    rig = make_rig()
    job = orphan(rig, rig.repo(), "read", pid=4242, exe="node.exe", started=None)
    rig.restart().recover()
    done = rig.job(job)
    assert rig.killed == [] and done.violations == ()
    assert (done.status, done.error_code) == ("failed", "gateway_restarted")


# --- a process query failure is not "process gone" ----------------------------------------


@requires_git
def test_reap_query_failure_does_not_count_as_clean(make_rig) -> None:
    rig = make_rig()
    job = orphan(rig, rig.repo(), "read", pid=4242, exe="node.exe", started=1000.0)
    (attempt,) = rig.attempts(job)
    rig.images[4242] = "node.exe"
    rig.starts[4242] = 1000.0
    rig.image_failures = {4242}
    assert rig.dispatcher._reap(attempt) is False
    assert rig.killed == []


@requires_git
def test_recover_with_failed_query_records_orphan(make_rig) -> None:
    rig = make_rig()
    job = orphan(rig, rig.repo(), "read", pid=4242, exe="node.exe", started=1000.0)
    rig.images[4242] = "node.exe"
    rig.starts[4242] = 1000.0
    rig.image_failures = {4242}
    rig.restart().recover()
    done = rig.job(job)
    assert "orphan_process" in done.violations
    assert rig.killed == []


@requires_git
def test_reap_query_failure_after_kill_keeps_waiting(make_rig, monkeypatch) -> None:
    rig = make_rig()
    job = orphan(rig, rig.repo(), "read", pid=4242, exe="node.exe", started=1000.0)
    (attempt,) = rig.attempts(job)
    answers: deque = deque(["node.exe", ProcessQueryError("query failed"), None])

    def image(pid: int) -> str | None:
        answer = answers.popleft()
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(rig.dispatcher, "_image", image)
    assert rig.dispatcher._reap(attempt) is True
    assert rig.killed == [4242]
    assert not answers


@requires_git
def test_start_query_failure_leaves_exe_name_empty(make_rig) -> None:
    rig = make_rig()
    rig.image_failures = {4242}
    job = rig.submit(rig.plain())
    rig.tick(2)
    assert rig.status(job) == "completed"
    (attempt,) = rig.attempts(job)
    assert (attempt.pid, attempt.exe_name) == (4242, None)


@requires_git
def test_on_start_records_process_start(make_rig) -> None:
    rig = make_rig()
    rig.images[4242] = "harness.exe"
    rig.starts[4242] = 1234.5
    job = rig.submit(rig.plain())
    rig.tick()
    (attempt,) = rig.attempts(job)
    assert (attempt.pid, attempt.exe_name, attempt.proc_started) == (4242, "harness.exe", 1234.5)


# --- limit with a surviving process; reason for unmeasured tree ---------------------------


@requires_git
def test_limit_with_surviving_process_does_not_retry(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step("limit", survived=True))
    job = rig.submit(rig.plain(), "read")

    rig.tick(2)

    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "limit")
    assert done.violations == ("orphan_process",)
    assert len(rig.attempts(job)) == 1
    assert rig.block_until("pool-a") == rig.clock.now() + timedelta(seconds=1800)


@requires_git
def test_limit_without_survivor_still_retries(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step("limit"))
    job = rig.submit(rig.plain(), "read")

    rig.tick(2)

    current = rig.job(job)
    assert (current.status, current.route) == ("running", "b/main")
    assert [attempt.pool for attempt in rig.attempts(job)] == ["pool-a", "pool-b"]
    rig.tick()
    assert rig.status(job) == "completed"


@requires_git
def test_changes_unknown_keeps_reason_in_log_tail(make_rig) -> None:
    rig = make_rig()
    output = "ok:" + "x" * (dispatcher_module.LOG_TAIL_CHARS + 100)
    rig.runner.add(Step(output, action=break_index))
    job = rig.submit(rig.repo())

    rig.tick(2)

    done = rig.job(job)
    (attempt,) = rig.attempts(job)
    assert done.violations == ("changes_unknown",)
    assert attempt.log_tail is not None
    assert len(attempt.log_tail) <= dispatcher_module.LOG_TAIL_CHARS
    reason = attempt.log_tail.splitlines()[-1]
    assert reason.startswith("[agent-router: post-run measurement failed: ")
    assert "git status" in reason and reason.endswith("]")


@requires_git
def test_recover_changes_unknown_keeps_reason(make_rig) -> None:
    rig = make_rig()
    repo = rig.repo()
    job = orphan(rig, repo, "write")
    (attempt,) = rig.attempts(job)
    previous = "прежний вывод\n"  # non-ascii on purpose
    attempt.log_tail = previous + "x" * (dispatcher_module.LOG_TAIL_CHARS + 100)
    rig.jobs.update_attempt(attempt)
    break_index(repo)

    rig.restart().recover()

    done = rig.job(job)
    (attempt,) = rig.attempts(job)
    assert done.violations == ("changes_unknown",)
    assert attempt.log_tail is not None
    assert len(attempt.log_tail) <= dispatcher_module.LOG_TAIL_CHARS
    reason = attempt.log_tail.splitlines()[-1]
    assert reason.startswith("[agent-router: post-run measurement failed: ")
    assert "git status" in reason and reason.endswith("]")


# --- error texts without paths ------------------------------------------------------------


@requires_git
def test_adapter_error_loses_task_directory(make_rig) -> None:
    rig = make_rig()
    cwd = rig.plain()
    rig.runner.add(Step(f"error:failed: {cwd} and {cwd.as_posix()}", exit_code=2))
    job = rig.submit(cwd, route="a/main")

    rig.tick(2)

    message = rig.job(job).error_message or ""
    assert str(cwd) not in message
    assert cwd.as_posix() not in message
    assert "<dir>" in message


@requires_git
def test_long_adapter_error_is_cut(make_rig) -> None:
    rig = make_rig()
    rig.runner.add(Step("error:" + "x" * 2000, exit_code=2))
    job = rig.submit(rig.plain(), route="a/main")

    rig.tick(2)

    message = rig.job(job).error_message or ""
    assert len(message) <= dispatcher_module.ERROR_TEXT_MAX + 1
    assert message.endswith("…")


@requires_git
def test_attempt_exception_text_loses_task_directory(make_rig) -> None:
    rig = make_rig()
    cwd = rig.plain()
    rig.runner.add(Step(raises=RuntimeError(f"launch failed: {cwd}")))
    job = rig.submit(cwd, route="a/main")

    rig.tick(2)

    message = rig.job(job).error_message or ""
    assert message.startswith("attempt failed: RuntimeError: ")
    assert str(cwd) not in message
    assert cwd.as_posix() not in message
    assert "<dir>" in message


@requires_git
def test_prompt_file_failure_has_no_path(make_rig, monkeypatch) -> None:
    rig = make_rig()
    job = rig.submit(rig.plain())

    def fail(self, *args, **kwargs):
        raise OSError(f"write failed: {self}")

    monkeypatch.setattr(Path, "write_text", fail)
    rig.tick()

    done = rig.job(job)
    message = done.error_message or ""
    assert (done.status, done.error_code) == ("failed", "executor_error")
    assert "OSError" in message
    assert str(rig.home) not in message
    assert rig.home.as_posix() not in message


@requires_git
def test_no_route_explains_caller_pool(make_rig) -> None:
    rig = make_rig(routes=(route("a/main", "pool-a", 5),))
    job = rig.submit(rig.plain(), caller="agent-a")
    rig.tick()

    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "no_route")
    message = done.error_message or ""
    assert "a/main: caller pool" in message
    assert (
        "the gateway never sends a job back to the caller's own pool pool-a: "
        "add a route on another pool or run without --caller"
    ) in message


@requires_git
def test_no_route_without_caller_pool_has_no_hint(make_rig) -> None:
    rig = make_rig(routes=(route("a/main", "pool-a", 5, modes=("read",)),))
    job = rig.submit(rig.repo(), mode="write")
    rig.tick()

    done = rig.job(job)
    assert "caller's own pool" not in (done.error_message or "")


# --- executor error: fallback and route health ------------------------------------------

TWO_ROUTES = (route("a/main", "pool-a", 5), route("b/main", "pool-b", 4))


@requires_git
def test_request_excluded_pool_uses_another_pool_without_job_exclusion(make_rig) -> None:
    rig = make_rig(routes=TWO_ROUTES)
    job = rig.submit(rig.plain(), excluded_pools=frozenset({"pool-a"}))

    assert job.excluded_pools == frozenset()
    rig.tick(2)

    done = rig.job(job)
    assert (done.status, done.route) == ("completed", "b/main")
    (attempt,) = rig.attempts(job)
    assert attempt.decision is not None
    assert {"route": "a/main", "reason": "excluded by request"} in attempt.decision["excluded"]


@requires_git
def test_request_excluded_pool_no_route_names_request_reason(make_rig) -> None:
    rig = make_rig(routes=(route("a/main", "pool-a", 5),))
    job = rig.submit(rig.plain(), excluded_pools=frozenset({"pool-a"}))

    rig.tick()

    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "no_route")
    assert "a/main: excluded by request" in (done.error_message or "")


# --- review jobs: the author of the reviewed job is avoided -------------------------------

REVIEW_ROUTES = (
    route("a/main", "pool-a", 5, model_key="mk-author"),
    route("b/twin", "pool-b", 4, model_key="mk-author"),
    route("c/other", "pool-c", 3),
)


def reviewed(rig: Rig, route_id: str = "a/main") -> Job:
    """A completed job on the given route: the author the review looks at."""
    author = rig.submit(rig.plain(), route=route_id)
    rig.tick(2)
    assert rig.status(author) == "completed"
    return author


@requires_git
def test_review_stays_off_the_author_model_group_and_pool(make_rig) -> None:
    rig = make_rig(routes=REVIEW_ROUTES)
    author = reviewed(rig)

    review = rig.submit(rig.plain(), kind="review", review_of=author.id)
    rig.tick(2)

    done = rig.job(review)
    assert (done.status, done.route) == ("completed", "c/other")
    (attempt,) = rig.attempts(review)
    assert attempt.decision is not None
    excluded = {item["route"]: item["reason"] for item in attempt.decision["excluded"]}
    assert excluded == {"a/main": "author of reviewed job", "b/twin": "author of reviewed job"}
    assert attempt.decision["note"] is None


@requires_git
def test_review_of_requires_review_kind(make_rig) -> None:
    rig = make_rig()
    author = reviewed(rig)

    for kind in (None, "code"):
        with pytest.raises(RequestRejected) as caught:
            rig.submit(rig.plain(), kind=kind, review_of=author.id)
        assert caught.value.code == "bad_review_of"


@requires_git
def test_review_of_unknown_job_is_rejected(make_rig) -> None:
    rig = make_rig()
    with pytest.raises(RequestRejected) as caught:
        rig.submit(rig.plain(), kind="review", review_of="j_missing")
    assert caught.value.code == "bad_review_of"
    assert caught.value.message == "unknown job: j_missing"


@requires_git
def test_review_with_pinned_route_is_not_overridden(make_rig) -> None:
    rig = make_rig(routes=REVIEW_ROUTES)
    author = reviewed(rig)

    review = rig.submit(rig.plain(), kind="review", review_of=author.id, route="a/main")
    rig.tick(2)

    done = rig.job(review)
    assert (done.status, done.route) == ("completed", "a/main")
    assert done.reason == "route set by caller"


@requires_git
def test_review_runs_on_author_model_when_nothing_else_is_eligible(make_rig) -> None:
    rig = make_rig(routes=(route("a/main", "pool-a", 5),))
    author = reviewed(rig)

    review = rig.submit(rig.plain(), kind="review", review_of=author.id)
    rig.tick(2)

    done = rig.job(review)
    assert (done.status, done.route) == ("completed", "a/main")
    note = "review runs on the author's model: nothing else was eligible"
    assert done.reason is not None and done.reason.endswith(f"; {note}")
    (attempt,) = rig.attempts(review)
    assert attempt.decision is not None
    assert attempt.decision["note"] == note
    assert attempt.decision["excluded"] == []


@requires_git
def test_review_no_route_message_names_the_author_reason(make_rig) -> None:
    # the author's route is the only one, and it does not serve the tier: no fallback
    rig = make_rig(routes=(route("a/main", "pool-a", 5, tiers=("cheap",)),))
    author = rig.submit(rig.plain(), tier="cheap", route="a/main")
    rig.tick(2)
    assert rig.status(author) == "completed"

    review = rig.submit(rig.plain(), kind="review", review_of=author.id)
    rig.tick()

    done = rig.job(review)
    assert (done.status, done.error_code) == ("failed", "no_route")
    assert "a/main: author of reviewed job" in (done.error_message or "")


@requires_git
def test_review_of_a_queued_job_has_no_author_yet(make_rig) -> None:
    rig = make_rig(routes=REVIEW_ROUTES)
    queued = rig.submit(rig.plain())

    review = rig.submit(rig.plain(), kind="review", review_of=queued.id)
    rig.tick(2)

    assert rig.status(review) == "completed"  # no route recorded yet: nothing is excluded


@requires_git
def test_review_of_survives_restart(make_rig) -> None:
    rig = make_rig(routes=REVIEW_ROUTES)
    author = reviewed(rig)
    review = rig.submit(rig.plain(), kind="review", review_of=author.id)

    rig.restart()
    rig.tick(2)

    done = rig.job(review)
    assert (done.status, done.route) == ("completed", "c/other")


@requires_git
def test_read_job_error_falls_back_to_next_route(make_rig) -> None:
    rig = make_rig(routes=TWO_ROUTES)
    rig.runner.add(Step("error:harness crashed", exit_code=2))
    job = rig.submit(rig.plain())
    rig.tick(4)

    done = rig.job(job)
    assert (done.status, done.error_code, done.route) == ("completed", None, "b/main")
    assert [(a.route, a.outcome) for a in rig.attempts(job)] == [
        ("a/main", "error"),
        ("b/main", "ok"),
    ]
    assert "a/main" in done.excluded_routes


@requires_git
def test_write_job_error_without_changes_falls_back(make_rig) -> None:
    rig = make_rig(routes=TWO_ROUTES)
    rig.runner.add(Step("error:harness crashed", exit_code=2))
    job = rig.submit(rig.repo(), "write")
    rig.tick(4)

    done = rig.job(job)
    assert (done.status, done.route) == ("completed", "b/main")


@requires_git
def test_write_job_error_with_changes_is_partial(make_rig) -> None:
    rig = make_rig(routes=TWO_ROUTES)
    rig.runner.add(Step("error:harness crashed", exit_code=2, action=new_file()))
    job = rig.submit(rig.repo(), "write")
    rig.tick(4)

    done = rig.job(job)
    assert (done.status, done.error_code) == ("partial", "executor_error")
    assert len(rig.attempts(job)) == 1


@requires_git
def test_error_with_pinned_route_fails(make_rig) -> None:
    rig = make_rig(routes=TWO_ROUTES)
    rig.runner.add(Step("error:harness crashed", exit_code=2))
    job = rig.submit(rig.plain(), route="a/main")
    rig.tick(4)

    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "executor_error")
    assert "harness crashed" in (done.error_message or "")
    assert len(rig.attempts(job)) == 1


@requires_git
def test_error_fallback_to_confirm_route_awaits_approval(make_rig) -> None:
    routes = (route("a/main", "pool-a", 5), route("b/main", "pool-b", 4, confirm=True))
    rig = make_rig(routes=routes)
    rig.runner.add(Step("error:harness crashed", exit_code=2))
    job = rig.submit(rig.plain())
    rig.tick(4)

    done = rig.job(job)
    assert (done.status, done.approval_route) == ("awaiting_approval", "b/main")
    assert done.error_code is None


@requires_git
def test_error_fallback_exhausts_attempts(make_rig) -> None:
    rig = make_rig(routes=TWO_ROUTES, max_attempts=2)
    rig.runner.add(Step("error:first", exit_code=2), Step("error:second", exit_code=2))
    job = rig.submit(rig.plain())
    rig.tick(5)

    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "attempts_exhausted")
    assert [a.route for a in rig.attempts(job)] == ["a/main", "b/main"]


def views_of(rig) -> dict:
    return {view.route.id: view for view in rig.dispatcher.views()}


@requires_git
def test_route_with_error_streak_is_unavailable_until_cooldown(make_rig) -> None:
    rig = make_rig(routes=TWO_ROUTES)
    rig.runner.add(Step("error:one", exit_code=2), Step("error:two", exit_code=2))
    for _ in range(2):
        rig.submit(rig.plain(), route="a/main")
        rig.tick(3)
    until = rig.clock.now() + timedelta(seconds=dispatcher_module.ROUTE_ERROR_COOLDOWN_S)

    marked = views_of(rig)["a/main"]
    assert not marked.available
    assert marked.unavailable_reason == (
        f"2 errors in a row, retry after {format_local(until, rig.dispatcher.tz)}"
    )
    assert views_of(rig)["b/main"].available
    free = rig.submit(rig.plain())
    rig.tick(3)
    assert rig.job(free).route == "b/main"

    rig.clock.advance(timedelta(seconds=dispatcher_module.ROUTE_ERROR_COOLDOWN_S + 1))
    assert views_of(rig)["a/main"].available


@requires_git
def test_pinned_route_runs_despite_error_streak(make_rig) -> None:
    rig = make_rig(routes=TWO_ROUTES)
    rig.runner.add(Step("error:one", exit_code=2), Step("error:two", exit_code=2))
    for _ in range(2):
        rig.submit(rig.plain(), route="a/main")
        rig.tick(3)
    pinned = rig.submit(rig.plain(), route="a/main")
    rig.tick(3)

    done = rig.job(pinned)
    assert (done.status, done.route) == ("completed", "a/main")


@requires_git
def test_ok_between_errors_resets_streak(make_rig) -> None:
    rig = make_rig(routes=TWO_ROUTES)
    rig.runner.add(
        Step("error:one", exit_code=2),
        Step("ok:готово"),  # non-ascii on purpose
        Step("error:two", exit_code=2),
    )
    for _ in range(3):
        rig.submit(rig.plain(), route="a/main")
        rig.tick(3)

    assert views_of(rig)["a/main"].available


def test_error_streak_constants() -> None:
    assert dispatcher_module.ROUTE_ERROR_STREAK == 2
    assert dispatcher_module.ROUTE_ERROR_COOLDOWN_S == 1800


@requires_git
def test_model_unavailable_excludes_route_until_cooldown(make_rig) -> None:
    rig = make_rig(routes=TWO_ROUTES, model_unavailable_cooldown_s=600)
    rig.runner.add(Step("nomodel", exit_code=2))
    rig.submit(rig.plain(), route="a/main")
    rig.tick(2)
    until = rig.clock.now() + timedelta(seconds=600)

    marked = views_of(rig)["a/main"]
    assert not marked.available
    assert marked.unavailable_reason == (
        f"model unavailable, retry after {format_local(until, rig.dispatcher.tz)}"
    )
    assert views_of(rig)["b/main"].available
    assert rig.jobs.active_blocks(rig.clock.now()) == {}  # the pool is not blocked

    free = rig.submit(rig.plain())
    rig.tick(3)
    assert rig.job(free).route == "b/main"

    rig.clock.advance(timedelta(seconds=601))
    assert views_of(rig)["a/main"].available


@requires_git
def test_model_unavailable_before_timeout_excludes_route_until_cooldown(make_rig) -> None:
    rig = make_rig(routes=TWO_ROUTES, model_unavailable_cooldown_s=600)
    rig.runner.add(Step("nomodel", exit_code=None, timed_out=True))
    job = rig.submit(rig.plain(), route="a/main")
    rig.tick(2)
    until = rig.clock.now() + timedelta(seconds=600)

    (attempt,) = rig.attempts(job)
    assert attempt.outcome == "timeout"
    assert attempt.result is not None and attempt.result.model_unavailable
    marked = views_of(rig)["a/main"]
    assert not marked.available
    assert marked.unavailable_reason == (
        f"model unavailable, retry after {format_local(until, rig.dispatcher.tz)}"
    )
    assert views_of(rig)["b/main"].available

    free = rig.submit(rig.plain())
    rig.tick(3)
    assert rig.job(free).route == "b/main"

    rig.clock.advance(timedelta(seconds=601))
    assert views_of(rig)["a/main"].available


@requires_git
def test_model_unavailable_retries_on_next_route(make_rig) -> None:
    rig = make_rig(routes=TWO_ROUTES)
    rig.runner.add(Step("nomodel", exit_code=2))
    job = rig.submit(rig.plain())
    rig.tick(3)

    done = rig.job(job)
    assert (done.status, done.route) == ("completed", "b/main")
    first, second = rig.attempts(job)
    assert (first.route, first.outcome) == ("a/main", "error")
    assert first.result is not None and first.result.model_unavailable
    assert (second.route, second.outcome) == ("b/main", "ok")
    assert second.result is not None and not second.result.model_unavailable


@requires_git
def test_model_unavailable_pinned_route_fails_without_retry(make_rig) -> None:
    rig = make_rig(routes=TWO_ROUTES)
    rig.runner.add(Step("nomodel", exit_code=2))
    job = rig.submit(rig.plain(), route="a/main")
    rig.tick(2)

    done = rig.job(job)
    assert (done.status, done.error_code) == ("failed", "executor_error")
    assert "Model unavailable" in (done.error_message or "")
    assert len(rig.attempts(job)) == 1


@requires_git
def test_advice_shows_model_unavailable_reason(make_rig) -> None:
    from agent_router.service import advise

    rig = make_rig(routes=TWO_ROUTES)
    rig.runner.add(Step("nomodel", exit_code=2))
    rig.submit(rig.plain(), route="a/main")
    rig.tick(2)

    decision = advise(
        rig.config, rig.dispatcher.snapshot(), "standard", None, views=rig.dispatcher.views()
    )
    excluded = {item.route.id: item.reason for item in decision.excluded}
    assert excluded["a/main"].startswith("harness unavailable: model unavailable, retry after ")
    assert [choice.route.id for choice in decision.chosen] == ["b/main"]


# --- live progress of a running attempt -------------------------------------------------------


@requires_git
def test_progress_of_a_running_attempt(make_rig) -> None:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    rig = make_rig(executor)
    cwd = rig.plain()
    step = Step(last_signal="output", release=release)
    rig.runner.add(step)
    job = rig.submit(cwd)
    try:
        rig.tick()
        assert step.started.wait(5)
        log_dir = rig.home / "jobs" / job.id / "attempt-1"
        (log_dir / "stdout.log").write_text(f"first\n{cwd}\nlast line\n", encoding="utf-8")
        (log_dir / "stderr.log").write_text("warn\n", encoding="utf-8")
        rig.clock.advance(timedelta(seconds=30))

        progress = rig.dispatcher.progress(job.id)

        assert progress is not None
        assert progress["job"] == job.id
        assert progress["status"] == "running"
        attempt = progress["attempt"]
        assert (attempt["n"], attempt["route"]) == (1, "a/main")
        assert attempt["elapsed_s"] == 30  # the dispatcher's clock, advanced deterministically
        assert attempt["last_signal"] == "output"
        assert attempt["last_signal_age_s"] is not None
        assert attempt["idle_left_s"] is not None and 0 <= attempt["idle_left_s"] <= 600
        assert attempt["cap_left_s"] is not None and 0 <= attempt["cap_left_s"] <= 14400
        assert "last line" in attempt["tail"]
        assert "[stderr]\nwarn" in attempt["tail"]
        assert str(cwd) not in attempt["tail"]  # paths of the job directory are redacted
        assert "<dir>" in attempt["tail"]
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


@requires_git
def test_progress_without_a_running_attempt_or_job(make_rig) -> None:
    rig = make_rig()
    job = rig.submit(rig.plain())
    queued = rig.dispatcher.progress(job.id)
    assert queued == {"job": job.id, "status": "queued", "attempt": None}
    rig.tick(2)
    done = rig.dispatcher.progress(job.id)
    assert done == {"job": job.id, "status": "completed", "attempt": None}
    assert rig.dispatcher.progress("j_нет такой") is None  # non-ascii on purpose


@requires_git
def test_progress_idle_left_grows_no_shorter_than_the_limit(make_rig) -> None:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    rig = make_rig(executor, idle_timeout_s=120)
    step = Step(release=release)  # no signal seen yet: idle counts from the start
    rig.runner.add(step)
    job = rig.submit(rig.plain())
    try:
        rig.tick()
        assert step.started.wait(5)
        progress = rig.dispatcher.progress(job.id)
        assert progress is not None
        attempt = progress["attempt"]
        assert attempt["last_signal"] is None and attempt["last_signal_age_s"] is None
        assert 0 <= attempt["idle_left_s"] <= 120
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)
