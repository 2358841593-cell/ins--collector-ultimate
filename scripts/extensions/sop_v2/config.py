"""sop_v2.toml 加载器（所有规则模块共享，零硬编码阈值）。"""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

from .contracts import sha256_of_file

_DEFAULT = Path(__file__).resolve().parents[2].parent / "config" / "sop_v2.toml"


@lru_cache(maxsize=8)
def load_config(path: str | None = None) -> dict:
    p = Path(path) if path else _DEFAULT
    with open(p, "rb") as f:
        return tomllib.load(f)


def config_sha256(path: str | None = None) -> str:
    p = Path(path) if path else _DEFAULT
    return sha256_of_file(str(p))
