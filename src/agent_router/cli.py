"""CLI of the local gateway."""

from __future__ import annotations

import io
import json
import math
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import typer

from agent_router import __version__, cli_gateway, cli_jobs
from agent_router.cli_common import _config, _export_moment, _gateway, _request
from agent_router.cli_jobs import _job_failure, _job_request
from agent_router.client import GatewayClient, GatewayError, GatewayUnavailable
from agent_router.clock import SystemClock, format_age, format_local
from agent_router.config import Config, ConfigError, home_dir
from agent_router.diagnostics import (
    DoctorFinding,
    caller_findings,
    spend_findings,
    task_findings,
    usage_findings,
    weight_findings,
)
from agent_router.export import ExportError
from agent_router.export import export as export_journal
from agent_router.jobs import TERMINAL
from agent_router.jobstore import open_jobstore
from agent_router.periods import parse_moment
from agent_router.registry import HARNESS_GROUP, harnesses, load_harness, resolve_backend
from agent_router.runner import ResolvedExecutable, resolve_executable
from agent_router.settings_store import SqliteSettings
from agent_router.store import open_store
from agent_router.usage_reader import usage_drift_warnings

app = typer.Typer(no_args_is_help=True)
_OLLAMA_TIMEOUT_S = 5.0
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"agent-router {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the version and exit",
        is_eager=True,
        callback=_print_version,
    ),
) -> None:
    """Route coding jobs to AI coding agents by the limits left on your subscriptions."""


def _utf8_output() -> None:
    # piped output on Windows uses the ANSI code page, and Cyrillic crashed the command; a real
    # Windows console is already UTF-8, and NUL is a character device, so isatty() is true for it
    for stream in (sys.stdout, sys.stderr):
        if not isinstance(stream, io.TextIOWrapper):
            continue
        utf8 = (stream.encoding or "").lower().replace("-", "").replace("_", "") == "utf8"
        if stream.isatty() and sys.platform != "win32":
            if not utf8:
                stream.reconfigure(errors="replace")
            continue
        if not utf8:
            stream.reconfigure(encoding="utf-8")


def main() -> None:
    """Console entry point: UTF-8 streams first, because click renders --help while parsing."""
    _utf8_output()
    app()


cli_gateway.register(app)


@app.command()
def advice(
    tier: str = typer.Option("standard", "--tier", help="Job tier: cheap, standard or strong"),
    caller: str | None = typer.Option(
        None, "--caller", help="Name of the calling agent, e.g. claude-code"
    ),
    mode: str | None = typer.Option(
        None, "--mode", help="Job mode: read or write; without it routes of either mode match"
    ),
) -> None:
    """Show which routes match a job and why the others are excluded."""
    result = _request(lambda: _gateway().get("/api/v1/advice", tier=tier, caller=caller, mode=mode))
    typer.echo(f"Tier: {result['tier']}")
    if result.get("mode"):
        typer.echo(f"Mode: {result['mode']}")
    for item in result["chosen"]:
        typer.echo(f"Matching: {item['route']} ({item['pool']}) — {item['reason']}")
    for item in result["excluded"]:
        typer.echo(f"Excluded: {item['route']} ({item['pool']}) — {item['reason']}")


@app.command()
def calibrate(
    pool: str = typer.Argument(..., help="Pool identifier"),
    window: str = typer.Argument(..., help="Window id, e.g. 5h or month"),
    percent: float = typer.Argument(..., help="Used share of the window, in percent"),
    resets_at: str | None = typer.Option(
        None,
        "--resets-at",
        help='Window reset date and optional local time, e.g. 2026-12-01, "01.12.2026 14:00"',
    ),
    resets_in: str | None = typer.Option(
        None, "--resets-in", help="Time until the window resets, e.g. 3h20m"
    ),
) -> None:
    """Record the usage your subscription shows for a pool window."""
    if not (math.isfinite(percent) and 0 <= percent <= 100):
        typer.echo("percent must be between 0 and 100", err=True)
        raise typer.Exit(1)
    body: dict[str, Any] = {"pool": pool, "window": window, "percent": percent}
    if resets_at is not None:
        try:
            moment = parse_moment(resets_at)
        except ValueError as exc:
            typer.echo(
                "invalid reset time: expected a date such as 2026-12-01 or 01.12.2026, "
                "optionally with a time such as 14:00"
            )
            raise typer.Exit(1) from exc
        body["resets_at"] = moment.isoformat()
    if resets_in is not None:
        body["resets_in"] = resets_in
    result = _request(lambda: _gateway(admin=True).post("/api/v1/observations", body))
    line = f"Recorded: {result['pool']} / {result['window']}, {percent:g}%"
    reset = result.get("resets_at") or result.get("window_resets_at")
    if reset:
        line += f", resets {format_local(reset)}"
    typer.echo(line)


usage_app = typer.Typer(help="Read the usage sources of your plans")
app.add_typer(usage_app, name="usage")


@usage_app.command("refresh")
def usage_refresh(
    pool: str | None = typer.Argument(
        None, help="Pool identifier; without it every pool with a usage source"
    ),
) -> None:
    """Read the usage sources now and record what they report."""
    body: dict[str, Any] = {} if pool is None else {"pool": pool}
    result = _request(lambda: _gateway(admin=True).post("/api/v1/usage/refresh", body))
    for item in result.get("read", []):
        typer.echo(f"Read: {item['pool']} ({', '.join(item['windows'])})")
    for item in result.get("failed", []):
        typer.echo(f"Failed: {item['pool']} — {item['reason']}")
    for item in result.get("skipped", []):
        typer.echo(f"Skipped: {item['pool']} — {item['reason']}")


@app.command()
def purge(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be deleted without deleting it"
    ),
) -> None:
    """Delete terminal jobs older than storage.retention_days and their local files."""
    home = home_dir()
    config = _config(home)
    retention_days = config.storage.retention_days
    if retention_days is None:
        typer.echo("retention is disabled: set storage.retention_days in config.toml")
        return
    store = None
    try:
        store = open_store(config.storage, home)
        jobs = open_jobstore(config.storage, home, store)
        before = SystemClock().now() - timedelta(days=retention_days)
        purged, failed_dirs = jobs.purge(before, home / "jobs", dry_run=True)
        row_ids = {job_id for job_id in purged if jobs.get(job_id) is not None}
        if not dry_run:
            purged, failed_dirs = jobs.purge(before, home / "jobs")
    except (ConfigError, OSError, sqlite3.Error) as exc:
        typer.echo(f"purge failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        if store is not None:
            store.close()
    row_count = sum(job_id in row_ids for job_id in purged)
    entry_count = len(purged) - row_count
    messages = []
    if row_count or not entry_count:
        noun = "job" if row_count == 1 else "jobs"
        messages.append(f"{'would purge' if dry_run else 'purged'} {row_count} {noun}")
    if entry_count:
        noun = "entry" if entry_count == 1 else "entries"
        messages.append(f"{'would remove' if dry_run else 'removed'} {entry_count} job {noun}")
    typer.echo(" and ".join(messages))
    for directory in failed_dirs:
        typer.echo(f"warning: could not remove {directory}", err=True)
    if failed_dirs:
        raise typer.Exit(1)


@app.command("export")
def export_command(
    since: str = typer.Option(..., "--since", help="Start time in ISO 8601 format"),
    until: str | None = typer.Option(None, "--until", help="End time in ISO 8601 format"),
    fmt: str = typer.Option("jsonl", "--format", help="Output format: jsonl or csv"),
    out: Path | None = typer.Option(None, "--out", help="Output directory"),
) -> None:
    """Export the job journal as JSON Lines or CSV."""
    since_moment = _export_moment(since)
    until_moment = None if until is None else _export_moment(until)
    if fmt not in ("jsonl", "csv"):
        typer.echo("invalid export format: expected jsonl or csv")
        raise typer.Exit(1)
    home = home_dir()
    config = _config(home)
    if config.storage.backend != "sqlite":
        typer.echo("export supports only the sqlite store")
        raise typer.Exit(1)
    db_path = home / "agent-router.sqlite"
    if not db_path.exists():
        typer.echo(f"no database: {db_path}")
        raise typer.Exit(1)
    out_dir = out or Path(f"agent-router-export-{datetime.now():%Y%m%d}")
    route_models = {route.id: route.model for route in config.routes if route.model}
    primary_model = config.classifier.primary_model
    primary = None if primary_model is None else primary_model.id
    try:
        paths = export_journal(
            db_path, out_dir, since_moment, until_moment, fmt, route_models, primary
        )
    except FileNotFoundError as exc:
        typer.echo(f"no database: {db_path}")
        raise typer.Exit(1) from exc
    except (ExportError, sqlite3.Error) as exc:
        typer.echo(f"export failed: {exc}")
        raise typer.Exit(1) from exc
    for path in paths:
        typer.echo(path)


classifier_app = typer.Typer(help="Inspect the job tier classifier")
app.add_typer(classifier_app, name="classifier")


@classifier_app.command("report")
def classifier_report(
    since: str | None = typer.Option(None, "--since", help="Start time in ISO 8601 format"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of text"),
) -> None:
    """Show classifier accuracy and vote counts."""
    from agent_router import classifier

    home = home_dir()
    config = _config(home)
    if config.storage.backend != "sqlite":
        typer.echo("classifier report supports only the sqlite store")
        raise typer.Exit(1)
    store = open_store(config.storage, home)
    try:
        jobs = open_jobstore(config.storage, home, store)
        settings = SqliteSettings(store)
        moment = _export_moment(since) if since else _EPOCH
        result = classifier.report(jobs.votes_since(moment), settings.get("classifier_mode_since"))
    finally:
        store.close()
    if as_json:
        typer.echo(json.dumps(result, ensure_ascii=False))
        return
    split = result.get("split")
    if split is not None:
        before = sum(group["votes"] for group in split["before"])
        after = sum(group["votes"] for group in split["after"])
        typer.echo(
            f"mode {split['mode']} since {split['since']}: "
            f"{before} votes before, {after} votes after"
        )
    if not result["classifiers"]:
        typer.echo("no votes yet")
        return
    for group in result["classifiers"]:
        kind = "offline" if group["offline"] else "live"
        accuracy = group["truth"]["accuracy"]
        typer.echo(
            f"Classifier {group['classifier_id']} ({group['model']}, {kind}): "
            f"{group['votes']} votes, {group['errors']} errors, "
            f"accuracy {'no data' if accuracy is None else f'{accuracy:.1%}'}"
        )


@classifier_app.command("eval")
def classifier_eval(
    models: str = typer.Option(..., "--models", help="Comma-separated classifier identifiers"),
    since: str | None = typer.Option(None, "--since", help="Start time in ISO 8601 format"),
) -> None:
    """Evaluate classifier models offline on stored prompts."""
    from agent_router import classifier

    home = home_dir()
    config = _config(home)
    if config.storage.backend != "sqlite":
        typer.echo("offline eval supports only the sqlite store")
        raise typer.Exit(1)
    store = open_store(config.storage, home)
    try:
        jobs = open_jobstore(config.storage, home, store)
        settings = SqliteSettings(store)
        selected = [item.strip() for item in models.split(",") if item.strip()]
        known = {item.id: item for item in config.classifier.effective_models}
        unknown = [item for item in selected if item not in known]
        if unknown:
            typer.echo(f"unknown classifier: {unknown[0]}")
            raise typer.Exit(1)
        chosen = [known[item] for item in selected]
        moment = _export_moment(since) if since else _EPOCH
        prompts = jobs.prompts_since(moment)
        live = [item for item in config.classifier.effective_models if item.live]
        try:
            count = classifier.run_eval(
                prompts,
                chosen,
                live=live,
                flags=settings,
                add_vote=jobs.add_vote,
                now=SystemClock().now,
                pid=os.getpid(),
                post=classifier.post,
                get=classifier.get,
                clock=time.monotonic,
                sleep=time.sleep,
            )
        except classifier.EvalError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1) from exc
    finally:
        store.close()
    typer.echo(f"votes recorded: {count}")


def _doctor_classifier(config: Config) -> None:
    """Local Ollama memory and budget; other classifier endpoints report their key state."""
    from agent_router import classifier

    settings = config.classifier
    models = settings.effective_models
    grouped: dict[str, list[Any]] = {}
    for model in models:
        if not classifier.is_local_ollama(model):
            parsed = urlparse(model.url)
            host = parsed.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            port = "" if parsed.port is None else f":{parsed.port}"
            line = (
                f"Classifier {model.id} ({model.model}): {model.kind} at "
                f"{parsed.scheme}://{host}{port}"
            )
            if model.api_key_env is not None:
                state = "set" if os.environ.get(model.api_key_env) else "missing"
                line += f", key from ${model.api_key_env}: {state}"
            typer.echo(line)
            continue
        grouped.setdefault(model.url.rstrip("/"), []).append(model)
    total = 0
    for url, local_models in grouped.items():
        try:
            tags = {
                item.get("name")
                for item in classifier.get(f"{url}/api/tags", timeout=_OLLAMA_TIMEOUT_S)
                .json()
                .get("models", [])
                if isinstance(item, dict)
            }
            ps = classifier.get(f"{url}/api/ps", timeout=_OLLAMA_TIMEOUT_S).json().get("models", [])
            loaded = {
                item.get("name"): round(int(item.get("size_vram") or 0) / (1024 * 1024))
                for item in ps
                if isinstance(item, dict)
            }
        except Exception:
            typer.echo("Ollama unavailable")
            continue
        for model in local_models:
            if model.model not in tags:
                typer.echo(f"Classifier {model.id} ({model.model}): not in Ollama")
                continue
            size = loaded.get(model.model)
            if size is None:
                typer.echo(f"Classifier {model.id} ({model.model}): not loaded")
                continue
            typer.echo(f"Classifier {model.id} ({model.model}): size_vram {size} MiB")
            if model.live:
                total += size
    if not grouped:
        return
    typer.echo(f"Live classifiers: {total} MiB with a budget of {settings.vram_budget_mb} MiB")
    if total > settings.vram_budget_mb:
        typer.echo(
            f"Warning: live classifiers take {total} MiB "
            f"with a budget of {settings.vram_budget_mb} MiB"
        )


def _print_doctor_findings(findings: list[DoctorFinding]) -> None:
    for finding in findings:
        prefix = "Warning: " if finding.level == "warning" else ""
        typer.echo(f"{prefix}{finding.message}")


def _doctor_callers(config: Config) -> None:
    _print_doctor_findings(caller_findings(config))


def _doctor_store(config: Config, home: Path) -> None:
    store = None
    try:
        store = open_store(config.storage, home)
        typer.echo(f"Store: available ({config.storage.backend})")
        if config.statusline is None:
            typer.echo("Last status line signal: not configured")
            return
        observations = [
            store.last_observation(config.statusline.pool, window)
            for window in (config.statusline.five_hour, config.statusline.seven_day)
        ]
        signals = [
            obs for obs in observations if obs is not None and obs.source == "claude_statusline"
        ]
        if not signals:
            typer.echo("Last status line signal: no data")
            return
        age = max(
            0.0, (SystemClock().now() - max(obs.observed_at for obs in signals)).total_seconds()
        )
        typer.echo(f"Last status line signal: {age:.0f} s ago")
    except (ConfigError, OSError, RuntimeError, sqlite3.Error) as exc:
        typer.echo(f"Store: error: {exc}")
        raise typer.Exit(1) from exc
    finally:
        if store is not None:
            store.close()


def _harness_version(exe: ResolvedExecutable) -> str | None:
    if exe.path is None:
        return None
    try:
        done = subprocess.run(
            [str(exe.path), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    output = done.stdout or done.stderr
    return next(
        (line.strip() for line in output.decode("utf-8", "replace").splitlines() if line.strip()),
        None,
    )


def _doctor_backends(config: Config) -> None:
    typer.echo("Harness | Found | Launch | Version | Enabled routes")
    configured = harnesses(config)
    builtins = {entry.name for entry in metadata.entry_points(group=HARNESS_GROUP)}
    for name in sorted(builtins | set(configured)):
        spec = configured.get(name) or load_harness(name)
        section = config.backend_section(name)
        exe = resolve_backend(section, spec, resolve_executable)
        label = name if section.harness == name else f"{name} ({section.harness})"
        routes = [route.id for route in config.routes if route.enabled and route.backend == name]
        if exe.path is None:
            launch = "—"
            version = "—"
        else:
            kind = "shim" if exe.is_shim else "executable"
            launch = f"{exe.path} ({kind})"
            version = _harness_version(exe) or "failed to detect"
        typer.echo(
            f"{label} | {'yes' if exe.path is not None else 'no'} | {launch} | "
            f"{version} | {', '.join(routes) or '—'}"
        )
        if exe.path is None:
            for route in routes:
                typer.echo(f"Warning: route {route} is enabled, but harness {name} not found")


def _doctor_weights(config: Config) -> None:
    _print_doctor_findings(weight_findings(config))


def _doctor_spend(config: Config) -> None:
    _print_doctor_findings(spend_findings(config))


def _doctor_usage(config: Config) -> None:
    _print_doctor_findings(usage_findings(config))


def _doctor_usage_state(config: Config, client: GatewayClient) -> None:
    """Read health from the running gateway: read problems and stale usage data per pool."""
    if not any(pool.usage.source is not None for pool in config.pools):
        return
    snapshot = _request(lambda: client.get("/api/v1/snapshot"))
    configured = {pool.id: pool for pool in config.pools}
    for pool in snapshot.get("pools", []):
        info = pool.get("usage")
        if info is None:
            continue
        settings = configured.get(pool["id"])
        if settings is not None and info["mode"] != "manual":
            for warning in usage_drift_warnings(
                info["source"],
                info.get("missing_fields", []),
                info.get("format_changed", False),
                settings.usage.windows,
            ):
                typer.echo(f"Warning: pool {pool['id']}: {warning}")
        if info.get("last_error"):
            typer.echo(f"Warning: pool {pool['id']}: last usage read failed: {info['last_error']}")
        if info.get("warning"):
            typer.echo(f"Warning: pool {pool['id']}: last usage read: {info['warning']}")
        if not info.get("stale"):
            continue
        age = info.get("age_s")
        if age is None:
            typer.echo(f"Warning: pool {pool['id']}: usage data is stale, no successful read yet")
        else:
            typer.echo(
                f"Warning: pool {pool['id']}: usage data is stale, "
                f"no successful read for {format_age(age)}"
            )


def _doctor_live(config: Config, client: GatewayClient) -> bool:
    if not config.tasks.allowed_roots:
        typer.echo("Warning: allowed_roots is empty — live probes skipped")
        return True
    ok = True
    probed = False
    seen: set[str] = set()
    cwd = config.tasks.allowed_roots[0]
    for route in config.routes:
        if (
            not route.enabled
            or route.confirm
            or route.backend is None
            or route.backend in seen
            or "read" not in route.modes
        ):
            continue
        seen.add(route.backend)
        probed = True
        body = {
            "prompt": "Reply with exactly: ok",
            "cwd": str(cwd.resolve()),
            "mode": "read",
            "route": route.id,
        }
        submitted = _job_request(lambda body=body: client.post("/api/v1/jobs", body))
        job_id = submitted["job_id"]
        job = _job_request(
            lambda job_id=job_id: client.get(f"/api/v1/jobs/{job_id}/wait", timeout=100)
        )
        status = job["status"]
        if status not in TERMINAL:
            try:
                client.post(f"/api/v1/jobs/{job_id}/cancel")
            except GatewayUnavailable as exc:
                _job_failure(exc)
            except GatewayError as exc:
                if exc.status != 409:
                    _job_failure(exc)
                job = _job_request(lambda job_id=job_id: client.get(f"/api/v1/jobs/{job_id}"))
                status = job["status"]
                if status not in TERMINAL:
                    ok = False
                    typer.echo(f"Live {route.id}: {status} — cancel failed, job still running")
                    continue
            else:
                ok = False
                if status == "awaiting_approval":
                    typer.echo(f"Live {route.id}: awaiting approval — cancelled")
                else:
                    typer.echo(f"Live {route.id}: did not complete within 100 s — cancelled")
                continue
        if status == "completed":
            typer.echo(f"Live {route.id}: completed")
            continue
        ok = False
        code = (job.get("error") or {}).get("code")
        suffix = f" ({code})" if code else ""
        typer.echo(f"Live {route.id}: {status}{suffix}")
    if not probed:
        typer.echo("no routes to probe (disabled, confirm or write-only routes are skipped)")
        return False
    return ok


def statusline_command(home: Path, platform: str = sys.platform) -> str:
    """Status-line command with the path shell-escaped; ValueError means the path is unsafe."""
    text = str(home)
    if "\n" in text or "\r" in text:
        raise ValueError("newline in the gateway home directory path is not allowed")
    if platform != "win32":
        return f"agent-router-statusline --home {shlex.quote(text)}"
    path = home.as_posix()
    if any(character in path for character in '"%!^&|<>$`'):
        raise ValueError("gateway home directory path contains a shell-unsafe character")
    return f'agent-router-statusline --home "{path}"'


@app.command()
def doctor(
    statusline_snippet: bool = typer.Option(
        False, "--statusline-snippet", help="Print the Claude Code status line settings snippet"
    ),
    live: bool = typer.Option(
        False, "--live", help="Also run a short read job on each enabled harness"
    ),
) -> None:
    """Check the setup: config, store, harnesses, weights, usage — and the live gateway."""
    home = home_dir()
    if statusline_snippet:
        expanded = home.expanduser()
        try:
            home = expanded.resolve()
        except (OSError, RuntimeError):
            home = expanded.absolute()
        try:
            command = statusline_command(home)
        except ValueError as exc:
            typer.echo(
                "gateway home directory path is unsafe for the command line: "
                "set AGENT_ROUTER_HOME to a simple path",
                err=True,
            )
            raise typer.Exit(1) from exc
        typer.echo(
            json.dumps(
                {"statusLine": {"type": "command", "command": command}},
                ensure_ascii=False,
            )
        )
        return
    config = _config(home)
    typer.echo("Config: loaded")
    _doctor_store(config, home)
    _doctor_backends(config)
    _doctor_weights(config)
    _doctor_spend(config)
    _doctor_classifier(config)
    _doctor_callers(config)
    _doctor_usage(config)
    _print_doctor_findings(task_findings(config))
    client = _gateway()
    stats = _request(lambda: client.get("/api/v1/events/stats"))
    typer.echo(f"Gateway: available, event #{stats['seq']}")
    for name, counters in stats["subscribers"].items():
        typer.echo(
            f"Subscriber {name}: delivered={counters['delivered']} "
            f"dropped={counters['dropped']} errors={counters['errors']}"
        )
        if counters["dropped"] or counters["errors"]:
            typer.echo(f"Warning: subscriber {name} has drops or errors")
    _doctor_usage_state(config, client)
    if live and not _doctor_live(config, client):
        raise typer.Exit(1)


cli_jobs.register(app)


@app.command()
def mcp() -> None:
    """Run an MCP server over stdio for orchestrator agents."""
    # imported lazily so the other commands don't pay for the mcp package
    from agent_router import mcp_server

    # stdout belongs to the MCP protocol, so setup failures go to stderr
    mcp_server.serve(_gateway(err=True))
