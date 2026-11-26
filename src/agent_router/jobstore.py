"""Job store: the JobStore protocol and a SQLite implementation (schema version 8)."""

from __future__ import annotations

import builtins
import contextlib
import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from agent_router.clock import to_utc
from agent_router.config import ConfigError, StorageSettings
from agent_router.jobs import (
    OUTCOMES,
    Attempt,
    AttemptResult,
    Job,
    JobRequest,
    Rating,
    Status,
    Vote,
    VoteRow,
)
from agent_router.policy import PoolBlock
from agent_router.spend import SpendBucket, SpendEntry, SpendTotal
from agent_router.store import STORAGE_GROUP, SqliteStore, Store, from_us, plugin_call, to_us

_JOB_DIR_NAME = re.compile(r"j_[0-9a-f]{12}")


@dataclass(frozen=True)
class GatewayUsage:
    """Pool usage through the gateway: jobs, seconds, attempt and proxy-request tokens."""

    jobs: int
    seconds: float
    tokens: int
    proxy_tokens: int = 0


class JobStore(Protocol):
    """The core knows only this protocol."""

    def create(self, job: Job) -> None: ...

    def get(self, job_id: str) -> Job | None: ...

    def update(self, job: Job) -> None: ...

    def list(self, status: Status | None = None, limit: int = 50) -> builtins.list[Job]:
        """Jobs, newest first."""
        ...

    def search(
        self,
        *,
        status: Status | None = None,
        route: str | None = None,
        caller: str | None = None,
        q: str | None = None,
        sort: str = "newest",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[builtins.list[Job], int]:
        """One sorted page of the jobs matching every given filter, with the total match count."""
        ...

    def purge(
        self, before: datetime, jobs_dir: Path, dry_run: bool = False
    ) -> tuple[builtins.list[str], builtins.list[Path]]:
        """Delete old terminal jobs and orphan directories; return ids and removal failures."""
        ...

    def queued(self, limit: int, after: str | None = None) -> builtins.list[Job]:
        """Queued jobs in creation order; `after` is the id of the last one seen."""
        ...

    def classifier_agreement(self, limit: int = 500) -> tuple[int, int]: ...

    def count_waiting(self) -> int:
        """How many jobs are in `queued` and `awaiting_approval`."""
        ...

    def add_attempt(self, a: Attempt) -> None: ...

    def start_attempt(self, job: Job, a: Attempt) -> None:
        """The attempt row and the job row are one transaction."""
        ...

    def update_attempt(self, a: Attempt, spend: SpendEntry | None = None) -> bool: ...

    def attempts(self, job_id: str) -> builtins.list[Attempt]: ...

    def add_rating(self, r: Rating) -> None: ...

    def last_rating(self, job_id: str) -> Rating | None:
        """Latest rating for the whole job; None when there are none."""
        ...

    def add_vote(self, job_id: str, vote: Vote, at: datetime) -> None: ...

    def votes(self, job_id: str) -> builtins.list[Vote]: ...

    def votes_since(
        self, since: datetime, offline: bool | None = None
    ) -> builtins.list[VoteRow]: ...

    def prompts_since(self, since: datetime) -> builtins.list[tuple[str, str]]: ...

    def add_block(self, block: PoolBlock, job_id: str | None) -> PoolBlock:
        """Blocks the pool; an active block's deadline is never shortened. Returns the active
        block."""
        ...

    def active_blocks(self, now: datetime) -> dict[str, PoolBlock]: ...

    def clear_block(self, pool: str) -> None: ...

    def expire_blocks(self, now: datetime) -> builtins.list[str]:
        """Drops expired blocks; returns the pools that reopened."""
        ...

    def close(self) -> None: ...

    def usage_since(self, pool: str, since: datetime | None) -> GatewayUsage: ...

    def add_spend(self, e: SpendEntry) -> bool: ...

    def spend_since(self, pool: str, since: datetime | None) -> SpendTotal: ...

    def spend_buckets(
        self, pool: str, starts: Sequence[datetime], until: datetime
    ) -> builtins.list[SpendBucket]:
        """Non-empty buckets between consecutive starts; the end is exclusive."""
        ...

    def limit_hits_in_row(self, pool: str) -> int:
        """Limit-hit attempts finished after the pool's last successful attempt."""
        ...

    def route_errors_in_row(self, route: str) -> tuple[int, datetime | None]:
        """Error attempts finished after the route's last successful attempt, and the last one's
        finish time; None when the count is 0."""
        ...


_SCHEMA_V2 = (
    """
    CREATE TABLE jobs (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        finished_at INTEGER,
        data TEXT NOT NULL
    )
    """,
    "CREATE INDEX jobs_status ON jobs (status, created_at, seq)",
    "CREATE INDEX jobs_created ON jobs (created_at, seq)",
    """
    CREATE TABLE attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        n INTEGER NOT NULL,
        route TEXT NOT NULL,
        pool TEXT NOT NULL,
        started_at INTEGER NOT NULL,
        finished_at INTEGER,
        outcome TEXT,
        duration_s REAL,
        tokens_in INTEGER,
        tokens_out INTEGER,
        cost_usd REAL,
        head_before TEXT,
        tree_before TEXT,
        log_tail TEXT,
        pid INTEGER,
        exe_name TEXT,
        UNIQUE (job_id, n)
    )
    """,
    "CREATE INDEX attempts_pool ON attempts (pool, started_at)",
    """
    CREATE TABLE pool_blocks (
        pool TEXT PRIMARY KEY,
        until INTEGER NOT NULL,
        reason TEXT NOT NULL,
        job_id TEXT
    )
    """,
)

_SCHEMA_V5 = (
    "ALTER TABLE attempts ADD COLUMN spend_source TEXT",
    """
    CREATE TABLE spend (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pool TEXT NOT NULL,
        at INTEGER NOT NULL,
        source TEXT NOT NULL,
        route TEXT NOT NULL,
        tokens_in INTEGER,
        tokens_out INTEGER,
        cost_usd REAL,
        estimated INTEGER NOT NULL,
        prices_version TEXT,
        ref_id TEXT NOT NULL,
        ref_n INTEGER NOT NULL,
        UNIQUE (source, ref_id, ref_n)
    )
    """,
    "CREATE INDEX spend_pool_at ON spend (pool, at)",
)

_SCHEMA_V6 = ("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",)

_SCHEMA_V7 = ("ALTER TABLE attempts ADD COLUMN decision TEXT",)

_SCHEMA_V8 = (
    """
    CREATE TABLE ratings (
        id INTEGER PRIMARY KEY,
        job_id TEXT NOT NULL,
        value TEXT NOT NULL,
        should_tier TEXT,
        by TEXT,
        note_sha256 TEXT,
        note_chars INTEGER,
        at TEXT NOT NULL
    )
    """,
    "CREATE INDEX ratings_job_id ON ratings (job_id, id)",
)

_SCHEMA_V9 = (
    "ALTER TABLE counter_samples ADD COLUMN scale_hash TEXT",
    "ALTER TABLE attempts ADD COLUMN cost_estimated INTEGER",
)

_SCHEMA_V10 = (
    """
    CREATE TABLE classifier_votes (
        id INTEGER PRIMARY KEY,
        job_id TEXT NOT NULL,
        classifier_id TEXT NOT NULL,
        model TEXT NOT NULL,
        tier TEXT,
        confidence REAL,
        latency_ms INTEGER,
        error TEXT,
        offline INTEGER NOT NULL DEFAULT 0,
        at TEXT NOT NULL
    )
    """,
    "CREATE INDEX classifier_votes_job ON classifier_votes (job_id)",
)


def migrate_jobs_schema(store: Store) -> None:
    """Migrates the SQLite schema to the latest version; called by both the job store and the
    gateway."""
    store.migrate(2, _SCHEMA_V2)
    store.migrate(3, ["ALTER TABLE attempts ADD COLUMN result TEXT"])
    store.migrate(4, ["ALTER TABLE attempts ADD COLUMN proc_started REAL"])
    store.migrate(5, _SCHEMA_V5)
    store.migrate(6, _SCHEMA_V6)
    store.migrate(7, _SCHEMA_V7)
    store.migrate(8, _SCHEMA_V8)
    store.migrate(9, _SCHEMA_V9)
    store.migrate(10, _SCHEMA_V10)


_ATTEMPT_COLUMNS = (
    "job_id, n, route, pool, started_at, finished_at, outcome, duration_s, tokens_in,"
    " tokens_out, cost_usd, head_before, tree_before, log_tail, pid, exe_name, proc_started,"
    " result, spend_source, decision, cost_estimated"
)

_SPEND_COLUMNS = (
    "pool, at, source, route, tokens_in, tokens_out, cost_usd, estimated, prices_version,"
    " ref_id, ref_n"
)


def _us(moment: datetime | None) -> int | None:
    return None if moment is None else to_us(moment)


def _moment(value: int | None) -> datetime | None:
    return None if value is None else from_us(value)


def _result_data(result: AttemptResult | None) -> str | None:
    if result is None:
        return None
    return json.dumps(
        {
            "outcome": result.outcome,
            "text": result.text,
            "error": result.error,
            "limit_reset_at": _us(result.limit_reset_at),
            "changed": None if result.changed is None else list(result.changed),
            "head_moved": result.head_moved,
            "changes_unknown": result.changes_unknown,
            "survived": result.survived,
            "model_unavailable": result.model_unavailable,
        },
        ensure_ascii=False,
    )


def _attempt_result(raw: str | None) -> AttemptResult | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        outcome = data["outcome"]
        reset = data["limit_reset_at"]
        changed = data["changed"]
        if outcome not in OUTCOMES:
            return None
        if reset is not None and (isinstance(reset, bool) or not isinstance(reset, int)):
            return None
        if changed is not None and not (
            isinstance(changed, list) and all(isinstance(name, str) for name in changed)
        ):
            return None
        flags = (data["head_moved"], data["changes_unknown"], data["survived"])
        if not all(isinstance(flag, bool) for flag in flags):
            return None
        unavailable = data.get("model_unavailable", False)
        if not isinstance(unavailable, bool):
            return None
        if not all(isinstance(data[key], (str, type(None))) for key in ("text", "error")):
            return None
        return AttemptResult(
            outcome=outcome,
            text=data["text"],
            error=data["error"],
            limit_reset_at=None if reset is None else from_us(reset),
            changed=None if changed is None else tuple(changed),
            head_moved=data["head_moved"],
            changes_unknown=data["changes_unknown"],
            survived=data["survived"],
            model_unavailable=unavailable,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OverflowError):
        return None


def _decision_data(decision: dict | None) -> str | None:
    if decision is None:
        return None
    return json.dumps(decision, ensure_ascii=False)


def _decision(raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _job_data(job: Job) -> str:
    request = job.request
    data = {
        "request": {
            "prompt": request.prompt,
            "cwd": str(request.cwd),
            "mode": request.mode,
            "tier": request.tier,
            "kind": request.kind,
            "caller": request.caller,
            "route": request.route,
            "timeout_s": request.timeout_s,
            "excluded_pools": sorted(request.excluded_pools),
        },
        "tier": job.tier,
        "tier_source": job.tier_source,
        "repo_root": None if job.repo_root is None else str(job.repo_root),
        "route": job.route,
        "reason": job.reason,
        "approval_route": job.approval_route,
        "approval_deadline": _us(job.approval_deadline),
        "approved_route": job.approved_route,
        "had_refusal": job.had_refusal,
        "excluded_routes": sorted(job.excluded_routes),
        "excluded_pools": sorted(job.excluded_pools),
        "cancel_requested": job.cancel_requested,
        "text": job.text,
        "changed_files": None if job.changed_files is None else list(job.changed_files),
        "violations": list(job.violations),
        "error_code": job.error_code,
        "error_message": job.error_message,
        "classifier_tier": job.classifier_tier,
        "classifier_confidence": job.classifier_confidence,
        "runner": job.runner,
        "prompt_sha256": job.prompt_sha256,
        "prompt_chars": job.prompt_chars,
        "tier_pending": job.tier_pending,
    }
    return json.dumps(data, ensure_ascii=False)


def _job(row: Sequence[Any]) -> Job:
    job_id, status, created_at, finished_at, raw = row
    data = json.loads(raw)
    request = data["request"]
    changed = data["changed_files"]
    return Job(
        id=job_id,
        request=JobRequest(
            prompt=request["prompt"],
            cwd=Path(request["cwd"]),
            mode=request["mode"],
            tier=request["tier"],
            kind=request["kind"],
            caller=request["caller"],
            route=request["route"],
            timeout_s=request["timeout_s"],
            excluded_pools=frozenset(request.get("excluded_pools", [])),
        ),
        status=status,
        tier=data["tier"],
        tier_source=data["tier_source"],
        repo_root=None if data["repo_root"] is None else Path(data["repo_root"]),
        route=data["route"],
        reason=data["reason"],
        approval_route=data["approval_route"],
        approval_deadline=_moment(data["approval_deadline"]),
        approved_route=data["approved_route"],
        had_refusal=bool(data["had_refusal"]),
        excluded_routes=frozenset(data["excluded_routes"]),
        excluded_pools=frozenset(data["excluded_pools"]),
        cancel_requested=bool(data["cancel_requested"]),
        text=data["text"],
        changed_files=None if changed is None else tuple(changed),
        violations=tuple(data["violations"]),
        error_code=data["error_code"],
        error_message=data["error_message"],
        classifier_tier=data["classifier_tier"],
        classifier_confidence=data["classifier_confidence"],
        runner=data["runner"],
        created_at=from_us(created_at),
        finished_at=_moment(finished_at),
        prompt_sha256=data.get("prompt_sha256"),
        prompt_chars=data.get("prompt_chars"),
        tier_pending=bool(data.get("tier_pending", False)),
    )


def _attempt_values(a: Attempt) -> tuple[Any, ...]:
    tree = None
    if a.tree_before is not None:
        tree = json.dumps({path: list(state) for path, state in a.tree_before.items()})
    return (
        a.job_id,
        a.n,
        a.route,
        a.pool,
        to_us(a.started_at),
        _us(a.finished_at),
        a.outcome,
        a.duration_s,
        a.tokens_in,
        a.tokens_out,
        a.cost_usd,
        a.head_before,
        tree,
        a.log_tail,
        a.pid,
        a.exe_name,
        a.proc_started,
        _result_data(a.result),
        a.spend_source,
        _decision_data(a.decision),
        int(a.cost_estimated),
    )


def _attempt(row: Sequence[Any]) -> Attempt:
    tree = None
    if row[12] is not None:
        tree = {
            path: (str(state[0]), int(state[1]), int(state[2]))
            for path, state in json.loads(row[12]).items()
        }
    return Attempt(
        job_id=row[0],
        n=row[1],
        route=row[2],
        pool=row[3],
        started_at=from_us(row[4]),
        finished_at=_moment(row[5]),
        outcome=row[6],
        duration_s=row[7],
        tokens_in=row[8],
        tokens_out=row[9],
        cost_usd=row[10],
        head_before=row[11],
        tree_before=tree,
        log_tail=row[13],
        pid=row[14],
        exe_name=row[15],
        proc_started=row[16],
        result=_attempt_result(row[17]),
        spend_source=row[18],
        decision=_decision(row[19]),
        cost_estimated=bool(row[20]),
    )


def _spend_values(e: SpendEntry) -> tuple[Any, ...]:
    return (
        e.pool,
        to_us(e.at),
        e.source,
        e.route,
        e.tokens_in,
        e.tokens_out,
        e.cost_usd,
        int(e.estimated),
        e.prices_version,
        e.ref_id,
        e.ref_n,
    )


_RATING_COLUMNS = "job_id, value, should_tier, by, note_sha256, note_chars, at"


def _rating_values(r: Rating) -> tuple[Any, ...]:
    return (
        r.job_id,
        r.value,
        r.should_tier,
        r.by,
        r.note_sha256,
        r.note_chars,
        to_utc(r.at).isoformat(),
    )


def _rating(row: Sequence[Any]) -> Rating:
    return Rating(
        job_id=row[0],
        value=row[1],
        should_tier=row[2],
        by=row[3],
        note_sha256=row[4],
        note_chars=row[5],
        at=datetime.fromisoformat(row[6]),
    )


_VOTE_COLUMNS = "classifier_id, model, tier, confidence, latency_ms, error, offline"


def _vote(row: Sequence[Any]) -> Vote:
    return Vote(
        classifier_id=row[0],
        model=row[1],
        tier=row[2],
        confidence=row[3],
        latency_ms=row[4],
        error=row[5],
        offline=bool(row[6]),
    )


class SqliteJobStore:
    """Jobs, attempts, spend, and pool blocks in the observation store's SQLite file."""

    _JOB_COLUMNS = "id, status, created_at, finished_at, data"

    def __init__(self, store: SqliteStore) -> None:
        self._store = store
        migrate_jobs_schema(store)

    def close(self) -> None:
        """Connections belong to SqliteStore — there is nothing to close here."""

    # --- jobs -------------------------------------------------------------------

    def create(self, job: Job) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                f"INSERT INTO jobs ({self._JOB_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
                (job.id, job.status, to_us(job.created_at), _us(job.finished_at), _job_data(job)),
            )

    def get(self, job_id: str) -> Job | None:
        row = (
            self._store.connection()
            .execute(f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,))
            .fetchone()
        )
        return None if row is None else _job(row)

    def update(self, job: Job) -> None:
        with self._store.transaction() as conn:
            done = conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, data = ? WHERE id = ?",
                (job.status, _us(job.finished_at), _job_data(job), job.id),
            )
            if done.rowcount != 1:
                raise KeyError(job.id)

    def list(self, status: Status | None = None, limit: int = 50) -> builtins.list[Job]:
        conn = self._store.connection()
        if status is None:
            rows = conn.execute(
                f"SELECT {self._JOB_COLUMNS} FROM jobs ORDER BY created_at DESC, seq DESC LIMIT ?",
                (int(limit),),
            )
        else:
            rows = conn.execute(
                f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE status = ?"
                " ORDER BY created_at DESC, seq DESC LIMIT ?",
                (status, int(limit)),
            )
        return [_job(row) for row in rows.fetchall()]

    def search(
        self,
        *,
        status: Status | None = None,
        route: str | None = None,
        caller: str | None = None,
        q: str | None = None,
        sort: str = "newest",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[builtins.list[Job], int]:
        conditions: list[str] = []
        params: list[Any] = []
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if route is not None:
            conditions.append(
                "(json_extract(data, '$.route') = ?"
                " OR json_extract(data, '$.approval_route') = ?"
                " OR json_extract(data, '$.approved_route') = ?)"
            )
            params.extend((route, route, route))
        if caller is not None:
            conditions.append("json_extract(data, '$.request.caller') = ?")
            params.append(caller)
        if q is not None:
            conditions.append(
                "(id = ? OR instr(lower(json_extract(data, '$.request.prompt')), lower(?)) > 0)"
            )
            params.extend((q, q))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        order = {
            "oldest": "created_at ASC, seq ASC",
            "duration": (
                "finished_at IS NULL ASC, (finished_at - created_at) DESC,"
                " created_at DESC, seq DESC"
            ),
        }.get(sort, "created_at DESC, seq DESC")
        conn = self._store.connection()
        total = int(conn.execute(f"SELECT COUNT(*) FROM jobs{where}", params).fetchone()[0])
        rows = conn.execute(
            f"SELECT {self._JOB_COLUMNS} FROM jobs{where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, int(limit), int(offset)),
        )
        return [_job(row) for row in rows.fetchall()], total

    def purge(
        self, before: datetime, jobs_dir: Path, dry_run: bool = False
    ) -> tuple[builtins.list[str], builtins.list[Path]]:
        moment = to_us(before)
        condition = (
            "COALESCE(finished_at, created_at) < ? AND status IN"
            " ('completed', 'partial', 'failed', 'declined', 'cancelled')"
        )
        query = f"SELECT id FROM jobs WHERE {condition} ORDER BY created_at, seq"
        if dry_run:
            rows = self._store.connection().execute(query, (moment,)).fetchall()
            job_ids = [row[0] for row in rows]
        else:
            with self._store.transaction() as conn:
                rows = conn.execute(query, (moment,)).fetchall()
                job_ids = [row[0] for row in rows]
                for table in ("attempts", "ratings", "classifier_votes"):
                    conn.execute(
                        f"DELETE FROM {table} WHERE job_id IN"
                        " (SELECT id FROM jobs WHERE"
                        f" {condition})",
                        (moment,),
                    )
                conn.execute(f"DELETE FROM jobs WHERE {condition}", (moment,))
        failed_dirs = []
        if not dry_run:
            for job_id in job_ids:
                directory = Path(jobs_dir) / job_id
                try:
                    with contextlib.suppress(FileNotFoundError):
                        shutil.rmtree(directory)
                except OSError:
                    failed_dirs.append(directory)

        purged_ids = set(job_ids)
        cutoff = moment / 1_000_000
        # Sweeping retries failed removals without a schema change and also cleans up after crashes
        # and older versions.
        with contextlib.suppress(FileNotFoundError):
            for directory in sorted(Path(jobs_dir).iterdir()):
                job_id = directory.name
                if job_id in purged_ids or _JOB_DIR_NAME.fullmatch(job_id) is None:
                    continue
                if directory.is_symlink() or not directory.is_dir():
                    continue
                row = (
                    self._store.connection()
                    .execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,))
                    .fetchone()
                )
                if row is not None:
                    continue
                try:
                    newest_mtime = max(
                        (path.stat().st_mtime for path in directory.rglob("*") if path.is_file()),
                        default=directory.stat().st_mtime,
                    )
                    if newest_mtime >= cutoff:
                        continue
                    if dry_run:
                        job_ids.append(job_id)
                    else:
                        shutil.rmtree(directory)
                        job_ids.append(job_id)
                except FileNotFoundError:
                    continue
                except OSError:
                    failed_dirs.append(directory)
        return job_ids, failed_dirs

    def queued(self, limit: int, after: str | None = None) -> builtins.list[Job]:
        conn = self._store.connection()
        place = None
        if after is not None:
            place = conn.execute(
                "SELECT created_at, seq FROM jobs WHERE id = ?", (after,)
            ).fetchone()
        if place is None:
            rows = conn.execute(
                f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE status = 'queued'"
                " ORDER BY created_at, seq LIMIT ?",
                (int(limit),),
            )
        else:
            rows = conn.execute(
                f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE status = 'queued'"
                " AND (created_at > ? OR (created_at = ? AND seq > ?))"
                " ORDER BY created_at, seq LIMIT ?",
                (place[0], place[0], place[1], int(limit)),
            )
        return [_job(row) for row in rows.fetchall()]

    def classifier_agreement(self, limit: int = 500) -> tuple[int, int]:
        row = (
            self._store.connection()
            .execute(
                "SELECT"
                " COALESCE(SUM(json_extract(data, '$.classifier_tier') ="
                " json_extract(data, '$.tier')), 0), COUNT(*)"
                " FROM ("
                " SELECT data FROM jobs"
                " WHERE json_extract(data, '$.tier_source') = 'caller'"
                " AND json_extract(data, '$.classifier_tier') IS NOT NULL"
                " ORDER BY created_at DESC, seq DESC LIMIT ?"
                ")",
                (int(limit),),
            )
            .fetchone()
        )
        return int(row[0]), int(row[1])

    def count_waiting(self) -> int:
        row = (
            self._store.connection()
            .execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'awaiting_approval')")
            .fetchone()
        )
        return int(row[0])

    # --- attempts --------------------------------------------------------------

    def add_attempt(self, a: Attempt) -> None:
        marks = ", ".join("?" * 21)
        with self._store.transaction() as conn:
            conn.execute(
                f"INSERT INTO attempts ({_ATTEMPT_COLUMNS}) VALUES ({marks})", _attempt_values(a)
            )

    def start_attempt(self, job: Job, a: Attempt) -> None:
        """The attempt row and the job row are one transaction."""
        marks = ", ".join("?" * 21)
        with self._store.transaction() as conn:
            done = conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, data = ? WHERE id = ?",
                (job.status, _us(job.finished_at), _job_data(job), job.id),
            )
            if done.rowcount != 1:
                raise KeyError(job.id)
            conn.execute(
                f"INSERT INTO attempts ({_ATTEMPT_COLUMNS}) VALUES ({marks})", _attempt_values(a)
            )

    @staticmethod
    def _insert_spend(conn: Any, e: SpendEntry) -> bool:
        marks = ", ".join("?" * 11)
        inserted = conn.execute(
            f"INSERT INTO spend ({_SPEND_COLUMNS}) VALUES ({marks}) ON CONFLICT DO NOTHING",
            _spend_values(e),
        )
        return inserted.rowcount == 1

    def update_attempt(self, a: Attempt, spend: SpendEntry | None = None) -> bool:
        values = _attempt_values(a)
        inserted = False
        with self._store.transaction() as conn:
            done = conn.execute(
                "UPDATE attempts SET route = ?, pool = ?, started_at = ?, finished_at = ?,"
                " outcome = ?, duration_s = ?, tokens_in = ?, tokens_out = ?, cost_usd = ?,"
                " head_before = ?, tree_before = ?, log_tail = ?, pid = ?, exe_name = ?,"
                " proc_started = ?, result = ?, spend_source = ?, decision = ?,"
                " cost_estimated = ?"
                " WHERE job_id = ? AND n = ?",
                (*values[2:], a.job_id, a.n),
            )
            if done.rowcount != 1:
                raise KeyError((a.job_id, a.n))
            if spend is not None:
                inserted = self._insert_spend(conn, spend)
        return inserted

    def attempts(self, job_id: str) -> builtins.list[Attempt]:
        rows = self._store.connection().execute(
            f"SELECT {_ATTEMPT_COLUMNS} FROM attempts WHERE job_id = ? ORDER BY n", (job_id,)
        )
        return [_attempt(row) for row in rows.fetchall()]

    # --- ratings ----------------------------------------------------------------

    def add_rating(self, r: Rating) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                f"INSERT INTO ratings ({_RATING_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                _rating_values(r),
            )

    def last_rating(self, job_id: str) -> Rating | None:
        row = (
            self._store.connection()
            .execute(
                f"SELECT {_RATING_COLUMNS} FROM ratings WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (job_id,),
            )
            .fetchone()
        )
        return None if row is None else _rating(row)

    # --- classifier votes --------------------------------------------------------

    def add_vote(self, job_id: str, vote: Vote, at: datetime) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                "INSERT INTO classifier_votes"
                " (job_id, classifier_id, model, tier, confidence, latency_ms, error, offline, at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    vote.classifier_id,
                    vote.model,
                    vote.tier,
                    vote.confidence,
                    vote.latency_ms,
                    vote.error,
                    int(vote.offline),
                    to_utc(at).isoformat(),
                ),
            )

    def votes(self, job_id: str) -> builtins.list[Vote]:
        rows = self._store.connection().execute(
            f"SELECT {_VOTE_COLUMNS} FROM classifier_votes WHERE job_id = ? ORDER BY id",
            (job_id,),
        )
        return [_vote(row) for row in rows.fetchall()]

    def votes_since(self, since: datetime, offline: bool | None = None) -> builtins.list[VoteRow]:
        query = (
            "SELECT v.job_id, v.classifier_id, v.model, v.tier, v.confidence, v.latency_ms,"
            " v.error, v.offline, v.at, j.data, j.created_at,"
            " (SELECT r.value FROM ratings r WHERE r.job_id = v.job_id"
            "  ORDER BY r.id DESC LIMIT 1),"
            " (SELECT r.should_tier FROM ratings r WHERE r.job_id = v.job_id"
            "  ORDER BY r.id DESC LIMIT 1)"
            " FROM classifier_votes v JOIN jobs j ON j.id = v.job_id"
            " WHERE v.at >= ?"
        )
        params: list[Any] = [to_utc(since).isoformat()]
        if offline is not None:
            query += " AND v.offline = ?"
            params.append(int(offline))
        query += " ORDER BY v.at, v.id"
        rows = self._store.connection().execute(query, params)
        result: list[VoteRow] = []
        for row in rows.fetchall():
            data = json.loads(row[9])
            request = data["request"]
            result.append(
                VoteRow(
                    job_id=row[0],
                    classifier_id=row[1],
                    model=row[2],
                    tier=row[3],
                    confidence=row[4],
                    latency_ms=row[5],
                    error=row[6],
                    offline=bool(row[7]),
                    at=datetime.fromisoformat(row[8]),
                    job_tier=data["tier"],
                    tier_source=data["tier_source"],
                    mode=request["mode"],
                    created_at=from_us(row[10]),
                    rating_value=row[11],
                    rating_should_tier=row[12],
                )
            )
        return result

    def prompts_since(self, since: datetime) -> builtins.list[tuple[str, str]]:
        rows = self._store.connection().execute(
            "SELECT id, data FROM jobs WHERE created_at >= ? ORDER BY created_at, seq",
            (to_us(since),),
        )
        return [(job_id, json.loads(raw)["request"]["prompt"]) for job_id, raw in rows.fetchall()]

    def usage_since(self, pool: str, since: datetime | None) -> GatewayUsage:
        floor = -(2**62) if since is None else to_us(since)
        conn = self._store.connection()
        row = conn.execute(
            "SELECT COUNT(DISTINCT job_id), COALESCE(SUM(duration_s), 0),"
            " COALESCE(SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)), 0)"
            " FROM attempts WHERE pool = ? AND started_at >= ?",
            (pool, floor),
        ).fetchone()
        proxy = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)), 0)"
            " FROM spend WHERE pool = ? AND source = 'proxy' AND at > ?",
            (pool, floor),
        ).fetchone()
        return GatewayUsage(
            jobs=int(row[0]),
            seconds=float(row[1]),
            tokens=int(row[2]),
            proxy_tokens=int(proxy[0]),
        )

    def add_spend(self, e: SpendEntry) -> bool:
        with self._store.transaction() as conn:
            return self._insert_spend(conn, e)

    def spend_since(self, pool: str, since: datetime | None) -> SpendTotal:
        floor = -(2**62) if since is None else to_us(since)
        row = (
            self._store.connection()
            .execute(
                "SELECT COALESCE(SUM(source = 'job'), 0),"
                " COALESCE(SUM(source = 'proxy'), 0),"
                " COALESCE(SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)), 0),"
                " COALESCE(SUM(cost_usd), 0), COALESCE(MAX(estimated), 0)"
                " FROM spend WHERE pool = ? AND at > ?",
                (pool, floor),
            )
            .fetchone()
        )
        return SpendTotal(
            jobs=int(row[0]),
            requests=int(row[1]),
            tokens=int(row[2]),
            cost_usd=float(row[3]),
            estimated=bool(row[4]),
        )

    def spend_buckets(
        self, pool: str, starts: Sequence[datetime], until: datetime
    ) -> builtins.list[SpendBucket]:
        original_starts = list(starts)
        if not original_starts:
            return []
        moments = [to_utc(start) for start in original_starts]
        end = to_utc(until)
        if (
            any(left >= right for left, right in zip(moments, moments[1:], strict=False))
            or moments[-1] >= end
        ):
            raise ValueError("bucket starts must be ascending and before until")
        cases = []
        params: list[Any] = []
        for index, start in enumerate(moments):
            stop = moments[index + 1] if index + 1 < len(moments) else end
            cases.append("WHEN at >= ? AND at < ? THEN ?")
            params.extend((to_us(start), to_us(stop), index))
        params.extend((pool, to_us(moments[0]), to_us(end)))
        rows = self._store.connection().execute(
            "SELECT CASE " + " ".join(cases) + " ELSE -1 END AS bucket_index,"
            " COALESCE(SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)), 0),"
            " COALESCE(SUM(cost_usd), 0), COALESCE(MAX(estimated), 0)"
            " FROM spend WHERE pool = ? AND at >= ? AND at < ?"
            " GROUP BY bucket_index ORDER BY bucket_index",
            params,
        )
        result = []
        for index, tokens, cost_usd, estimated in rows.fetchall():
            if index < 0:
                continue
            result.append(
                SpendBucket(
                    start=original_starts[index],
                    tokens=int(tokens),
                    cost_usd=float(cost_usd),
                    estimated=bool(estimated),
                )
            )
        return result

    def limit_hits_in_row(self, pool: str) -> int:
        # Order the streak by finish time: a pool's attempts run in parallel, so a row
        # inserted earlier may finish later. The id breaks ties.
        conn = self._store.connection()
        last_ok = conn.execute(
            "SELECT finished_at, id FROM attempts"
            " WHERE pool = ? AND outcome = 'ok' AND finished_at IS NOT NULL"
            " ORDER BY finished_at DESC, id DESC LIMIT 1",
            (pool,),
        ).fetchone()
        if last_ok is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM attempts"
                " WHERE pool = ? AND outcome = 'limit' AND finished_at IS NOT NULL",
                (pool,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM attempts"
                " WHERE pool = ? AND outcome = 'limit' AND finished_at IS NOT NULL"
                " AND (finished_at > ? OR (finished_at = ? AND id > ?))",
                (pool, last_ok[0], last_ok[0], last_ok[1]),
            ).fetchone()
        return int(row[0])

    def route_errors_in_row(self, route: str) -> tuple[int, datetime | None]:
        # Order the streak by finish time: a row inserted earlier may finish later. The id
        # breaks ties.
        conn = self._store.connection()
        last_ok = conn.execute(
            "SELECT finished_at, id FROM attempts"
            " WHERE route = ? AND outcome = 'ok' AND finished_at IS NOT NULL"
            " ORDER BY finished_at DESC, id DESC LIMIT 1",
            (route,),
        ).fetchone()
        query = (
            "SELECT COUNT(*), MAX(finished_at) FROM attempts"
            " WHERE route = ? AND outcome = 'error' AND finished_at IS NOT NULL"
        )
        if last_ok is None:
            row = conn.execute(query, (route,)).fetchone()
        else:
            row = conn.execute(
                query + " AND (finished_at > ? OR (finished_at = ? AND id > ?))",
                (route, last_ok[0], last_ok[0], last_ok[1]),
            ).fetchone()
        count = int(row[0])
        return count, None if count == 0 else from_us(row[1])

    # --- pool blocks ------------------------------------------------------------

    def add_block(self, block: PoolBlock, job_id: str | None) -> PoolBlock:
        until = to_us(block.until)
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT until, reason FROM pool_blocks WHERE pool = ?", (block.pool,)
            ).fetchone()
            if row is not None and row[0] >= until:
                return PoolBlock(block.pool, from_us(row[0]), row[1])
            conn.execute(
                "INSERT OR REPLACE INTO pool_blocks (pool, until, reason, job_id)"
                " VALUES (?, ?, ?, ?)",
                (block.pool, until, block.reason, job_id),
            )
        return block

    def active_blocks(self, now: datetime) -> dict[str, PoolBlock]:
        rows = self._store.connection().execute(
            "SELECT pool, until, reason FROM pool_blocks WHERE until > ? ORDER BY pool",
            (to_us(now),),
        )
        return {row[0]: PoolBlock(row[0], from_us(row[1]), row[2]) for row in rows.fetchall()}

    def clear_block(self, pool: str) -> None:
        with self._store.transaction() as conn:
            conn.execute("DELETE FROM pool_blocks WHERE pool = ?", (pool,))

    def expire_blocks(self, now: datetime) -> builtins.list[str]:
        moment = to_us(now)
        with self._store.transaction() as conn:
            rows = conn.execute(
                "SELECT pool FROM pool_blocks WHERE until <= ? ORDER BY pool", (moment,)
            ).fetchall()
            conn.execute("DELETE FROM pool_blocks WHERE until <= ?", (moment,))
        return [row[0] for row in rows]


def open_jobstore(settings: StorageSettings, home: Path, store: Store) -> JobStore:
    """Job store per settings: built-in SQLite or a third-party backend by name."""
    if settings.backend == "sqlite":
        if not isinstance(store, SqliteStore):
            raise ConfigError(["sqlite job store requires an sqlite observation store"])
        return SqliteJobStore(store)
    return plugin_call(
        STORAGE_GROUP, settings.backend, "open_jobstore", settings.options, Path(home), store
    )
