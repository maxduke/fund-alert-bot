"""Shared application models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Runtime wiring details for the lightweight bot process."""

    sqlite_path: Path
    timezone: str
