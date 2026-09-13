"""Application configuration (categories, data location).

Personal configuration lives OUTSIDE the source tree: a TOML file at
~/.financials.toml (user level) and/or ./financials.toml (project level),
loaded via clevis's cascade (project overrides user overrides defaults).
None of those values are committed — the repo carries only generic
defaults so the tool runs out-of-the-box anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from financials import config_loader


DEFAULT_DATA_DIR = "~/financials-data"

# Generic, non-personal fallback so the tool works without any config file.
# Personal category sets live in ~/.financials.toml only.
DEFAULT_CATEGORIES = [
  "Inkomsten",
  "Uitgaven",
]


@dataclass
class FinancialsConfig:
  """Runtime configuration (see ~/.financials.toml for the personal set)."""

  categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
  data_dir: str | Path = Path(DEFAULT_DATA_DIR)

  def __post_init__(self):
    if isinstance(self.data_dir, str):
      self.data_dir = Path(self.data_dir)

_config: FinancialsConfig | None = None


def get_config() -> FinancialsConfig:
  """Cached config accessor: user TOML (~/.financials.toml) + optional
  project TOML (./financials.toml) over dataclass defaults."""
  global _config
  if _config is None:
    _config = config_loader.load_config(FinancialsConfig)
  return _config


def reload_config() -> FinancialsConfig:
  """Re-read configuration (used by tests to isolate config files)."""
  global _config
  _config = config_loader.load_config(FinancialsConfig)
  return _config


def approved_categories() -> frozenset[str]:
  """The approved category set (config-driven)."""
  return frozenset(get_config().categories)


def data_dir() -> Path:
  """Root directory of the data stores (config-driven, expanduser'd)."""
  path = Path(get_config().data_dir)
  return path.expanduser()
