"""Configuration findings shared by the doctor command and the dashboard."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from agent_router.cli_common import _route_harness
from agent_router.config_model import Config, ConfigError, model_key
from agent_router.registry import load_counter, load_usage_source


@dataclass(frozen=True)
class DoctorFinding:
    level: Literal["info", "warning"]
    message: str
    hint: str | None = None


def weight_findings(config: Config) -> list[DoctorFinding]:
    """Pool model weights and route prices: table date, missing limits, routes without a price."""
    findings: list[DoctorFinding] = []
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
                findings.append(
                    DoctorFinding(
                        "info",
                        f"Pool {pool.id}: model_limits from {pool.model_limits_date} "
                        f"({days} days ago)",
                    )
                )
        for route in config.routes:
            if not route.enabled or route.pool != pool.id:
                continue
            key = model_key(route.model) if route.model else route.model_group
            if key not in pool.model_limits:
                findings.append(
                    DoctorFinding(
                        "warning",
                        f"route {route.id}: model {key} missing from model_limits "
                        f"of pool {pool.id}, weight 1.0",
                        "add the model to [pools.model_limits] for this pool",
                    )
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
            findings.append(
                DoctorFinding(
                    "warning",
                    f"route {route.id}: no price for model {keys[0]} "
                    f"in price_file of pool {pool.id}",
                    "add the model to this pool's price_file",
                )
            )
    return findings


def spend_findings(config: Config) -> list[DoctorFinding]:
    """Warn when a route's counter spend cannot be seen by its pool."""
    findings: list[DoctorFinding] = []
    for route in config.routes:
        if not route.enabled or config.route_spend(route) != "counter":
            continue
        pool = next((item for item in config.pools if item.id == route.pool), None)
        harness = _route_harness(config, route)
        if pool is None or harness is None:
            continue
        if pool.counter is None:
            findings.append(
                DoctorFinding(
                    "warning",
                    f"route {route.id}: pool {pool.id} has no counter, so its spend is "
                    'invisible; set spend = "journal"',
                    'set spend = "journal" on the route',
                )
            )
            continue
        try:
            counter = load_counter(pool.counter)
        except ConfigError:
            continue  # an unknown counter is rejected when the config loads
        if harness not in counter.sees:
            findings.append(
                DoctorFinding(
                    "warning",
                    f"route {route.id}: counter {pool.counter} does not see harness "
                    f'{harness}, so its spend is invisible; set spend = "journal"',
                    'set spend = "journal" on the route',
                )
            )
    return findings


def caller_findings(config: Config) -> list[DoctorFinding]:
    """Warn when a mapped caller has no enabled route outside its own pool."""
    findings: list[DoctorFinding] = []
    enabled = [route for route in config.routes if route.enabled]
    for name, pool in config.callers.items():
        if pool is None:
            continue
        if enabled and all(route.pool == pool for route in enabled):
            findings.append(
                DoctorFinding(
                    "warning",
                    f"caller {name}: every enabled route is on its own pool "
                    f"{pool}, so its delegated jobs have no route",
                    "enable a route outside the caller's pool or leave the caller unmapped",
                )
            )
    return findings


def usage_findings(config: Config) -> list[DoctorFinding]:
    """Usage sources: key variables, the source's own check, and auto mode without a source."""
    findings: list[DoctorFinding] = []
    for pool in config.pools:
        if pool.usage.source is None:
            # a pool that never mentions usage stays quiet unless [usage] opted into reads
            if config.usage_mode(pool.id) == "auto" and (
                pool.usage.mode == "auto" or config.usage.present
            ):
                findings.append(
                    DoctorFinding(
                        "warning",
                        f"pool {pool.id}: usage mode is auto but no source is set",
                        "set a usage source or use manual mode",
                    )
                )
            continue
        try:
            spec = load_usage_source(pool.usage.source)
        except ConfigError:
            continue  # an unknown source is rejected when the config loads
        variable = pool.usage.api_key_env or spec.default_api_key_env
        # presence only: an empty value counts as unset, and the value is never printed or logged
        if variable is not None and not os.environ.get(variable):
            findings.append(
                DoctorFinding(
                    "warning",
                    f"pool {pool.id}: usage source {pool.usage.source} "
                    f"needs environment variable {variable}, which is not set",
                    "set the environment variable in the gateway environment",
                )
            )
        warning = spec.check(pool.usage, os.environ) if spec.check is not None else None
        if warning is not None:
            findings.append(
                DoctorFinding(
                    "warning",
                    f"pool {pool.id}: usage source {pool.usage.source}: {warning}",
                    "check the usage source setup",
                )
            )
    return findings


def sink_findings(config: Config) -> list[DoctorFinding]:
    """Webhook sinks: the environment variable that holds the delivery URL."""
    findings: list[DoctorFinding] = []
    for index, sink in enumerate(config.sinks):
        if sink.type != "webhook":
            continue
        label = f'sinks[{index}] "webhook"'
        variable = sink.options.get("url_env")
        if not isinstance(variable, str) or not variable:
            findings.append(
                DoctorFinding(
                    "warning",
                    f"{label}: url_env must be a non-empty string",
                    "name the environment variable that holds the webhook URL",
                )
            )
            continue
        # presence only: the URL is a secret, so its value is never printed or logged
        if not os.environ.get(variable):
            findings.append(
                DoctorFinding(
                    "warning",
                    f"{label}: needs environment variable {variable}, which is not set",
                    "set the environment variable in the gateway environment",
                )
            )
    return findings


def task_findings(config: Config) -> list[DoctorFinding]:
    if config.tasks.allowed_roots:
        return []
    return [
        DoctorFinding(
            "warning",
            "tasks.allowed_roots is empty, jobs not accepted",
            "add a working directory to tasks.allowed_roots",
        )
    ]


def doctor_findings(config: Config) -> list[DoctorFinding]:
    """Configuration-only doctor findings, in the same check order as the CLI."""
    return [
        *weight_findings(config),
        *spend_findings(config),
        *caller_findings(config),
        *usage_findings(config),
        *sink_findings(config),
        *task_findings(config),
    ]
