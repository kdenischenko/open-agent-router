"""Accumulated OpenCode cost: stats parsing and periodic observations."""

from __future__ import annotations

import contextlib
import json
import math
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any

from agent_router.clock import Clock
from agent_router.config import Config
from agent_router.events import EventBus
from agent_router.registry import CounterSpec, load_harness, resolve_backend
from agent_router.runner import (
    KILL_WAIT_S,
    ResolvedExecutable,
    kill_tree,
    resolve_executable,
)
from agent_router.store import CounterSample, Store


def counter_from_stats(
    stats: Any,
    provider: str,
    model: str | None = None,
    scale: Callable[[str], float] = lambda _: 1.0,
) -> float | None:
    """Cost of the provider's models; each is multiplied by its pool model weight."""
    if not isinstance(stats, Mapping) or not isinstance(stats.get("models"), list):
        return None
    total = 0.0
    for entry in stats["models"]:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("model"), Mapping):
            continue
        description = entry["model"]
        if description.get("providerID") != provider:
            continue
        if model is not None and description.get("id") != model:
            continue
        cost = entry.get("cost")
        if isinstance(cost, bool) or not isinstance(cost, int | float):
            return None
        if not math.isfinite(cost) or cost < 0:
            return None
        model_id = description.get("id")
        total += float(cost) if model_id is None else float(cost) * float(scale(str(model_id)))
    return total if math.isfinite(total) else None


def run_stats(executable: str, timeout_s: float = 30) -> Any | None:
    options: dict[str, object] = {}
    if sys.platform == "win32":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        options["start_new_session"] = True  # own process group so the tree can be killed
    try:
        proc = subprocess.Popen(
            [executable, "stats", "--json", "--cost", "--models", "--all"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **options,
        )
        try:
            stdout, _stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            try:
                proc.communicate(timeout=KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.communicate(timeout=KILL_WAIT_S)
            return None
        if proc.returncode != 0:
            return None
        return json.loads(stdout)
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, TypeError):
        return None


class CounterSampler:
    def __init__(
        self,
        config: Config,
        store: Store,
        clock: Clock,
        bus: EventBus,
        run: Callable[[str], Any | None] = run_stats,
        resolve: Callable[[str], ResolvedExecutable] = resolve_executable,
    ) -> None:
        self.config = config
        self.store = store
        self.clock = clock
        self.bus = bus
        self.run = run
        self.resolve = resolve

    def sample_once(self) -> int:
        pools = [
            pool
            for pool in self.config.pools
            if pool.counter == "opencode_stats" and pool.counter_provider is not None
        ]
        if not pools:
            return 0
        exe = resolve_backend(
            self.config.backend_section("opencode"), load_harness("opencode"), self.resolve
        )
        if exe.path is None:
            return 0
        stats = self.run(str(exe.path))
        if stats is None:
            return 0

        sampled_at = self.clock.now()
        written = 0
        for pool in pools:
            value = counter_from_stats(
                stats, pool.counter_provider, pool.counter_model, scale=pool.scale
            )
            if value is None:
                continue
            self.store.add_counter_sample(
                CounterSample(pool.id, value, sampled_at, pool.scale_hash)
            )
            self.bus.publish("counter.sampled", {"pool": pool.id})
            written += 1
        return written


COUNTER = CounterSpec("opencode_stats", CounterSampler, frozenset({"opencode"}))
