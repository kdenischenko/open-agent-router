"""Job store: the JobStore protocol on each backend and schema migration 1 → 2."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_router import jobstore as jobstore_module
from agent_router.config import ConfigError, StorageSettings
from agent_router.jobs import Attempt, AttemptResult, Job, JobRequest, Rating
from agent_router.jobstore import GatewayUsage, SqliteJobStore, open_jobstore
from agent_router.policy import PoolBlock
from agent_router.store import CounterSample, Observation, SqliteStore, to_us

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def make_job(job_id: str, *, status: str = "queued", created_at: datetime = T0) -> Job:
    return Job(
        id=job_id,
        request=JobRequest("задание", Path("/work/проект")),  # non-ascii on purpose
        status=status,  # type: ignore[arg-type]
        tier="standard",
        tier_source="default",
        repo_root=None,
        created_at=created_at,
    )


def make_attempt(job_id: str, n: int, pool: str, started: datetime, **fields) -> Attempt:
    return Attempt(
        job_id=job_id, n=n, route=f"{pool}/model", pool=pool, started_at=started, **fields
    )


# --- schema -----------------------------------------------------------------------


def test_migration_from_1_keeps_observations(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        assert store.schema_version() == 1
        obs = Observation("claude-pro", "5h", 0.42, T0, T0 + timedelta(hours=3), "manual")
        store.add_observation(obs)
        SqliteJobStore(store)  # a fresh database runs all migrations up to the latest version
        assert store.schema_version() == 10
        assert store.last_observation("claude-pro", "5h") == obs
        SqliteJobStore(store)  # reopening does not touch the schema
        assert store.schema_version() == 10
    finally:
        store.close()


def test_open_jobstore_sqlite(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        jobstore = open_jobstore(StorageSettings(), tmp_path, store)
        assert isinstance(jobstore, SqliteJobStore)
        jobstore.close()  # connections belong to SqliteStore
        assert store.schema_version() == 10
        with pytest.raises(ConfigError):
            open_jobstore(StorageSettings(), tmp_path, object())  # type: ignore[arg-type]
    finally:
        store.close()


def test_open_jobstore_plugin_gets_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_router import plugins

    seen: list[tuple] = []

    class Plugin:
        @staticmethod
        def open_jobstore(options, home, store):
            seen.append((dict(options), home, store))
            return "third-party store"

    monkeypatch.setattr(plugins, "load_plugin", lambda group, name: Plugin)
    marker = object()
    settings = StorageSettings(backend="other", options={"dsn": "x"})
    opened = open_jobstore(settings, tmp_path, marker)
    assert opened == "third-party store"  # type: ignore[arg-type]
    assert seen == [({"dsn": "x"}, tmp_path, marker)]


# --- jobs ---------------------------------------------------------------------------


def test_job_roundtrip_with_all_fields(jobstore) -> None:
    job = Job(
        id="j_0123456789ab",
        request=JobRequest(
            prompt="сделай «это» и то\nвторая строка",  # non-ascii on purpose
            cwd=Path("/work/проект с пробелом"),  # non-ascii on purpose
            mode="write",
            tier="strong",
            kind="code",
            caller="claude-code",
            route="kimi/k3",
            timeout_s=900,
        ),
        status="awaiting_approval",
        tier="strong",
        tier_source="caller",
        repo_root=Path("/work/проект с пробелом"),  # non-ascii on purpose
        route="kimi/k3",
        reason="лучший запас",  # non-ascii on purpose
        approval_route="kimi/k3",
        approval_deadline=T0 + timedelta(minutes=10),
        approved_route="opencode/big",
        had_refusal=True,
        excluded_routes=frozenset({"a/b", "c/d"}),
        excluded_pools=frozenset({"claude-pro"}),
        cancel_requested=True,
        text="итог работы",  # non-ascii on purpose
        changed_files=("src/файл один.py", "README.md"),  # non-ascii on purpose
        violations=("head_moved",),
        error_code="timeout",
        error_message="не уложился",  # non-ascii on purpose
        classifier_tier="cheap",
        classifier_confidence=0.75,
        runner="local",
        created_at=T0,
        finished_at=T0 + timedelta(minutes=5, microseconds=7),
    )
    jobstore.create(job)
    assert jobstore.get(job.id) == job
    assert jobstore.get("j_missing") is None


def test_job_defaults_roundtrip_and_update(jobstore) -> None:
    job = make_job("j_a")
    jobstore.create(job)
    loaded = jobstore.get("j_a")
    assert loaded == job
    assert loaded.changed_files is None and loaded.violations == ()
    loaded.status = "running"
    loaded.route = "kimi/k3"
    loaded.changed_files = ()
    jobstore.update(loaded)
    again = jobstore.get("j_a")
    assert again.status == "running" and again.route == "kimi/k3" and again.changed_files == ()


def test_classifier_agreement_counts_caller_hints_only(jobstore) -> None:
    matched = make_job("j_1")
    matched.tier = "cheap"
    matched.tier_source = "caller"
    matched.classifier_tier = "cheap"
    mismatched = make_job("j_2")
    mismatched.tier = "strong"
    mismatched.tier_source = "caller"
    mismatched.classifier_tier = "cheap"
    defaulted = make_job("j_3")
    defaulted.classifier_tier = "standard"
    without_classifier = make_job("j_4")
    without_classifier.tier_source = "caller"
    for job in (matched, mismatched, defaulted, without_classifier):
        jobstore.create(job)

    assert jobstore.classifier_agreement() == (1, 2)
    assert jobstore.classifier_agreement(limit=1) == (0, 1)


def test_classifier_agreement_limit_takes_latest_jobs(jobstore) -> None:
    # job ids are random hex, so the latest job may sort first or last by id
    for job_id, minutes, classifier_tier in (("j_f", 0, "strong"), ("j_0", 10, "cheap")):
        job = make_job(job_id, created_at=T0 + timedelta(minutes=minutes))
        job.tier = "cheap"
        job.tier_source = "caller"
        job.classifier_tier = classifier_tier
        jobstore.create(job)

    assert jobstore.classifier_agreement(limit=1) == (1, 1)


def test_duplicate_id_is_rejected(jobstore) -> None:
    jobstore.create(make_job("j_a"))
    with pytest.raises(sqlite3.IntegrityError):  # duplicate id is rejected by the database
        jobstore.create(make_job("j_a"))


def test_list_newest_first_with_status_and_limit(jobstore) -> None:
    for index in range(5):
        status = "completed" if index % 2 else "queued"
        jobstore.create(
            make_job(f"j_{index}", status=status, created_at=T0 + timedelta(seconds=index))
        )
    assert [job.id for job in jobstore.list()] == ["j_4", "j_3", "j_2", "j_1", "j_0"]
    assert [job.id for job in jobstore.list(limit=2)] == ["j_4", "j_3"]
    assert [job.id for job in jobstore.list(status="completed")] == ["j_3", "j_1"]
    assert jobstore.list(status="failed") == []


def test_queued_order_by_creation_then_insertion(jobstore) -> None:
    jobstore.create(make_job("j_late", created_at=T0 + timedelta(seconds=5)))
    jobstore.create(make_job("j_same2", created_at=T0))  # same time: insertion order
    jobstore.create(make_job("j_same1", created_at=T0))
    jobstore.create(make_job("j_done", status="completed", created_at=T0))
    jobstore.create(make_job("j_early", created_at=T0 - timedelta(seconds=1)))
    assert [job.id for job in jobstore.queued(10)] == ["j_early", "j_same2", "j_same1", "j_late"]
    assert [job.id for job in jobstore.queued(2)] == ["j_early", "j_same2"]


def test_queued_pages_through_after(jobstore) -> None:
    ids = [f"j_{index}" for index in range(6)]
    for job_id in ids:
        jobstore.create(make_job(job_id))  # identical created_at timestamps
    first = jobstore.queued(2)
    assert [job.id for job in first] == ids[:2]
    # previous page's jobs changed status — their position in the order stays put
    for job in first:
        job.status = "running"
        jobstore.update(job)
    second = jobstore.queued(2, after=first[-1].id)
    assert [job.id for job in second] == ids[2:4]
    third = jobstore.queued(10, after=second[-1].id)
    assert [job.id for job in third] == ids[4:]
    assert jobstore.queued(10, after=ids[-1]) == []
    unknown_id = "j_неизвестная"  # non-ascii on purpose
    assert [job.id for job in jobstore.queued(10, after=unknown_id)] == ids[2:]


def test_count_waiting(jobstore) -> None:
    assert jobstore.count_waiting() == 0
    statuses = [
        "queued",
        "queued",
        "awaiting_approval",
        "running",
        "completed",
        "failed",
        "declined",
        "cancelled",
        "partial",
    ]
    for index, status in enumerate(statuses):
        jobstore.create(make_job(f"j_{index}", status=status))
    assert jobstore.count_waiting() == 3


# --- attempts -----------------------------------------------------------------------


def test_attempt_roundtrip_and_update(jobstore) -> None:
    jobstore.create(make_job("j_a"))
    changed_file = "src/файл один.py"  # non-ascii on purpose
    first = make_attempt(
        "j_a",
        1,
        "kimi-code",
        T0,
        head_before="abc123",
        tree_before={
            changed_file: (" M", 120, 1_700_000_000_123_456_789),
            "gone": ("D", -1, -1),
        },
        pid=4321,
        exe_name="kimi.exe",
    )
    jobstore.add_attempt(first)
    assert jobstore.attempts("j_a") == [first]
    first.finished_at = T0 + timedelta(seconds=90)
    first.outcome = "ok"
    first.duration_s = 90.5
    first.tokens_in = 1000
    first.tokens_out = 250
    first.cost_usd = 0.0125
    first.log_tail = "хвост вывода"  # non-ascii on purpose
    jobstore.update_attempt(first)
    second = make_attempt("j_a", 2, "opencode-go", T0 + timedelta(minutes=2))
    jobstore.add_attempt(second)
    assert jobstore.attempts("j_a") == [first, second]
    assert jobstore.attempts("j_missing") == []


def test_attempt_result_roundtrip(jobstore) -> None:
    jobstore.create(make_job("j_a"))
    first = make_attempt("j_a", 1, "kimi-code", T0)
    jobstore.add_attempt(first)
    assert jobstore.attempts("j_a")[0].result is None
    full = AttemptResult(
        "limit",
        text="ответ «итог»\nвторая строка",  # non-ascii on purpose
        error="упор в лимит",  # non-ascii on purpose
        limit_reset_at=T0 + timedelta(hours=2, microseconds=123456),
        changed=("src/файл один.py", "документ два.txt"),  # non-ascii on purpose
        head_moved=True,
        changes_unknown=True,
        survived=True,
    )
    first.result = full
    jobstore.update_attempt(first)
    assert jobstore.attempts("j_a")[0].result == full
    # directory outside git: changed is None
    plain = AttemptResult("ok", text="готово")  # non-ascii on purpose
    jobstore.add_attempt(make_attempt("j_a", 2, "kimi-code", T0, result=plain))
    assert [a.result for a in jobstore.attempts("j_a")] == [full, plain]
    first.result = None
    jobstore.update_attempt(first)
    assert jobstore.attempts("j_a")[0].result is None


def test_start_attempt_writes_both_or_nothing(jobstore) -> None:
    job = make_job("j_a")
    jobstore.create(job)
    attempt = make_attempt("j_a", 1, "kimi-code", T0)
    job.status = "running"
    jobstore.start_attempt(job, attempt)
    stored = jobstore.get("j_a")
    assert stored is not None and stored.status == "running"
    assert jobstore.attempts("j_a") == [attempt]
    job.status = "completed"
    with pytest.raises(sqlite3.IntegrityError):  # duplicate (job_id, n): the job row is unchanged
        jobstore.start_attempt(job, attempt)
    stored = jobstore.get("j_a")
    assert stored is not None and stored.status == "running"
    assert jobstore.attempts("j_a") == [attempt]
    ghost = make_job("j_missing", status="running")
    with pytest.raises(KeyError):  # no such job: no attempt row is left behind
        ghost_attempt = make_attempt("j_missing", 1, "kimi-code", T0)
        jobstore.start_attempt(ghost, ghost_attempt)
    assert jobstore.attempts("j_missing") == []


def test_schema_v2_database_migrates(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        store.migrate(2, jobstore_module._SCHEMA_V2)
        job = make_job("j_a", status="running")
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO jobs (id, status, created_at, finished_at, data)"
                " VALUES (?, ?, ?, NULL, ?)",
                (job.id, job.status, to_us(job.created_at), jobstore_module._job_data(job)),
            )
            conn.execute(
                "INSERT INTO attempts (job_id, n, route, pool, started_at, outcome)"
                " VALUES ('j_a', 1, 'kimi-code/model', 'kimi-code', ?, 'ok')",
                (to_us(T0),),
            )
        jobstore = SqliteJobStore(store)
        assert store.schema_version() == 10
        stored = jobstore.get("j_a")
        assert stored is not None and stored.status == "running"
        (attempt,) = jobstore.attempts("j_a")
        assert (attempt.outcome, attempt.result) == ("ok", None)
        attempt.result = AttemptResult("ok", text="готово")  # non-ascii on purpose
        jobstore.update_attempt(attempt)
        assert jobstore.attempts("j_a")[0].result == attempt.result
    finally:
        store.close()


@pytest.mark.parametrize(
    "raw",
    [
        "не json",  # non-ascii on purpose
        "[]",
        "{}",
        (
            '{"outcome": "нет такого", "text": "",'  # non-ascii on purpose
            ' "error": null, "limit_reset_at": null, "changed": null,'
            ' "head_moved": false, "changes_unknown": false, "survived": false}'
        ),
        (
            '{"outcome": "ok", "text": "", "error": null,'
            ' "limit_reset_at": "вчера", "changed": null,'  # non-ascii on purpose
            ' "head_moved": false, "changes_unknown": false, "survived": false}'
        ),
        '{"outcome": "ok", "text": "", "error": null, "limit_reset_at": null,'
        ' "changed": 5, "head_moved": false, "changes_unknown": false, "survived": false}',
        '{"outcome": "ok", "text": "", "error": null, "limit_reset_at": null,'
        ' "changed": ["a.txt", 5], "head_moved": false, "changes_unknown": false,'
        ' "survived": false}',
        '{"outcome": "ok", "text": 5, "error": null, "limit_reset_at": null,'
        ' "changed": null, "head_moved": false, "changes_unknown": false, "survived": false}',
        '{"outcome": "ok", "text": "", "error": [], "limit_reset_at": null,'
        ' "changed": null, "head_moved": false, "changes_unknown": false, "survived": false}',
        (
            '{"outcome": "ok", "text": "", "error": null, "limit_reset_at": null,'
            ' "changed": null, "head_moved": "да",'  # non-ascii on purpose
            ' "changes_unknown": false, "survived": false}'
        ),
        '{"outcome": "ok", "text": "", "error": null, "limit_reset_at": null,'
        ' "changed": null, "head_moved": false, "changes_unknown": false, "survived": 1}',
        '{"outcome": "ok", "text": "", "error": null, "limit_reset_at": 1.5,'
        ' "changed": null, "head_moved": false, "changes_unknown": false, "survived": false}',
    ],
)
def test_unreadable_result_reads_as_none(tmp_path: Path, raw: str) -> None:
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        jobstore = SqliteJobStore(store)
        ok_result = AttemptResult("ok", text="готово")  # non-ascii on purpose
        jobstore.add_attempt(make_attempt("j_a", 1, "kimi-code", T0, result=ok_result))
        with store.transaction() as conn:
            conn.execute("UPDATE attempts SET result = ?", (raw,))
        (attempt,) = jobstore.attempts("j_a")
        assert attempt.result is None and attempt.n == 1
    finally:
        store.close()


def test_usage_since_counts_pool_attempts_after_moment(jobstore) -> None:
    assert jobstore.usage_since("kimi-code", None) == GatewayUsage(0, 0.0, 0)
    jobstore.add_attempt(
        make_attempt("j_a", 1, "kimi-code", T0, duration_s=60.0, tokens_in=100, tokens_out=50)
    )
    jobstore.add_attempt(
        make_attempt("j_a", 2, "kimi-code", T0 + timedelta(hours=1), duration_s=30.0, tokens_in=10)
    )
    jobstore.add_attempt(
        make_attempt("j_b", 1, "kimi-code", T0 + timedelta(hours=2), duration_s=12.5)
    )
    jobstore.add_attempt(
        make_attempt(
            "j_c", 1, "opencode-go", T0 + timedelta(hours=2), duration_s=500.0, tokens_out=9
        )
    )
    assert jobstore.usage_since("kimi-code", None) == GatewayUsage(2, 102.5, 160)
    assert jobstore.usage_since("kimi-code", T0 + timedelta(minutes=30)) == GatewayUsage(
        2, 42.5, 10
    )
    assert jobstore.usage_since("kimi-code", T0 + timedelta(hours=2)) == GatewayUsage(1, 12.5, 0)
    assert jobstore.usage_since("kimi-code", T0 + timedelta(hours=3)) == GatewayUsage(0, 0.0, 0)
    assert jobstore.usage_since("opencode-go", None) == GatewayUsage(1, 500.0, 9)


def test_limit_hits_in_row(jobstore) -> None:
    assert jobstore.limit_hits_in_row("kimi-code") == 0
    outcomes = ["limit", "ok", "limit", "error", "limit"]
    for index, outcome in enumerate(outcomes):
        started = T0 + timedelta(minutes=index)
        jobstore.add_attempt(
            make_attempt(
                f"j_{index}",
                1,
                "kimi-code",
                started,
                outcome=outcome,
                finished_at=started + timedelta(seconds=30),
            )
        )
    jobstore.add_attempt(make_attempt("j_x", 1, "opencode-go", T0, outcome="limit", finished_at=T0))
    assert jobstore.limit_hits_in_row("kimi-code") == 2
    assert jobstore.limit_hits_in_row("opencode-go") == 1
    # an unfinished attempt is not part of the streak, whatever outcome it carries
    jobstore.add_attempt(
        make_attempt("j_u", 1, "kimi-code", T0 + timedelta(minutes=30), outcome="limit")
    )
    assert jobstore.limit_hits_in_row("kimi-code") == 2
    late = T0 + timedelta(hours=1)
    jobstore.add_attempt(make_attempt("j_y", 1, "kimi-code", late, outcome="ok", finished_at=late))
    assert jobstore.limit_hits_in_row("kimi-code") == 0


def test_limit_hits_counts_by_finish_time(jobstore) -> None:
    """Streak order is set by finish time, not by row insertion order."""
    t2, t3 = T0 + timedelta(minutes=2), T0 + timedelta(minutes=3)
    jobstore.add_attempt(make_attempt("j_a", 1, "kimi-code", T0, outcome="limit", finished_at=t3))
    jobstore.add_attempt(make_attempt("j_b", 1, "kimi-code", T0, outcome="ok", finished_at=t2))
    assert jobstore.limit_hits_in_row("kimi-code") == 1
    jobstore.add_attempt(make_attempt("j_c", 1, "opencode-go", T0, outcome="ok", finished_at=t3))
    jobstore.add_attempt(make_attempt("j_d", 1, "opencode-go", T0, outcome="limit", finished_at=t2))
    assert jobstore.limit_hits_in_row("opencode-go") == 0


def test_limit_hits_same_finish_time_breaks_tie_by_id(jobstore) -> None:
    jobstore.add_attempt(make_attempt("j_a", 1, "kimi-code", T0, outcome="ok", finished_at=T0))
    jobstore.add_attempt(make_attempt("j_b", 1, "kimi-code", T0, outcome="limit", finished_at=T0))
    assert jobstore.limit_hits_in_row("kimi-code") == 1
    jobstore.add_attempt(make_attempt("j_c", 1, "opencode-go", T0, outcome="limit", finished_at=T0))
    jobstore.add_attempt(make_attempt("j_d", 1, "opencode-go", T0, outcome="ok", finished_at=T0))
    assert jobstore.limit_hits_in_row("opencode-go") == 0


# --- pool blocks --------------------------------------------------------------------


def test_block_expires_by_time(jobstore) -> None:
    until = T0 + timedelta(minutes=30)
    block = PoolBlock("kimi-code", until, "limit hit")
    jobstore.add_block(block, "j_a")
    assert jobstore.active_blocks(T0) == {"kimi-code": block}
    assert jobstore.active_blocks(until - timedelta(microseconds=1)) == {"kimi-code": block}
    assert jobstore.active_blocks(until) == {}
    assert jobstore.active_blocks(until + timedelta(hours=1)) == {}


def test_new_block_replaces_old_one(jobstore) -> None:
    first_block = PoolBlock("kimi-code", T0 + timedelta(minutes=30), "first")
    jobstore.add_block(first_block, None)
    longer = PoolBlock("kimi-code", T0 + timedelta(hours=1), "second")
    assert jobstore.add_block(longer, "j_b") == longer
    assert jobstore.active_blocks(T0) == {"kimi-code": longer}


def test_add_block_keeps_later_deadline(jobstore) -> None:
    reset_known = "reset known"
    long = PoolBlock("kimi-code", T0 + timedelta(hours=5), reset_known)
    assert jobstore.add_block(long, "j_a") == long
    short = PoolBlock("kimi-code", T0 + timedelta(seconds=60), "fallback")
    assert jobstore.add_block(short, "j_b") == long
    assert jobstore.active_blocks(T0) == {"kimi-code": long}
    same = PoolBlock("kimi-code", T0 + timedelta(hours=5), "same deadline")
    assert jobstore.add_block(same, "j_c") == long
    longer = PoolBlock("kimi-code", T0 + timedelta(hours=6), "later")
    assert jobstore.add_block(longer, "j_d") == longer
    assert jobstore.active_blocks(T0) == {"kimi-code": longer}


def test_clear_block(jobstore) -> None:
    jobstore.add_block(PoolBlock("kimi-code", T0 + timedelta(hours=1), "limit hit"), None)
    other = PoolBlock("opencode-go", T0 + timedelta(hours=1), "limit hit")
    jobstore.add_block(other, None)
    jobstore.clear_block("kimi-code")
    jobstore.clear_block("no-such-pool")
    assert jobstore.active_blocks(T0) == {"opencode-go": other}


def test_expire_blocks_removes_only_expired(jobstore) -> None:
    jobstore.add_block(PoolBlock("kimi-code", T0 + timedelta(minutes=10), "limit hit"), None)
    jobstore.add_block(PoolBlock("claude-pro", T0 + timedelta(minutes=5), "limit hit"), None)
    alive = PoolBlock("opencode-go", T0 + timedelta(hours=1), "limit hit")
    jobstore.add_block(alive, None)
    assert jobstore.expire_blocks(T0) == []
    assert jobstore.expire_blocks(T0 + timedelta(minutes=10)) == ["claude-pro", "kimi-code"]
    assert jobstore.expire_blocks(T0 + timedelta(minutes=10)) == []
    assert jobstore.active_blocks(T0) == {"opencode-go": alive}


# --- schema 4: attempt process start time -------------------------------------------


def test_schema_3_database_opens_as_4(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        store.migrate(2, jobstore_module._SCHEMA_V2)
        store.migrate(3, ["ALTER TABLE attempts ADD COLUMN result TEXT"])
        assert store.schema_version() == 3
        job = make_job("j_a", status="running")
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO jobs (id, status, created_at, finished_at, data)"
                " VALUES (?, ?, ?, NULL, ?)",
                (job.id, job.status, to_us(job.created_at), jobstore_module._job_data(job)),
            )
            conn.execute(
                "INSERT INTO attempts (job_id, n, route, pool, started_at, pid, exe_name)"
                " VALUES ('j_a', 1, 'kimi-code/model', 'kimi-code', ?, 4321, 'kimi.exe')",
                (to_us(T0),),
            )
        jobstore = SqliteJobStore(store)
        assert store.schema_version() == 10
        (attempt,) = jobstore.attempts("j_a")
        assert (attempt.pid, attempt.exe_name, attempt.proc_started) == (4321, "kimi.exe", None)
        assert attempt.started_at == T0 and attempt.finished_at is None and attempt.result is None
        attempt.proc_started = 1731000000.25
        jobstore.update_attempt(attempt)
        assert jobstore.attempts("j_a")[0].proc_started == 1731000000.25
        SqliteJobStore(store)  # reopening does not touch the schema
        assert store.schema_version() == 10
    finally:
        store.close()


def test_schema_5_database_opens_as_10(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        store.migrate(2, jobstore_module._SCHEMA_V2)
        store.migrate(3, ["ALTER TABLE attempts ADD COLUMN result TEXT"])
        store.migrate(4, ["ALTER TABLE attempts ADD COLUMN proc_started REAL"])
        store.migrate(5, jobstore_module._SCHEMA_V5)
        assert store.schema_version() == 5
        jobstore = SqliteJobStore(store)
        assert store.schema_version() == 10
        tables = {
            row[0]
            for row in store.connection().execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "settings" in tables
        jobstore.close()
        SqliteJobStore(store)  # reopening does not touch the schema
        assert store.schema_version() == 10
    finally:
        store.close()


def test_schema_6_database_opens_as_10(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        store.migrate(2, jobstore_module._SCHEMA_V2)
        store.migrate(3, ["ALTER TABLE attempts ADD COLUMN result TEXT"])
        store.migrate(4, ["ALTER TABLE attempts ADD COLUMN proc_started REAL"])
        store.migrate(5, jobstore_module._SCHEMA_V5)
        store.migrate(6, jobstore_module._SCHEMA_V6)
        assert store.schema_version() == 6
        job = make_job("j_a", status="running")
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO jobs (id, status, created_at, finished_at, data)"
                " VALUES (?, ?, ?, NULL, ?)",
                (job.id, job.status, to_us(job.created_at), jobstore_module._job_data(job)),
            )
            conn.execute(
                "INSERT INTO attempts (job_id, n, route, pool, started_at, outcome)"
                " VALUES ('j_a', 1, 'kimi-code/model', 'kimi-code', ?, 'ok')",
                (to_us(T0),),
            )
        jobstore = SqliteJobStore(store)
        assert store.schema_version() == 10
        (attempt,) = jobstore.attempts("j_a")
        assert attempt.decision is None and attempt.outcome == "ok"
        jobstore.close()
        SqliteJobStore(store)  # reopening does not touch the schema
        assert store.schema_version() == 10
    finally:
        store.close()


def test_schema_7_database_opens_as_10(tmp_path: Path) -> None:
    """A version-7 database opens at 10: the ratings table appears."""
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        store.migrate(2, jobstore_module._SCHEMA_V2)
        store.migrate(3, ["ALTER TABLE attempts ADD COLUMN result TEXT"])
        store.migrate(4, ["ALTER TABLE attempts ADD COLUMN proc_started REAL"])
        store.migrate(5, jobstore_module._SCHEMA_V5)
        store.migrate(6, jobstore_module._SCHEMA_V6)
        store.migrate(7, jobstore_module._SCHEMA_V7)
        assert store.schema_version() == 7
        job = make_job("j_a", status="completed")
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO jobs (id, status, created_at, finished_at, data)"
                " VALUES (?, ?, ?, NULL, ?)",
                (job.id, job.status, to_us(job.created_at), jobstore_module._job_data(job)),
            )
        jobstore = SqliteJobStore(store)
        assert store.schema_version() == 10
        tables = {
            row[0]
            for row in store.connection().execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "ratings" in tables
        assert jobstore.last_rating("j_a") is None
        jobstore.close()
        SqliteJobStore(store)  # reopening does not touch the schema
        assert store.schema_version() == 10
    finally:
        store.close()


def test_schema_8_database_opens_as_10(tmp_path: Path) -> None:
    """A version-8 database opens at version 10: old attempts and observations
    can be read with empty new fields."""
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        store.migrate(2, jobstore_module._SCHEMA_V2)
        store.migrate(3, ["ALTER TABLE attempts ADD COLUMN result TEXT"])
        store.migrate(4, ["ALTER TABLE attempts ADD COLUMN proc_started REAL"])
        store.migrate(5, jobstore_module._SCHEMA_V5)
        store.migrate(6, jobstore_module._SCHEMA_V6)
        store.migrate(7, jobstore_module._SCHEMA_V7)
        store.migrate(8, jobstore_module._SCHEMA_V8)
        assert store.schema_version() == 8
        store.add_counter_sample(CounterSample("opencode-go", 1.5, T0))
        job = make_job("j_a", status="completed")
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO jobs (id, status, created_at, finished_at, data)"
                " VALUES (?, ?, ?, NULL, ?)",
                (job.id, job.status, to_us(job.created_at), jobstore_module._job_data(job)),
            )
            conn.execute(
                "INSERT INTO attempts (job_id, n, route, pool, started_at, outcome, cost_usd)"
                " VALUES ('j_a', 1, 'opencode-cli/glm-5.3', 'opencode-go', ?, 'ok', 0.25)",
                (to_us(T0),),
            )
        jobstore = SqliteJobStore(store)
        assert store.schema_version() == 10
        (attempt,) = jobstore.attempts("j_a")
        assert attempt.cost_usd == 0.25 and attempt.cost_estimated is False
        sample = store.last_counter("opencode-go")
        assert sample is not None and sample.scale_hash is None
        store.add_counter_sample(CounterSample("opencode-go", 2.0, T0 + timedelta(hours=1), "abc"))
        assert store.last_counter("opencode-go").scale_hash == "abc"
        attempt.cost_usd, attempt.cost_estimated = 0.42, True
        jobstore.update_attempt(attempt)
        assert jobstore.attempts("j_a")[0].cost_estimated is True
        SqliteJobStore(store)  # reopening does not touch the schema
        assert store.schema_version() == 10
    finally:
        store.close()


def test_old_attempt_rows_read_with_null_decision(tmp_path: Path) -> None:
    """Attempt rows from before migration 7 still read: missing `decision` becomes None."""
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        store.migrate(2, jobstore_module._SCHEMA_V2)
        store.migrate(3, ["ALTER TABLE attempts ADD COLUMN result TEXT"])
        store.migrate(4, ["ALTER TABLE attempts ADD COLUMN proc_started REAL"])
        store.migrate(5, jobstore_module._SCHEMA_V5)
        store.migrate(6, jobstore_module._SCHEMA_V6)
        job = make_job("j_a", status="running")
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO jobs (id, status, created_at, finished_at, data)"
                " VALUES (?, ?, ?, NULL, ?)",
                (job.id, job.status, to_us(job.created_at), jobstore_module._job_data(job)),
            )
            conn.execute(
                "INSERT INTO attempts (job_id, n, route, pool, started_at, outcome)"
                " VALUES ('j_a', 1, 'kimi-code/model', 'kimi-code', ?, 'ok')",
                (to_us(T0),),
            )
        jobstore = SqliteJobStore(store)
        (attempt,) = jobstore.attempts("j_a")
        assert attempt.decision is None
        attempt.decision = {"route": "kimi-code/модель", "model": "модель"}  # non-ascii on purpose
        jobstore.update_attempt(attempt)
        assert jobstore.attempts("j_a")[0].decision == {
            "route": "kimi-code/модель",  # non-ascii on purpose
            "model": "модель",  # non-ascii on purpose
        }
    finally:
        store.close()


def test_old_job_json_without_prompt_fields_reads(tmp_path: Path) -> None:
    """A job written before `prompt_sha256`/`prompt_chars` existed still reads via `.get()`."""
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        store.migrate(2, jobstore_module._SCHEMA_V2)
        store.migrate(3, ["ALTER TABLE attempts ADD COLUMN result TEXT"])
        store.migrate(4, ["ALTER TABLE attempts ADD COLUMN proc_started REAL"])
        store.migrate(5, jobstore_module._SCHEMA_V5)
        store.migrate(6, jobstore_module._SCHEMA_V6)
        old_data = {
            "request": {
                "prompt": "задание",  # non-ascii on purpose
                "cwd": "/work/проект",  # non-ascii on purpose
                "mode": "read",
                "tier": None,
                "kind": None,
                "caller": None,
                "route": None,
                "timeout_s": None,
            },
            "tier": "standard",
            "tier_source": "default",
            "repo_root": None,
            "route": None,
            "reason": None,
            "approval_route": None,
            "approval_deadline": None,
            "approved_route": None,
            "had_refusal": False,
            "excluded_routes": [],
            "excluded_pools": [],
            "cancel_requested": False,
            "text": None,
            "changed_files": None,
            "violations": [],
            "error_code": None,
            "error_message": None,
            "classifier_tier": None,
            "classifier_confidence": None,
            "runner": "local",
        }
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO jobs (id, status, created_at, finished_at, data)"
                " VALUES ('j_old', 'queued', ?, NULL, ?)",
                (to_us(T0), json.dumps(old_data, ensure_ascii=False)),
            )
        jobstore = SqliteJobStore(store)
        loaded = jobstore.get("j_old")
        assert loaded is not None
        assert loaded.prompt_sha256 is None and loaded.prompt_chars is None
    finally:
        store.close()


def test_old_job_json_without_tier_pending_reads(tmp_path: Path) -> None:
    """A job written before the `tier_pending` key existed reads with `tier_pending = False`."""
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        jobstore = SqliteJobStore(store)
        old_data = (
            '{"request": {"prompt": "задание", "cwd": "/work/проект",'  # non-ascii on purpose
            ' "mode": "read", "tier": null, "kind": null, "caller": null,'
            ' "route": null, "timeout_s": null}, "tier": "standard",'
            ' "tier_source": "default", "repo_root": null, "route": null,'
            ' "reason": null, "approval_route": null, "approval_deadline": null,'
            ' "approved_route": null, "had_refusal": false, "excluded_routes": [],'
            ' "excluded_pools": [], "cancel_requested": false, "text": null,'
            ' "changed_files": null, "violations": [], "error_code": null,'
            ' "error_message": null, "classifier_tier": null, "classifier_confidence": null,'
            ' "runner": "local", "prompt_sha256": "abc", "prompt_chars": 7}'
        )
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO jobs (id, status, created_at, finished_at, data)"
                " VALUES ('j_old', 'queued', ?, NULL, ?)",
                (to_us(T0), old_data),
            )
        loaded = jobstore.get("j_old")
        assert loaded is not None
        assert loaded.tier_pending is False
        assert loaded.tier_source == "default"
        [queued] = jobstore.queued(10)
        assert queued.id == "j_old" and queued.tier_pending is False
    finally:
        store.close()


def test_tier_pending_and_classifier_source_roundtrip(jobstore) -> None:
    job = make_job("j_a")
    job.tier_pending = True
    jobstore.create(job)
    loaded = jobstore.get("j_a")
    assert loaded is not None and loaded.tier_pending is True

    loaded.tier = "cheap"
    loaded.tier_source = "classifier"
    loaded.tier_pending = False
    jobstore.update(loaded)
    again = jobstore.get("j_a")
    assert again is not None
    assert (again.tier, again.tier_source, again.tier_pending) == ("cheap", "classifier", False)


# --- ratings ------------------------------------------------------------------------


def test_rating_roundtrip_last_wins(jobstore) -> None:
    """Ratings are stored as rows; the last one wins, including its empty should_tier."""
    jobstore.create(make_job("j_a", status="completed"))
    first = Rating(
        job_id="j_a",
        value="good",
        should_tier="strong",
        by="agent-a",
        note_sha256="хэш заметки",  # non-ascii on purpose
        note_chars=12,
        at=T0,
    )
    jobstore.add_rating(first)
    assert jobstore.last_rating("j_a") == first
    second = Rating(
        job_id="j_a",
        value="bad",
        should_tier=None,
        by=None,
        note_sha256=None,
        note_chars=None,
        at=T0 + timedelta(minutes=1),
    )
    jobstore.add_rating(second)
    assert jobstore.last_rating("j_a") == second
    assert jobstore.last_rating("j_missing") is None


def test_rating_note_text_is_not_stored(tmp_path: Path) -> None:
    """The rating row holds no note text — only its hash and length."""
    store = SqliteStore(tmp_path / "agent-router.sqlite")
    try:
        jobstore = SqliteJobStore(store)
        jobstore.create(make_job("j_a", status="completed"))
        secret = "заметка про C:/Users/<user>/секрет"  # non-ascii on purpose
        jobstore.add_rating(
            Rating(
                job_id="j_a",
                value="good",
                should_tier=None,
                by=None,
                note_sha256=hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                note_chars=len(secret),
                at=T0,
            )
        )
        row = (
            store.connection()
            .execute(
                "SELECT job_id, value, should_tier, by, note_sha256, note_chars, at"
                " FROM ratings WHERE job_id = 'j_a'"
            )
            .fetchone()
        )
        assert row is not None
        assert secret not in " ".join(str(part) for part in row)
    finally:
        store.close()


def test_attempt_proc_started_roundtrip(jobstore) -> None:
    jobstore.create(make_job("j_a"))
    first = make_attempt(
        "j_a", 1, "kimi-code", T0, pid=4321, exe_name="kimi.exe", proc_started=1731000000.125
    )
    jobstore.add_attempt(first)
    assert jobstore.attempts("j_a") == [first]
    job = make_job("j_b")
    jobstore.create(job)
    job.status = "running"
    second = make_attempt("j_b", 1, "kimi-code", T0)
    jobstore.start_attempt(job, second)
    assert jobstore.attempts("j_b")[0].proc_started is None
    second.pid, second.exe_name, second.proc_started = 77, "node", 1731000111.5
    jobstore.update_attempt(second)
    assert jobstore.attempts("j_b") == [second]
    first.proc_started = None
    jobstore.update_attempt(first)
    assert jobstore.attempts("j_a")[0].proc_started is None


def test_route_errors_in_row_counts_after_last_ok(jobstore) -> None:
    route = "kimi-code/model"
    assert jobstore.route_errors_in_row(route) == (0, None)
    outcomes = ["error", "ok", "error", "limit", "error"]
    for index, outcome in enumerate(outcomes):
        started = T0 + timedelta(minutes=index)
        jobstore.add_attempt(
            make_attempt(
                f"j_{index}",
                1,
                "kimi-code",
                started,
                outcome=outcome,
                finished_at=started + timedelta(seconds=30),
            )
        )
    jobstore.add_attempt(make_attempt("j_x", 1, "opencode-go", T0, outcome="error", finished_at=T0))
    last = T0 + timedelta(minutes=4, seconds=30)
    assert jobstore.route_errors_in_row(route) == (2, last)
    assert jobstore.route_errors_in_row("opencode-go/model") == (1, T0)
    # an unfinished attempt is not part of the streak
    jobstore.add_attempt(
        make_attempt("j_u", 1, "kimi-code", T0 + timedelta(minutes=30), outcome="error")
    )
    assert jobstore.route_errors_in_row(route) == (2, last)
    late = T0 + timedelta(hours=1)
    jobstore.add_attempt(make_attempt("j_y", 1, "kimi-code", late, outcome="ok", finished_at=late))
    assert jobstore.route_errors_in_row(route) == (0, None)


def test_route_errors_in_row_orders_by_finish_time(jobstore) -> None:
    t2, t3 = T0 + timedelta(minutes=2), T0 + timedelta(minutes=3)
    jobstore.add_attempt(make_attempt("j_a", 1, "kimi-code", T0, outcome="error", finished_at=t3))
    jobstore.add_attempt(make_attempt("j_b", 1, "kimi-code", T0, outcome="ok", finished_at=t2))
    assert jobstore.route_errors_in_row("kimi-code/model") == (1, t3)
