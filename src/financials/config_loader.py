"""Config-file loader (separate module so tests can patch it in isolation).

Thin wrapper around clevis.get_config with the CLI layer disabled —
financials config comes from files and defaults only.
"""

from __future__ import annotations

from typing import TypeVar

from financials import config as _config_module

T = TypeVar("T")


def load_config(clz: type[T]) -> T:
  """Load `clz` from the clevis cascade (user + project TOML, no CLI)."""
  from clevis import get_config

  return get_config(
    clz,
    name="financials",
    user=True,
    project=True,
    cli=False,
    security=None,  # strict defaults; the file holds no secrets
  )