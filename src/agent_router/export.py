"""Export the SQLite event log without job texts or full paths."""

from __future__ import annotations

import csv
import hmac
import json
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from agent_router.config import effort_of
from agent_router.jobs import Attempt, Job, Rating
from agent_router.jobstore import _ATTEMPT_COLUMNS, _RATING_COLUMNS, _attempt, _job, _rating
from agent_router.store import from_us, to_us

JOB_CSV_COLUMNS = (
    "id",
    "status",
    "tier",
    "tier_source",
    "mode",
    "kind",
    "caller",
    "route",
    "reason",
    "had_refusal",
    "excluded_routes",
    "excluded_pools",
    "cancel_requested",
    "violations",
    "error_code",
    "classifier_tier",
    "classifier_confidence",
    "prompt_sha256",
    "prompt_chars",
    "created_at",
    "finished_at",
    "timeout_s",
    "changed_count",
    "error_chars",
    "repo_name",
    "repo_hash",
    "cwd_name",
    "cwd_hash",
    "rating_value",
    "rating_should_tier",
    "rating_by",
    "rating_at",
    "rating_note_chars",
    "attempt_n",
    "attempt_route",
    "attempt_pool",
    "attempt_started_at",
    "attempt_finished_at",
    "attempt_outcome",
    "attempt_duration_s",
    "attempt_tokens_in",
    "attempt_tokens_out",
    "attempt_cost_usd",
    "attempt_cost_estimated",
    "attempt_spend_source",
    "attempt_decision",
    "attempt_effort",
    "attempt_limit_reset_at",
    "attempt_head_moved",
    "attempt_changes_unknown",
    "attempt_changed_count",
    "primary_classifier",
    "primary_model",
    "primary_tier",
    "primary_confidence",
    "primary_latency_ms",
    "primary_error",
)
SPEND_COLUMNS = (
    "pool",
    "at",
    "source",
    "route",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "estimated",
    "prices_version",
    "ref_id",
    "ref_n",
)
OBSERVATION_COLUMNS = ("pool", "window", "fraction", "observed_at", "resets_at", "source")
COUNTER_COLUMNS = ("pool", "value_usd", "sampled_at", "scale_hash")
BLOCK_COLUMNS = ("pool", "until", "reason", "job_id")


# Lowest schema version that has everything the export reads
# (settings, decision, ratings, counter_samples.scale_hash, attempts.cost_estimated,
# classifier_votes)
REQUIRED_SCHEMA = 10


class ExportError(Exception):
    """The database is unsuitable for export; the message is for the user."""


def _schema_version(db_path: Path) -> int:
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _epoch_iso(value: int | None) -> str | None:
    return None if value is None else from_us(value).isoformat()


def _path_record(path: str | None, salt: str) -> dict[str, str] | None:
    if path is None:
        return None
    digest = hmac.new(salt.encode(), path.encode(), sha256).hexdigest()[:12]
    return {"name": Path(path).name, "hash": digest}


def _effort(attempt: Attempt, route_models: Mapping[str, str] | None) -> str | None:
    model = None
    if attempt.decision is not None:
        chosen = attempt.decision.get("chosen")
        if isinstance(chosen, dict) and isinstance(chosen.get("model"), str):
            model = chosen["model"]
    if model is None and route_models is not None:
        model = route_models.get(attempt.route)
    return None if model is None else effort_of(model)


def _attempt_record(attempt: Attempt, route_models: Mapping[str, str] | None) -> dict[str, Any]:
    result = attempt.result
    changed = None if result is None else result.changed
    return {
        "n": attempt.n,
        "route": attempt.route,
        "pool": attempt.pool,
        "started_at": _iso(attempt.started_at),
        "finished_at": _iso(attempt.finished_at),
        "outcome": attempt.outcome,
        "duration_s": attempt.duration_s,
        "tokens_in": attempt.tokens_in,
        "tokens_out": attempt.tokens_out,
        "cost_usd": attempt.cost_usd,
        "cost_estimated": attempt.cost_estimated,
        "spend_source": attempt.spend_source,
        "decision": attempt.decision,
        "effort": _effort(attempt, route_models),
        "limit_reset_at": None if result is None else _iso(result.limit_reset_at),
        "head_moved": None if result is None else result.head_moved,
        "changes_unknown": None if result is None else result.changes_unknown,
        "changed_count": None if changed is None else len(changed),
    }


def _rating_record(rating: Rating | None) -> dict[str, Any] | None:
    if rating is None:
        return None
    return {
        "value": rating.value,
        "should_tier": rating.should_tier,
        "by": rating.by,
        "at": _iso(rating.at),
        "note_chars": rating.note_chars,
    }


def _votes_record(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT classifier_id, model, tier, confidence, latency_ms, error, offline"
        " FROM classifier_votes WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    return [
        {
            "classifier_id": row[0],
            "model": row[1],
            "tier": row[2],
            "confidence": row[3],
            "latency_ms": row[4],
            "error": row[5],
            "offline": bool(row[6]),
        }
        for row in rows
    ]


def _primary_vote(votes: list[dict[str, Any]], primary: str | None) -> dict[str, Any] | None:
    """Last non-offline vote of the primary classifier, used for the jobs.csv columns."""
    if primary is None:
        return None
    found = None
    for vote in votes:
        if vote["classifier_id"] == primary and not vote["offline"]:
            found = vote
    return found


def _job_record(
    conn: sqlite3.Connection,
    job: Job,
    data: dict[str, Any],
    salt: str,
    route_models: Mapping[str, str] | None,
) -> dict[str, Any]:
    attempt_rows = conn.execute(
        f"SELECT {_ATTEMPT_COLUMNS} FROM attempts WHERE job_id = ? ORDER BY n", (job.id,)
    ).fetchall()
    attempts = [_attempt(row) for row in attempt_rows]
    rating_row = conn.execute(
        f"SELECT {_RATING_COLUMNS} FROM ratings WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job.id,),
    ).fetchone()
    rating = None if rating_row is None else _rating(rating_row)
    changed = job.changed_files
    return {
        "id": job.id,
        "status": job.status,
        "tier": job.tier,
        "tier_source": job.tier_source,
        "mode": job.request.mode,
        "kind": job.request.kind,
        "caller": job.request.caller,
        "route": job.route,
        "reason": job.reason,
        "had_refusal": job.had_refusal,
        "excluded_routes": sorted(job.excluded_routes),
        "excluded_pools": sorted(job.excluded_pools),
        "cancel_requested": job.cancel_requested,
        "violations": list(job.violations),
        "error_code": job.error_code,
        "classifier_tier": job.classifier_tier,
        "classifier_confidence": job.classifier_confidence,
        "prompt_sha256": job.prompt_sha256,
        "prompt_chars": job.prompt_chars,
        "created_at": _iso(job.created_at),
        "finished_at": _iso(job.finished_at),
        "timeout_s": job.request.timeout_s,
        "changed_count": None if changed is None else len(changed),
        "error_chars": None if job.error_message is None else len(job.error_message),
        "repo": _path_record(data["repo_root"], salt),
        "cwd": _path_record(data["request"]["cwd"], salt),
        "attempts": [_attempt_record(attempt, route_models) for attempt in attempts],
        "rating": _rating_record(rating),
        "votes": _votes_record(conn, job.id),
    }


def _read_jobs(
    conn: sqlite3.Connection,
    since: int,
    until: int | None,
    salt: str,
    route_models: Mapping[str, str] | None,
) -> list[dict[str, Any]]:
    query = "SELECT id, status, created_at, finished_at, data FROM jobs WHERE created_at >= ?"
    params: list[int] = [since]
    if until is not None:
        query += " AND created_at < ?"
        params.append(until)
    rows = conn.execute(query + " ORDER BY created_at, seq", params).fetchall()
    records = []
    for row in rows:
        data = json.loads(row[4])
        records.append(_job_record(conn, _job(row), data, salt, route_models))
    return records


def _read_spend(conn: sqlite3.Connection, since: int, until: int | None) -> list[dict[str, Any]]:
    query = f"SELECT {', '.join(SPEND_COLUMNS)} FROM spend WHERE at >= ?"
    params: list[int] = [since]
    if until is not None:
        query += " AND at < ?"
        params.append(until)
    rows = conn.execute(query + " ORDER BY at, id", params).fetchall()
    return [
        {
            "pool": row[0],
            "at": from_us(row[1]).isoformat(),
            "source": row[2],
            "route": row[3],
            "tokens_in": row[4],
            "tokens_out": row[5],
            "cost_usd": row[6],
            "estimated": bool(row[7]),
            "prices_version": row[8],
            "ref_id": row[9],
            "ref_n": row[10],
        }
        for row in rows
    ]


def _read_observations(
    conn: sqlite3.Connection, since: int, until: int | None
) -> list[dict[str, Any]]:
    query = f"SELECT {', '.join(OBSERVATION_COLUMNS)} FROM observations WHERE observed_at >= ?"
    params: list[int] = [since]
    if until is not None:
        query += " AND observed_at < ?"
        params.append(until)
    rows = conn.execute(query + " ORDER BY observed_at, id", params).fetchall()
    return [
        {
            "pool": row[0],
            "window": row[1],
            "fraction": row[2],
            "observed_at": from_us(row[3]).isoformat(),
            "resets_at": _epoch_iso(row[4]),
            "source": row[5],
        }
        for row in rows
    ]


def _read_counters(conn: sqlite3.Connection, since: int, until: int | None) -> list[dict[str, Any]]:
    query = f"SELECT {', '.join(COUNTER_COLUMNS)} FROM counter_samples WHERE sampled_at >= ?"
    params: list[int] = [since]
    if until is not None:
        query += " AND sampled_at < ?"
        params.append(until)
    rows = conn.execute(query + " ORDER BY sampled_at, id", params).fetchall()
    return [
        {
            "pool": row[0],
            "value_usd": row[1],
            "sampled_at": from_us(row[2]).isoformat(),
            "scale_hash": row[3],
        }
        for row in rows
    ]


def _read_blocks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(BLOCK_COLUMNS)} FROM pool_blocks ORDER BY until, pool"
    ).fetchall()
    return [
        {
            "pool": row[0],
            "until": from_us(row[1]).isoformat(),
            "reason": row[2],
            "job_id": row[3],
        }
        for row in rows
    ]


def _salt(db_path: Path) -> str:
    conn = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('export_salt', ?) ON CONFLICT DO NOTHING",
            (secrets.token_hex(16),),
        )
        row = conn.execute("SELECT value FROM settings WHERE key = 'export_salt'").fetchone()
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    if row is None:
        raise RuntimeError("failed to get export salt")
    return str(row[0])


def _csv_value(value: Any) -> Any:
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return value


def _job_csv_rows(jobs: list[dict[str, Any]], primary: str | None = None) -> list[dict[str, Any]]:
    rows = []
    for job in jobs:
        base = {key: job[key] for key in JOB_CSV_COLUMNS if key in job}
        for prefix, key in (("repo", "repo"), ("cwd", "cwd"), ("rating", "rating")):
            nested = job[key]
            if nested is not None:
                base.update({f"{prefix}_{name}": value for name, value in nested.items()})
        vote = _primary_vote(job["votes"], primary)
        if vote is not None:
            base.update(
                {
                    "primary_classifier": vote["classifier_id"],
                    "primary_model": vote["model"],
                    "primary_tier": vote["tier"],
                    "primary_confidence": vote["confidence"],
                    "primary_latency_ms": vote["latency_ms"],
                    "primary_error": vote["error"],
                }
            )
        attempts = job["attempts"] or [None]
        for attempt in attempts:
            row = dict(base)
            if attempt is not None:
                row.update({f"attempt_{key}": value for key, value in attempt.items()})
            rows.append({key: _csv_value(row.get(key)) for key in JOB_CSV_COLUMNS})
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def export(
    db_path: Path,
    out_dir: Path,
    since: datetime,
    until: datetime | None,
    fmt: Literal["jsonl", "csv"],
    route_models: Mapping[str, str] | None = None,
    primary: str | None = None,
) -> list[Path]:
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    version = _schema_version(db_path)
    if version < REQUIRED_SCHEMA:
        raise ExportError(
            f"database schema is version {version}, export requires {REQUIRED_SCHEMA}:"
            " restart the gateway with the new version and it will migrate the database"
        )
    salt = _salt(db_path)
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        since_us = to_us(since)
        until_us = None if until is None else to_us(until)
        jobs = _read_jobs(conn, since_us, until_us, salt, route_models)
        spend = _read_spend(conn, since_us, until_us)
        observations = _read_observations(conn, since_us, until_us)
        counters = _read_counters(conn, since_us, until_us)
        blocks = _read_blocks(conn)
    finally:
        conn.close()

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    extension = "jsonl" if fmt == "jsonl" else "csv"
    paths = [
        out_dir / f"{name}.{extension}"
        for name in ("jobs", "spend", "observations", "counter_samples", "pool_blocks")
    ]
    tables = (jobs, spend, observations, counters, blocks)
    if fmt == "jsonl":
        for path, rows in zip(paths, tables, strict=True):
            _write_jsonl(path, rows)
    else:
        _write_csv(paths[0], _job_csv_rows(jobs, primary), JOB_CSV_COLUMNS)
        for path, rows, columns in zip(
            paths[1:],
            tables[1:],
            (SPEND_COLUMNS, OBSERVATION_COLUMNS, COUNTER_COLUMNS, BLOCK_COLUMNS),
            strict=True,
        ):
            _write_csv(path, rows, columns)
    return paths
