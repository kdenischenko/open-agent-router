"""Model price table: TOML loading and per-token cost estimation."""

from __future__ import annotations

import hashlib
import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


@dataclass(frozen=True)
class PriceTable:
    version: str
    models: Mapping[str, tuple[float, float]]


def load_prices(path: Path) -> PriceTable:
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    models: dict[str, tuple[float, float]] = {}
    for model, value in data.items():
        valid = isinstance(value, list) and len(value) == 2
        if valid:
            valid = all(
                isinstance(price, int | float)
                and not isinstance(price, bool)
                and math.isfinite(price)
                and price >= 0
                for price in value
            )
        if not valid:
            raise ValueError(f"model {model!r}: expected a list of two non-negative finite numbers")
        models[model] = (float(value[0]), float(value[1]))
    version = hashlib.sha256(raw).hexdigest()[:12]
    return PriceTable(version=version, models=MappingProxyType(models))


def estimate(
    table: PriceTable,
    model: str,
    tokens_in: int | None,
    tokens_out: int | None,
) -> float | None:
    prices = table.models.get(model)
    if prices is None or (tokens_in is None and tokens_out is None):
        return None
    price_in, price_out = prices
    return ((tokens_in or 0) * price_in + (tokens_out or 0) * price_out) / 1_000_000
