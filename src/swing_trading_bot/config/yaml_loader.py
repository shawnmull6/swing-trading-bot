"""Safe YAML loading for non-secret configuration files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class ConfigFileError(ValueError):
    pass


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Load a YAML file whose top level must be a mapping.

    ``yaml.safe_load`` is used so no arbitrary objects can be constructed. Decimal
    values must be quoted in YAML ("0.20"): unquoted decimals become floats and are
    rejected by the models.
    """
    if not path.is_file():
        raise ConfigFileError(f"config file not found: {path}")
    try:
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigFileError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigFileError(f"{path}: top level must be a mapping")
    return {str(k): v for k, v in loaded.items()}
