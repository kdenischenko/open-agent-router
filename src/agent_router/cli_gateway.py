"""Gateway lifecycle commands: init, serve and status."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import tomllib
from importlib import metadata
from pathlib import Path

import typer

from agent_router.auth import TokenFileError, ensure_tokens
from agent_router.cli_common import (
    _config,
    _gateway,
    _percent,
    _request,
    _route_harness,
    _shell_arg,
    _shell_quote_dir,
)
from agent_router.clock import SystemClock, format_local
from agent_router.config import (
    Config,
    ConfigError,
    Route,
    default_config_text,
    home_dir,
    load_config,
)
from agent_router.events import EventBus
from agent_router.jobs import is_network_path
from agent_router.jobstore import open_jobstore
from agent_router.lifecycle import InstanceLocked, instance_lock
from agent_router.registry import HARNESS_GROUP, load_harness
from agent_router.runner import resolve_executable
from agent_router.store import open_store


def register(app: typer.Typer) -> None:
    """Register the gateway lifecycle commands on the root app."""
    app.command("init")(init_command)
    app.command()(serve)
    app.command()(status)


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
