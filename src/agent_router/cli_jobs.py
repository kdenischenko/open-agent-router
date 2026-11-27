"""Job commands: run, wait, result, rate, cancel, jobs, approve, deny and unblock."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import typer

from agent_router.cli_common import _gateway
from agent_router.client import GatewayClient, GatewayError, GatewayUnavailable
from agent_router.clock import format_local
from agent_router.config import home_dir
from agent_router.jobs import KINDS, TERMINAL


def _job_failure(exc: GatewayUnavailable | GatewayError) -> NoReturn:
    if isinstance(exc, GatewayUnavailable):
        typer.echo("gateway unavailable: check that agent-router serve is running", err=True)
        raise typer.Exit(5) from exc
    if exc.status in (401, 403):
        typer.echo("gateway rejected the token", err=True)
        raise typer.Exit(5) from exc
    if exc.code == "queue_full":
        typer.echo("queue is full, try again later", err=True)
        raise typer.Exit(6) from exc
    typer.echo(f"request denied: {exc.message}", err=True)
    raise typer.Exit(6 if 400 <= exc.status < 500 else 1) from exc


def _job_request(call: Callable[[], dict]) -> dict:
    try:
        return call()
    except (GatewayUnavailable, GatewayError) as exc:
        _job_failure(exc)


def _job_summary(job: dict) -> None:
    changed = len(job.get("changed_files") or [])
    typer.echo(
        f"[agent-router] job {job['id']}, status {job['status']}, "
        f"route {job.get('route') or '—'}, route choice {job.get('reason') or '—'}, "
        f"files changed: {changed}",
        err=True,
    )
    error = job.get("error") or {}
    if job["status"] in {"failed", "partial", "declined"} and error.get("code"):
        typer.echo(f"error {error['code']}: {error.get('message') or '—'}", err=True)
    elif job["status"] == "declined":
        # a declined job without an error: a confirmation was refused and no other route matched
        typer.echo("declined: confirmation refused and no other route matches", err=True)
    violations = job.get("violations") or []
    if violations:
        typer.echo(f"violations: {', '.join(violations)}", err=True)
    files = job.get("changed_files") or []
    if job.get("mode") == "read" and files:
        shown = ", ".join(files[:5])
        more = f" and {len(files) - 5} more" if len(files) > 5 else ""
        typer.echo(f"warning: the read job changed {len(files)} files: {shown}{more}", err=True)
    attempts = job.get("attempts") or []
    if job["status"] in {"failed", "partial"} and attempts:
        # client and gateway share the home, so the attempt directory is local
        last = attempts[-1]["n"]
        typer.echo(f"logs: {home_dir() / 'jobs' / job['id'] / f'attempt-{last}'}", err=True)
    tail = [line for line in (job.get("log_tail") or "").splitlines() if line.strip()]
    if job["status"] == "failed" and tail:
        typer.echo("last log lines:", err=True)
        for line in tail[-5:]:
            typer.echo(f"  {line}", err=True)


def _emit_job(job: dict, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(job, ensure_ascii=False))
    elif job.get("text"):
        typer.echo(job["text"])
    status = job["status"]
    if status in {"completed", "partial", "failed", "declined", "cancelled"}:
        _job_summary(job)
        if job.get("rating") is None:
            typer.echo(f"rate: agent-router rate {job['id']} good|bad|partial", err=True)
        if status == "partial":
            raise typer.Exit(2)
        if status != "completed":
            raise typer.Exit(3)
        return
    if status == "awaiting_approval":
        deadline = (job.get("approval") or {}).get("deadline")
        before = "" if not deadline else f" before {format_local(deadline)}"
        typer.echo(
            f"job awaits approval: agent-router approve {job['id']}{before}, "
            f"then agent-router wait {job['id']}",
            err=True,
        )
    else:
        typer.echo(f"job still running; retry: agent-router wait {job['id']}", err=True)
    raise typer.Exit(4)


def _prompt(text: str | None, prompt_file: Path | None) -> str:
    if text is not None and prompt_file is not None:
        typer.echo("pass the text positionally or via --prompt-file, not both", err=True)
        raise typer.Exit(1)
    if prompt_file is not None:
        try:
            return _prompt_text(prompt_file.read_bytes())
        except OSError as exc:
            detail = exc.strerror or type(exc).__name__
            typer.echo(f"could not read prompt file: {detail}", err=True)
            raise typer.Exit(1) from exc
        except UnicodeDecodeError as exc:
            typer.echo("prompt file is not UTF-8", err=True)
            raise typer.Exit(1) from exc
    if text == "-":
        try:
            return _prompt_text(sys.stdin.buffer.read())
        except UnicodeDecodeError as exc:
            typer.echo("prompt on stdin is not UTF-8", err=True)
            raise typer.Exit(1) from exc
    if text is None:
        typer.echo(
            "job text required: pass it as an argument, --prompt-file FILE or - for stdin", err=True
        )
        raise typer.Exit(1)
    return text


def _prompt_text(data: bytes) -> str:
    """Prompt from a file or stdin: BOM stripped, `\\r\\n` → `\\n`, a lone `\\r` kept as is."""
    return data.decode("utf-8-sig").replace("\r\n", "\n")


def run(
    text: str | None = typer.Argument(None, help="Job prompt text, or - for standard input"),
    cwd: Path = typer.Option(Path("."), "--cwd", help="Working directory for the job"),
    mode: str = typer.Option("read", "--mode", help="Job mode: read or write"),
    tier: str | None = typer.Option(None, "--tier", help="Job tier: cheap, standard or strong"),
    kind: str | None = typer.Option(
        None, "--kind", help="Job kind: code, review, research or other"
    ),
    caller: str | None = typer.Option(
        None, "--caller", help="Name of the calling agent, e.g. claude-code"
    ),
    route: str | None = typer.Option(
        None, "--route", help="Use this route instead of letting the gateway choose"
    ),
    exclude_pool: list[str] = typer.Option(
        [], "--exclude-pool", help="Pool that must not run the job; repeat to exclude several"
    ),
    review_of: str | None = typer.Option(
        None,
        "--review-of",
        help="Id of the job this job reviews: submits a review that stays off the model "
        "and pool that wrote the code",
    ),
    timeout: int | None = typer.Option(
        None,
        "--timeout",
        help="Job hard time limit in seconds; the idle limit still applies",
    ),
    no_wait: bool = typer.Option(
        False, "--no-wait", help="Print the job id and return without waiting"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of text"),
    prompt_file: Path | None = typer.Option(
        None, "--prompt-file", help="UTF-8 file containing the job prompt"
    ),
) -> None:
    """Submit a job and wait for its result."""
    if kind is not None and kind not in KINDS:
        typer.echo(f"unknown job kind: {kind}; choose code, review, research or other", err=True)
        raise typer.Exit(1)
    if review_of is not None and kind not in (None, "review"):
        typer.echo("job kind must be review when --review-of is set", err=True)
        raise typer.Exit(1)
    body: dict[str, Any] = {
        "prompt": _prompt(text, prompt_file),
        "cwd": str(cwd.resolve()),
        "mode": mode,
    }
    for name, value in (
        ("tier", tier),
        ("kind", kind),
        ("caller", caller),
        ("route", route),
        ("timeout_s", timeout),
        ("review_of", review_of),
    ):
        if value is not None:
            body[name] = value
    if review_of is not None:
        body["kind"] = "review"
    if exclude_pool:
        body["exclude_pools"] = exclude_pool
    client = _gateway()
    submitted = _job_request(lambda: client.post("/api/v1/jobs", body))
    job_id = submitted["job_id"]
    if no_wait:
        typer.echo(job_id)
        return
    # a short first wait lets a job that needs approval return at once instead of after 100 s
    job = _job_request(lambda: client.get(f"/api/v1/jobs/{job_id}/wait", timeout=2))
    if job["status"] not in TERMINAL and job["status"] != "awaiting_approval":
        job = _job_request(lambda: client.get(f"/api/v1/jobs/{job_id}/wait", timeout=98))
    _emit_job(job, as_json)


def _wait_until(
    client: GatewayClient,
    job_id: str,
    timeout_s: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Poll the job in short requests until it reaches a terminal status or the deadline passes.

    The first request always goes out; with `timeout_s <= 100` it is the only one.
    A `running` reply faster than 1 s is followed by a pause before retrying, so as not to
    hammer the gateway. The gateway may be down for at most 10 s in a row, otherwise the
    exception propagates. An `awaiting_approval` reply also returns at once: the job needs a
    human, not more polling.
    """
    timeout_s = max(0.0, float(timeout_s))
    deadline = clock() + timeout_s
    last = clock()
    down_since: float | None = None
    started = False
    job: dict | None = None
    while True:
        remaining = timeout_s if not started else deadline - clock()
        if job is not None and remaining <= 0:
            return job
        try:
            job = client.get(f"/api/v1/jobs/{job_id}/wait", timeout=min(100.0, max(0.0, remaining)))
        except GatewayUnavailable:
            failed_at = clock()
            if down_since is None:
                down_since = failed_at
            if failed_at - down_since >= 10.0:
                raise
            sleep(1.0)
            started = True
            continue
        down_since = None
        started = True
        now = clock()
        if job["status"] in TERMINAL or job["status"] == "awaiting_approval":
            return job
        if timeout_s <= 100.0:
            return job
        if now - last < 1.0:
            sleep(1.0)
        last = clock()


def wait(
    job_id: str = typer.Argument(..., help="Job identifier"),
    timeout: float = typer.Option(
        100, "--timeout", help="How long this command waits, in seconds; the job keeps running"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of text"),
) -> None:
    """Wait for a job and print its result."""
    client = _gateway()
    job = _job_request(lambda: _wait_until(client, job_id, timeout))
    _emit_job(job, as_json)


def result(
    job_id: str = typer.Argument(..., help="Job identifier"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of text"),
) -> None:
    """Print the status and result of a job without waiting."""
    job = _job_request(lambda: _gateway().get(f"/api/v1/jobs/{job_id}"))
    _emit_job(job, as_json)


def rate(
    job_id: str = typer.Argument(..., help="Job identifier"),
    value: str = typer.Argument(..., help="Rating: good, bad, or partial"),
    should_tier: str | None = typer.Option(
        None, "--should-tier", help="The tier the job should have had"
    ),
    by: str | None = typer.Option(None, "--by", help="Who rates the job"),
    note: str | None = typer.Option(
        None, "--note", help="Note; only its hash and length are stored"
    ),
) -> None:
    """Rate a job result."""
    body: dict[str, Any] = {"value": value}
    for name, option in (("should_tier", should_tier), ("by", by), ("note", note)):
        if option is not None:
            body[name] = option
    rated = _job_request(lambda: _gateway().post(f"/api/v1/jobs/{job_id}/rating", body))
    typer.echo(f"rating recorded: {rated['id']} — {value}")


def cancel(job_id: str = typer.Argument(..., help="Job identifier")) -> None:
    """Cancel a queued or running job."""
    job = _job_request(lambda: _gateway().post(f"/api/v1/jobs/{job_id}/cancel"))
    if job["status"] == "running":
        # the executor is still being stopped; the job ends as cancelled once it exits
        typer.echo(
            f"Job {job['id']}: cancel requested, the executor is stopping; "
            f"follow it with agent-router wait {job['id']}"
        )
        return
    typer.echo(f"Job {job['id']}: {job['status']}")


def jobs(
    status: str | None = typer.Option(None, "--status", help="Job status filter"),
) -> None:
    """List jobs, optionally by status."""
    found = _job_request(lambda: _gateway().get("/api/v1/jobs", status=status))
    for job in found["jobs"]:
        fields = (job.get("route"), job.get("tier"), format_local(job.get("created_at")))
        typer.echo("\t".join([job["id"], job["status"], *(value or "—" for value in fields)]))


def _approve_when_ready(
    client: GatewayClient,
    job_id: str,
    action: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout_s: float = 10.0,
    pause_s: float = 0.5,
) -> dict:
    """Wait briefly for a queued job to reach its approval state."""
    deadline = clock() + timeout_s
    path = f"/api/v1/jobs/{job_id}/{action}"
    while True:
        try:
            return client.post(path)
        except GatewayError as exc:
            queued = (
                exc.status == 409 and exc.code == "wrong_state" and exc.message.endswith(": queued")
            )
            if not queued or clock() >= deadline:
                raise
            sleep(pause_s)


def _approve_action(job_id: str, action: str) -> None:
    job = _job_request(lambda: _approve_when_ready(_gateway(admin=True), job_id, action))
    if action == "deny":
        typer.echo(
            f"denied route {job.get('route') or '—'}: the job looks for another route",
            err=True,
        )
    else:
        _job_summary(job)
        if job["status"] not in TERMINAL:
            typer.echo(f"approved: follow the job with agent-router wait {job['id']}", err=True)


def approve(job_id: str = typer.Argument(..., help="Job identifier")) -> None:
    """Approve a job that awaits confirmation."""
    _approve_action(job_id, "approve")


def deny(job_id: str = typer.Argument(..., help="Job identifier")) -> None:
    """Deny a job that awaits confirmation."""
    _approve_action(job_id, "deny")


def unblock(pool: str = typer.Argument(..., help="Pool identifier")) -> None:
    """Remove a pool block set after a limit hit."""
    result = _job_request(lambda: _gateway(admin=True).post(f"/api/v1/pools/{pool}/unblock"))
    window = result.get("blocked_by")
    if not result.get("blocked") or not window:
        typer.echo(f"Pool {result['pool']}: unblocked")
        return
    until = result.get("blocked_until")
    when = f" until {format_local(until)}" if until else ""
    typer.echo(
        f"Pool {result['pool']}: manual block removed, but window {window} is exhausted{when}; "
        "the pool stays excluded until the window resets or a calibration shows room"
    )


def register(app: typer.Typer) -> None:
    """Register the job commands on the root app."""
    app.command()(run)
    app.command()(wait)
    app.command()(result)
    app.command()(rate)
    app.command()(cancel)
    app.command()(jobs)
    app.command()(approve)
    app.command()(deny)
    app.command()(unblock)
