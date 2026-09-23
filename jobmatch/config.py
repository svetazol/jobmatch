"""config.toml -> a frozen Settings. Secrets are not here; they're in .env."""
from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("config.toml")


@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    params: Mapping[str, Any]  # opaque: passed to the source verbatim


@dataclass(frozen=True, slots=True)
class Settings:
    sources: tuple[SourceConfig, ...]
    fetch_delay: float = 1.0
    max_fetch_attempts: int = 3


def load_settings(path: Path | str = DEFAULT_PATH) -> Settings:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"No config at {path}; copy the one in the repo root") from None

    sources = tuple(
        SourceConfig(name=entry["name"], params=entry.get("params", {}))
        for entry in raw.get("sources", ())
    )
    if not sources:
        raise ValueError(f"{path} enables no sources")

    return Settings(
        sources=sources,
        fetch_delay=float(raw.get("fetch_delay", 1.0)),
        max_fetch_attempts=int(raw.get("max_fetch_attempts", 3)),
    )
