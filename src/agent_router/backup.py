"""Consistent SQLite backups and retention of timestamped copies."""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

BACKUP_NAME_FORMAT = "agent-router-%Y%m%d-%H%M%S.sqlite"
_BACKUP_NAME = re.compile(r"agent-router-\d{8}-\d{6}\.sqlite")


def default_backup_path(home: Path, moment: datetime) -> Path:
    name = moment.astimezone(UTC).strftime(BACKUP_NAME_FORMAT)
    return home / "backups" / name


def backup_database(source: Path, target: Path) -> int:
    source_connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        target_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
        temporary.replace(target)
        return target.stat().st_size
    finally:
        source_connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prune_backups(directory: Path, keep: int) -> list[Path]:
    backups = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and _BACKUP_NAME.fullmatch(path.name)
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    removed = backups[keep:]
    for path in removed:
        path.unlink()
    return removed
