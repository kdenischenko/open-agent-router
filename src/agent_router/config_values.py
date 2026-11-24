"""Generic TOML value checks shared by the config parsers."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

_CMD_UNSAFE = frozenset('&|^%"\r\n')


def _check_keys(
    raw: dict[str, Any], allowed: frozenset[str], label: str, errors: list[str]
) -> None:
    for key in raw:
        if key not in allowed:
            errors.append(f"{label}.{key}: unknown key")


def _table(value: Any, label: str, errors: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label}: expected a table")
        return {}
    return value


def _tables(value: Any, label: str, errors: list[str]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        errors.append(f"{label}: expected a list of tables")
        return []
    return value


def _text(value: Any, label: str, errors: list[str], default: str = "") -> str:
    if isinstance(value, str) and value:
        return value
    errors.append(f"{label}: a non-empty string is required")
    return default


def _optional_text(value: Any, label: str, errors: list[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    errors.append(f"{label}: expected a string")
    return None


def _choice(
    value: Any, choices: tuple[str, ...], label: str, errors: list[str], default: str
) -> str:
    if value in choices:
        return value
    errors.append(f"{label}: invalid value: {value!r}; allowed: {', '.join(choices)}")
    return default


def _number(value: Any, label: str, errors: list[str], default: float) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        if math.isfinite(value):
            return float(value)
        errors.append(f"{label}: expected a finite number")  # TOML allows nan and inf
        return default
    errors.append(f"{label}: expected a number")
    return default


def _integer(value: Any, label: str, errors: list[str], default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    errors.append(f"{label}: expected an integer")
    return default


def _boolean(value: Any, label: str, errors: list[str], default: bool) -> bool:
    if isinstance(value, bool):
        return value
    errors.append(f"{label}: expected true or false")
    return default


def _strings(value: Any, label: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        errors.append(f"{label}: expected a list of strings")
        return []
    return value


def _paths(value: Any, label: str, errors: list[str]) -> tuple[Path, ...]:
    paths = []
    for index, item in enumerate(_strings(value, label, errors)):
        path = Path(item).expanduser()
        if not item or not path.is_absolute():
            errors.append(f"{label}[{index}]: must be an absolute path")
        paths.append(path)
    return tuple(paths)


def _check_cmd_text(value: str | None, label: str, errors: list[str]) -> None:
    if value is not None and any(character in value for character in _CMD_UNSAFE):
        errors.append(f"{label}: characters unsafe for .cmd are not allowed")


def _required_text(value: Any, label: str, errors: list[str]) -> str:
    if value is None:
        errors.append(f"{label}: is required")
        return ""
    return _text(value, label, errors)
