"""Data model and load/save helpers for financials.

Since the journaled-ledger migration (roadmap step 6) the committed rows
live ONLY in the event journal + ledger checkpoint; the expected
register keeps its JSON file (hypotheticals, no balances). The
Transaction dataclass is the projection row shape shared by the ledger
projection and the overlay composition.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from financials import config


def _store_path(filename: str) -> Path:
  """Config-driven store path (data_dir from ~/.financials.toml)."""
  return config.data_dir() / filename


def expected_file() -> Path:
  return _store_path("expected.json")


def recurrences_file() -> Path:
  return _store_path("recurrences.json")


STATUS_ACTUAL = "actual"
STATUS_EXPECTED = "expected"

FLAG_MISSING_DATE = "missing-date"
FLAG_MISSING_CATEGORY = "missing-category"
FLAG_BAD_AMOUNT = "unparsable-amount"
FLAG_BAD_DATE = "unparsable-date"
FLAG_BALANCE_CHECKING = "balance-mismatch-checking"
FLAG_BALANCE_SAVINGS = "balance-mismatch-savings"

SUMMARY_NOTE_MARKER = "summary row"

# Default category set: generic, non-personal labels only. The personal
# category set lives in ~/.financials.toml (see financials.config).
APPROVED_CATEGORIES = frozenset(config.approved_categories())


@dataclass
class Transaction:
  """One cashflow row. The raw parsed fields are TSV-import history (the
  importer is retired with the journaled migration); journaled and
  expected rows carry the canonical fields, so the raw ones default to
  empty and the store row shape stays flexible."""

  id: str
  date: str  # ISO YYYY-MM-DD; "" when the source row had no date (flagged)
  description: str
  category_raw: str = ""  # exactly as found in the TSV, never modified
  category: str = ""  # canonical; equals category_raw right after import
  subcategory_raw: str = ""  # the part after ':' in the raw category, "" if none
  subcategory: str = ""  # canonical; equals subcategory_raw right after import
  amount_eur: float | None = None
  status: str = STATUS_ACTUAL
  linked_id: str = ""  # empty on import; used later to link expected -> actual
  balance_checking: float | None = None
  balance_savings: float | None = None
  flags: list[str] = field(default_factory=list)
  source_line: int = 0  # 1-based line number in the historical cashflow.tsv
  note: str = ""


def load_expected(path: Path | None = None) -> list[Transaction]:
  """Load manually entered one-off future entries; empty when absent."""
  path = path or expected_file()
  if not path.exists():
    return []
  with open(path, encoding="utf-8") as fh:
    rows = json.load(fh)
  return [Transaction(**row) for row in rows]


def save_expected(expected: list[Transaction], path: Path | None = None) -> None:
  """Write one-off future entries to JSON, sorted by date."""
  path = path or expected_file()
  path.parent.mkdir(parents=True, exist_ok=True)
  rows = sorted(
    (asdict(t) for t in expected),
    key=lambda t: t["date"],
  )
  with open(path, "w", encoding="utf-8") as fh:
    json.dump(rows, fh, ensure_ascii=False, indent=2)
    fh.write("\n")


def find_entry(
  entry_id: str, transactions: list[Transaction], expected: list[Transaction]
) -> Transaction | None:
  """Resolve an id within the provided stores — prefix-scoped: t####
  searches the actual register only, e#### the expected register only
  (the documented id contract; regression: an e-id colliding with an
  actual row used to resolve to the actual row while remove_row routed
  by prefix, deleting the other store's row). Committed rows resolve in
  the LEDGER (ledger.find); this helper is for the expected register and
  callers holding explicit lists. None when absent."""
  if entry_id.startswith("e") and entry_id[1:].isdigit():
    for t in expected:
      if t.id == entry_id:
        return t
    return None
  for t in transactions:
    if t.id == entry_id:
      return t
  return None
