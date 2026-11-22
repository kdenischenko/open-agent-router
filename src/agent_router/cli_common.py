"""Helpers shared by the CLI command modules."""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import typer

from agent_router.auth import TokenFileError, load_tokens
from agent_router.client import GatewayClient, GatewayError, GatewayUnavailable
from agent_router.config import Config, ConfigError, Route, home_dir, load_config


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


def _export_moment(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        typer.echo("invalid date: expected ISO 8601 format")
        raise typer.Exit(1) from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.astimezone()
    return moment


def _route_harness(config: Config, route: Route) -> str | None:
    if route.backend is None:
        return None
    return config.backend_section(route.backend).harness
