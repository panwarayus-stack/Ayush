"""Load optional local configuration without requiring environment variables."""

import json
import os
from pathlib import Path


def load_settings(path: Path) -> dict[str, str]:
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise SystemExit(f"Cannot read {path.name}; expected a valid JSON file") from None
    if not isinstance(values, dict) or any(not isinstance(value, str) for value in values.values()):
        raise SystemExit(f"{path.name} must be a JSON object with string values")
    return values


_local = load_settings(Path(__file__).with_name("credentials.json"))


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, _local.get(name, default))
