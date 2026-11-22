"""Configuration loading (TOML, Python 3.11 stdlib)."""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_toml(path: str | Path) -> dict:
    # utf-8-sig: Windows editors / PowerShell often write a byte-order mark, which tomllib rejects
    return tomllib.loads(Path(path).read_bytes().decode("utf-8-sig"))


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict[str, Any]:
    cfg = _read_toml(DEFAULT_CONFIG_PATH)
    if path:
        cfg = _merge(cfg, _read_toml(path))
    if overrides:
        cfg = _merge(cfg, overrides)
    return cfg
