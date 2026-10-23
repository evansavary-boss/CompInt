"""Load YAML configuration files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"


def load_yaml(path: str | Path) -> Any:
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_settings() -> dict[str, Any]:
    return load_yaml(SETTINGS_PATH)


def resolve(path: str | Path) -> Path:
    """Resolve a path from settings relative to the project root."""
    path = Path(path)
    return path if path.is_absolute() else ROOT / path
