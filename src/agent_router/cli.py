"""CLI of the local gateway."""

from __future__ import annotations

import io
import json
import math
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

import typer

from agent_router import __version__
from agent_router.auth import TokenFileError, ensure_tokens, load_tokens
from agent_router.client import GatewayClient, GatewayError, GatewayUnavailable
from agent_router.clock import SystemClock, format_local
from agent_router.config import (
    Config,
    ConfigError,
    Route,
    default_config_text,
    home_dir,
    load_config,
    model_key,
)
from agent_router.events import EventBus
from agent_router.export import ExportError
from agent_router.export import export as export_journal
from agent_router.jobs import TERMINAL, is_network_path
from agent_router.jobstore import open_jobstore
from agent_router.lifecycle import InstanceLocked, instance_lock
from agent_router.periods import parse_moment
from agent_router.registry import HARNESS_GROUP, harnesses, load_harness, resolve_backend
from agent_router.runner import ResolvedExecutable, resolve_executable
from agent_router.settings_store import SqliteSettings
from agent_router.store import open_store

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


def make_client(base_url: str, token: str) -> GatewayClient:
    return GatewayClient(base_url, token)


def _config(home: Path, *, err: bool = False) -> Config:
    path = home / "config.toml"
    if not path.exists():
        typer.echo(f"no config at {path}: run agent-router init first", err=err)
        raise typer.Exit(1)
    try:
        return load_config(path)
    except (ConfigError, OSError) as exc:
        typer.echo(f"config error: {exc}", err=err)
        raise typer.Exit(1) from exc


def _gateway(admin: bool = False, *, err: bool = False) -> GatewayClient:
    home = home_dir()
    config = _config(home, err=err)
    try:
        tokens = load_tokens(home)
    except TokenFileError as exc:
        typer.echo(str(exc), err=err)
        raise typer.Exit(1) from exc
    except (OSError, ValueError) as exc:
        typer.echo("failed to read tokens; run agent-router init", err=err)
        raise typer.Exit(1) from exc
    base_url = os.environ.get("AGENT_ROUTER_URL") or f"http://127.0.0.1:{config.port}"
    try:
        scheme = urlparse(base_url).scheme
    except ValueError:
        scheme = ""
    if scheme not in {"http", "https"}:
        typer.echo("config error: AGENT_ROUTER_URL must use http or https", err=err)
        raise typer.Exit(1)
    return make_client(base_url, tokens.admin if admin else tokens.client)


def _request(call: Callable[[], dict]) -> dict:
    try:
        return call()
    except GatewayUnavailable as exc:
        typer.echo("gateway unavailable: check that agent-router serve is running")
        raise typer.Exit(5) from exc
    except GatewayError as exc:
        if exc.status in (401, 403):
            typer.echo("gateway rejected the token")
            raise typer.Exit(5) from exc
        typer.echo(f"request denied: {exc.message}")
        raise typer.Exit(6 if 400 <= exc.status < 500 else 1) from exc


def _percent(value: Any) -> str:
    return "no data" if value is None else f"{float(value):.1%}"


def _found_harnesses() -> list[str]:
    """Harness entry-point names whose executable is on PATH."""
    found = []
    for entry in metadata.entry_points(group=HARNESS_GROUP):
        try:
            spec = load_harness(entry.name)
        except Exception as exc:  # a broken third-party plugin must not stop init
            typer.echo(f"Warning: harness {entry.name} failed to load: {exc}", err=True)
            continue
        if any(shutil.which(executable) for executable in spec.executables):
            found.append(entry.name)
    return sorted(found)


def _configured_harnesses(config: Config) -> set[str]:
    """Harnesses whose [backends.<name>] executable points to an existing file."""
    return {
        section.harness
        for section in config.backends.values()
        if section.executable and resolve_executable(section.executable).path is not None
    }


def _route_harness(config: Config, route: Route) -> str | None:
    if route.backend is None:
        return None
    return config.backend_section(route.backend).harness


_TIER_ORDER = ("standard", "cheap", "strong")


def _suggested_tier(routes: list[Route]) -> str | None:
    tiers = {tier for route in routes for tier in route.tiers}
    for tier in _TIER_ORDER:
        if tier in tiers:
            return tier
    return min(tiers) if tiers else None


def _missing_backend_hint(config: Config, route: Route, on_path: set[str]) -> str | None:
    """Why the backend of an enabled route cannot start, or None when its executable exists."""
    if route.backend is None:
        return None
    section = config.backend_section(route.backend)
    if section.executable:
        if resolve_executable(section.executable).path is not None:
            return None
        return (
            f"route {route.id} is enabled, but [backends.{route.backend}] executable "
            f"{section.executable} was not found: fix the path"
        )
    if section.harness in on_path:
        return None
    return (
        f"route {route.id} is enabled, but harness {section.harness} was not found: "
        f"install it or set [backends.{route.backend}] executable"
    )


def _init_hints(config: Config, found: list[str], on_path: set[str]) -> list[str]:
    hints = []
    if not found:
        hints.append(
            "no harness found: install and sign in to kimi, opencode, cline or claude, "
            "then add or uncomment the [[routes]] blocks of that harness in config.toml "
            "and set enabled = true where it is false"
        )

    for name in found:
        routes = [route for route in config.routes if _route_harness(config, route) == name]
        if not routes:
            hints.append(
                f"harness {name}: no routes in config.toml; add or uncomment [[routes]] "
                f'blocks with backend = "{name}" (see docs/examples)'
            )
        elif all(not route.enabled for route in routes):
            route_ids = ", ".join(route.id for route in routes)
            hints.append(
                f"harness {name}: routes are disabled in config.toml: {route_ids}; "
                "set enabled = true on one"
            )

    for route in config.routes:
        hint = _missing_backend_hint(config, route, on_path) if route.enabled else None
        if hint is not None:
            hints.append(hint)

    if "opencode" in found or "cline" in found:
        pool = next((pool for pool in config.pools if pool.id == "opencode-go"), None)
        if (
            pool is not None
            and pool.unit == "usd"
            and any(window.limit_usd == 10 for window in pool.windows)
        ):
            hints.append(
                "pool opencode-go: limit_usd = 10 is a placeholder, "
                "set your plan limits in config.toml"
            )

    enabled = [route for route in config.routes if route.enabled]
    tier = _suggested_tier(enabled)
    if tier is None:
        hints.append("no enabled route: jobs are rejected until a route is enabled")
    elif tier != "standard":
        hints.append(
            f"no enabled route for tier standard: run jobs with --tier {tier} or enable a route"
        )
    return hints


def _job_steps(config: Config) -> list[str]:
    """Next steps for a first read job; empty without an enabled read route or allowed root."""
    # the first job only reads, so only routes that take read jobs decide tier and approval
    readers = [route for route in config.routes if route.enabled and "read" in route.modes]
    tier = _suggested_tier(readers)
    if tier is None or not config.tasks.allowed_roots:
        return []
    routes = [route for route in readers if tier in route.tiers]
    tier_option = "" if tier == "standard" else f" --tier {tier}"
    cwd = _shell_quote_dir(config.tasks.allowed_roots[0])
    steps = [
        f"  check: agent-router advice --tier {tier}",
        f"  run: agent-router run --no-wait{tier_option} --cwd {cwd} "
        '--mode read "Describe this repository"',
    ]
    if any(route.confirm for route in routes):
        steps.append("  approve: agent-router approve <job-id>")
    steps.append("  wait: agent-router wait <job-id>")
    return steps


def _resolve_allowed_roots(raw_roots: list[str]) -> list[Path]:
    """Resolve --allowed-root values; nothing is created before they all check out."""
    roots: list[Path] = []
    for raw in raw_roots:
        # jobs reject such directories, so allowing one would only lead to cwd_not_allowed
        if is_network_path(raw):
            typer.echo(f"network and device paths are not accepted: {raw}")
            raise typer.Exit(1)
        candidate = Path(raw).expanduser()
        if not candidate.is_dir():
            typer.echo(f"not a directory: {candidate}")
            raise typer.Exit(1)
        roots.append(candidate.resolve())
    return list(dict.fromkeys(roots))


# a TOML table header such as [tasks], [ tasks ] # comment or [[routes]]
_TABLE_HEADER = re.compile(r"^\s*\[(?P<array>\[)?\s*(?P<name>[^\]]*?)\s*\]\]?\s*(#.*)?$")
_ALLOWED_ROOTS_LINE = re.compile(r"^(?P<indent>\s*)allowed_roots\s*=\s*(?P<value>.*)$")


def _toml_string_list(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"


def _split_inline_comment(value: str) -> tuple[str, str]:
    """Split a TOML value from its trailing comment; the comment keeps its leading spaces."""
    for position, char in enumerate(value):
        if char != "#":
            continue
        head = value[:position]
        try:
            tomllib.loads(f"x = {head}")
        except tomllib.TOMLDecodeError:
            continue
        stripped = head.rstrip()
        return stripped, value[len(stripped) :]
    return value, ""


def _replace_file(path: Path, data: bytes) -> None:
    """Write next to the file, then swap it in: a crash or a locked file leaves the old one."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _append_allowed_roots(config_path: Path, roots: list[Path]) -> bool:
    """Append unique roots to the single-line [tasks] allowed_roots list, other bytes intact.

    Returns whether any root was new.
    """
    try:
        load_config(config_path)
    except (ConfigError, OSError) as exc:
        typer.echo(f"cannot update {config_path}: {exc}")
        raise typer.Exit(1) from exc
    text = config_path.read_bytes().decode("utf-8")
    lines = text.splitlines(keepends=True)
    in_tasks = False
    for index, line in enumerate(lines):
        header = _TABLE_HEADER.match(line)
        if header is not None:
            in_tasks = header.group("array") is None and header.group("name") == "tasks"
            continue
        if not in_tasks:
            continue
        match = _ALLOWED_ROOTS_LINE.match(line.rstrip("\r\n"))
        if match is None:
            continue
        value, comment = _split_inline_comment(match.group("value").strip())
        try:
            existing = tomllib.loads(f"allowed_roots = {value}")["allowed_roots"]
        except tomllib.TOMLDecodeError:
            existing = None
        if not isinstance(existing, list) or not all(isinstance(item, str) for item in existing):
            typer.echo(
                f"cannot update {config_path}: "
                "tasks.allowed_roots must be a single-line list of strings"
            )
            raise typer.Exit(1)
        resolved = {Path(item).expanduser().resolve() for item in existing}
        merged = list(existing)
        for root in roots:
            if root not in resolved:
                merged.append(root.as_posix())
                resolved.add(root)
        ending = "\r\n" if line.endswith("\r\n") else "\n"
        lines[index] = (
            f"{match.group('indent')}allowed_roots = {_toml_string_list(merged)}{comment}{ending}"
        )
        try:
            _replace_file(config_path, "".join(lines).encode("utf-8"))
        except OSError as exc:
            typer.echo(f"cannot update {config_path}: {exc}")
            raise typer.Exit(1) from exc
        return len(merged) > len(existing)
    typer.echo(f"cannot update {config_path}: [tasks] has no allowed_roots line")
    raise typer.Exit(1)


_SHELL_SAFE = re.compile(r"[A-Za-z0-9_./:@%+=,-]+")


def _shell_arg(text: str) -> str:
    """Quote a value for a command the user copies; forward slashes keep "C:/" intact."""
    if _SHELL_SAFE.fullmatch(text):
        return text
    if sys.platform == "win32":
        return f'"{text}"'
    return shlex.quote(text)


def _shell_quote_dir(path: Path) -> str:
    return _shell_arg(path.as_posix())


@app.command("init")
def init_command(
    allowed_root: list[str] = typer.Option(
        [],
        "--allowed-root",
        help="Directory jobs may run in, added to tasks.allowed_roots; repeatable",
    ),
) -> None:
    """Create the gateway home directory, config.toml and access tokens."""
    roots = _resolve_allowed_roots(allowed_root)
    home = home_dir()
    try:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_path = home / "config.toml"
        found = _found_harnesses()
        on_path = set(found)
        existed = config_path.exists()
        if not existed:
            with config_path.open("x", encoding="utf-8") as stream:
                stream.write(default_config_text(found))
        added = bool(roots) and _append_allowed_roots(config_path, roots)
        ensure_tokens(home)
    except TokenFileError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except OSError as exc:
        typer.echo(f"failed to prepare gateway home directory: {exc}")
        raise typer.Exit(1) from exc
    config: Config | None = None
    config_error: str | None = None
    try:
        config = load_config(config_path)
    except (ConfigError, OSError) as exc:
        config_error = str(exc)
    if config is not None:
        found = sorted(on_path | _configured_harnesses(config))
    typer.echo(f"Gateway home directory: {home}")
    typer.echo(f"Config: {home / 'config.toml'}")
    typer.echo(f"Client token: {home / 'client.token'}")
    typer.echo(f"Admin token: {home / 'admin.token'}")
    typer.echo(f"Harnesses found: {', '.join(found) or 'none'}")
    if config is None:
        typer.echo(f"config.toml is invalid: {config_error}")
        raise typer.Exit(1)
    typer.echo("Jobs run without calibration; set the limits later with agent-router calibrate")
    if not config.tasks.allowed_roots:
        typer.echo(
            "Jobs are denied until you allow a directory: "
            f"agent-router init --allowed-root {_shell_quote_dir(Path.cwd())}"
        )
    for hint in _init_hints(config, found, on_path):
        typer.echo(hint)
    if existed and added:
        typer.echo("if the gateway is running, restart agent-router serve to apply the new roots")
    typer.echo("Next steps:")
    typer.echo("  serve: agent-router serve (keeps running; run the rest in another terminal)")
    typer.echo(f"  dashboard: http://127.0.0.1:{config.port}/")
    for step in _job_steps(config):
        typer.echo(step)


@app.command()
def serve(
    port: int | None = typer.Option(None, "--port", help="Port to listen on (default from config)"),
) -> None:
    """Run the gateway on 127.0.0.1."""
    home = home_dir()
    config = _config(home)
    try:
        with instance_lock(home):
            _serve(home, config, port)
    except InstanceLocked as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except OSError as exc:
        typer.echo(f"failed to lock gateway home directory: {exc}", err=True)
        raise typer.Exit(1) from exc


def _serve(home: Path, config: Config, port: int | None) -> None:
    # FastAPI and uvicorn take ~0.9 s to import; the other commands don't need them
    import uvicorn

    from agent_router.api import create_app
    from agent_router.service import build_dispatcher

    try:
        tokens = ensure_tokens(home)
    except TokenFileError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except OSError as exc:
        typer.echo(f"failed to prepare gateway home directory: {exc}")
        raise typer.Exit(1) from exc
    try:
        store = open_store(config.storage, home)
    except (ConfigError, OSError, sqlite3.Error) as exc:
        typer.echo(f"failed to open the store: {exc}")
        raise typer.Exit(1) from exc
    try:
        jobs = open_jobstore(config.storage, home, store)
    except (ConfigError, OSError, sqlite3.Error) as exc:
        store.close()
        typer.echo(f"failed to open the store: {exc}")
        raise typer.Exit(1) from exc
    clock = SystemClock()
    bus = EventBus(clock)
    tz = None  # the system zone at each formatting moment, so daylight saving time is followed
    dispatcher = build_dispatcher(config, store, jobs, bus, clock, home, tz)
    application = create_app(
        config,
        store,
        clock,
        tokens,
        home=home,
        bus=bus,
        dispatcher=dispatcher,
        background=True,
        tz=tz,
    )
    if port is not None and port != config.port:
        # client commands and init read server.port, not the --port of this process
        typer.echo(
            f"listening on port {port}, not server.port {config.port}: set "
            f"AGENT_ROUTER_URL=http://127.0.0.1:{port} for client commands; "
            f"dashboard: http://127.0.0.1:{port}/"
        )
    listen_port = config.port if port is None else port
    try:
        uvicorn.run(application, host="127.0.0.1", port=listen_port)
    except SystemExit as exc:
        if exc.code in (0, None):
            raise
        # uvicorn has logged the raw error and exits with 3, which reads as "job failed" here
        if _port_in_use(listen_port):
            typer.echo(
                f"port {listen_port} on 127.0.0.1 is in use: stop the program that holds it "
                "or run agent-router serve --port <free port>"
            )
        else:
            typer.echo("gateway failed to start: see the error above")
        raise typer.Exit(1) from exc


def _port_in_use(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


@app.command()
def status() -> None:
    """Show pool usage, pace and window resets."""
    snapshot = _request(lambda: _gateway().get("/api/v1/snapshot"))
    for pool in snapshot["pools"]:
        windows = pool["windows"]
        calibrated = any(window.get("source") for window in windows)
        header = f"{pool['id']} — {pool['title']}"
        if not calibrated:
            header += " (not calibrated)"
        typer.echo(header)
        for window in windows:
            typer.echo(_status_window_line(window))
            note = window.get("note")
            if note:
                typer.echo(f"    note: {note}")
        for line in _status_pool_lines(pool):
            typer.echo(line)
        # the pace window drives routing, so the hint stays until it has data
        paces = [window for window in windows if window.get("role", "pace") == "pace"]
        target = next(iter(paces or windows), None)
        if target is not None and not target.get("source"):
            typer.echo(
                "  calibrate: agent-router calibrate "
                f"{_shell_arg(pool['id'])} {_shell_arg(target['id'])} <percent>"
            )


def _status_window_line(window: dict) -> str:
    resets_at = window.get("resets_at")
    parts = [f"used {_percent(window.get('used'))}"]
    if window.get("role", "pace") == "pace":
        # a guard window only caps usage, it has no expected pace
        parts.append(f"expected {_percent(window.get('expected'))}")
    parts.append(f"reset: {format_local(resets_at) if resets_at else 'unknown'}")
    parts.append(f"source: {window.get('source') or 'no data'}")
    if window.get("blocked"):
        parts.append("exhausted")
    return f"  {window['id']}: " + "; ".join(parts)


def _status_pool_lines(pool: dict) -> list[str]:
    """Why the pool is excluded right now, in the words advice uses, and the gateway's own load."""
    lines = []
    block = pool.get("block")
    if block:
        lines.append(f"  closed until {format_local(block['until'])}: {block['reason']}")
        lines.append(f"  unblock early: agent-router unblock {_shell_arg(pool['id'])}")
    window = pool.get("blocked_by")
    if window:
        until = pool.get("blocked_until")
        when = f" until {format_local(until)}" if until else ""
        lines.append(f"  excluded: window {window} exhausted{when}")
    via = pool.get("via_gateway") or {}
    if via.get("jobs"):
        # a percent pool does not move with these jobs, so they tell when to calibrate again
        jobs = f"{via['jobs']} job" if via["jobs"] == 1 else f"{via['jobs']} jobs"
        lines.append(
            f"  via gateway since the last observation: {jobs}, "
            f"{via.get('minutes') or 0:g} min, {via.get('tokens') or 0} tokens"
        )
    return lines


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


def _export_moment(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        typer.echo("invalid date: expected ISO 8601 format")
        raise typer.Exit(1) from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.astimezone()
    return moment


@app.command()
def purge(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show how many jobs would be deleted without deleting them"
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
        purged, failed_dirs = jobs.purge(before, home / "jobs", dry_run=dry_run)
    except (ConfigError, OSError, sqlite3.Error) as exc:
        typer.echo(f"purge failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        if store is not None:
            store.close()
    count = len(purged)
    noun = "job" if count == 1 else "jobs"
    typer.echo(f"{'would purge' if dry_run else 'purged'} {count} {noun}")
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


def _doctor_callers(config: Config) -> None:
    """Warn when a mapped caller has no enabled route outside its own pool."""
    enabled = [route for route in config.routes if route.enabled]
    for name, pool in config.callers.items():
        if pool is None:
            continue
        if enabled and all(route.pool == pool for route in enabled):
            typer.echo(
                f"Warning: caller {name}: every enabled route is on its own pool "
                f"{pool}, so its delegated jobs have no route"
            )


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
    """Pool model weights and route prices: table date, missing limits, routes without a price."""
    today = datetime.now().date()
    for pool in config.pools:
        if pool.limit_base_usd is None:
            continue
        if pool.model_limits_date is not None:
            try:
                days = (today - date.fromisoformat(pool.model_limits_date)).days
            except ValueError:
                days = None
            if days is not None:
                typer.echo(
                    f"Pool {pool.id}: model_limits from {pool.model_limits_date} ({days} days ago)"
                )
        for route in config.routes:
            if not route.enabled or route.pool != pool.id:
                continue
            key = model_key(route.model) if route.model else route.model_group
            if key not in pool.model_limits:
                typer.echo(
                    f"Warning: route {route.id}: model {key} missing from model_limits "
                    f"of pool {pool.id}, weight 1.0"
                )
    for pool in config.pools:
        if pool.price_file is None or pool.prices is None:
            continue
        for route in config.routes:
            if not route.enabled or route.pool != pool.id:
                continue
            keys = (
                [model_key(route.model), route.model_group] if route.model else [route.model_group]
            )
            if any(key in pool.prices.models for key in keys):
                continue
            typer.echo(
                f"Warning: route {route.id}: no price for model {keys[0]} "
                f"in price_file of pool {pool.id}"
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
    """Check the setup: config, store, harnesses, weights — and optionally the live gateway."""
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
    _doctor_classifier(config)
    _doctor_callers(config)
    if not config.tasks.allowed_roots:
        typer.echo("Warning: tasks.allowed_roots is empty, jobs not accepted")
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
    if live and not _doctor_live(config, client):
        raise typer.Exit(1)


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


@app.command()
def run(
    text: str | None = typer.Argument(None, help="Job prompt text, or - for standard input"),
    cwd: Path = typer.Option(Path("."), "--cwd", help="Working directory for the job"),
    mode: str = typer.Option("read", "--mode", help="Job mode: read or write"),
    tier: str | None = typer.Option(None, "--tier", help="Job tier: cheap, standard or strong"),
    caller: str | None = typer.Option(
        None, "--caller", help="Name of the calling agent, e.g. claude-code"
    ),
    route: str | None = typer.Option(
        None, "--route", help="Use this route instead of letting the gateway choose"
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
    body: dict[str, Any] = {
        "prompt": _prompt(text, prompt_file),
        "cwd": str(cwd.resolve()),
        "mode": mode,
    }
    for name, value in (
        ("tier", tier),
        ("caller", caller),
        ("route", route),
        ("timeout_s", timeout),
    ):
        if value is not None:
            body[name] = value
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


@app.command()
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


@app.command()
def result(
    job_id: str = typer.Argument(..., help="Job identifier"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of text"),
) -> None:
    """Print the status and result of a job without waiting."""
    job = _job_request(lambda: _gateway().get(f"/api/v1/jobs/{job_id}"))
    _emit_job(job, as_json)


@app.command()
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


@app.command()
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


@app.command()
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


@app.command()
def approve(job_id: str = typer.Argument(..., help="Job identifier")) -> None:
    """Approve a job that awaits confirmation."""
    _approve_action(job_id, "approve")


@app.command()
def deny(job_id: str = typer.Argument(..., help="Job identifier")) -> None:
    """Deny a job that awaits confirmation."""
    _approve_action(job_id, "deny")


@app.command()
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


@app.command()
def mcp() -> None:
    """Run an MCP server over stdio for orchestrator agents."""
    # imported lazily so the other commands don't pay for the mcp package
    from agent_router import mcp_server

    # stdout belongs to the MCP protocol, so setup failures go to stderr
    mcp_server.serve(_gateway(err=True))
