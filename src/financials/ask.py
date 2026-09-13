"""Shared interactive prompting helpers (category selection).

`ask_category` prefers questionary (arrow-key select + type-to-filter)
when the dependency is installed; without it, it falls back to a plain
rich prompt with silent validation — no category dump in the prompt.
"""

from __future__ import annotations

from financials.model import APPROVED_CATEGORIES

from rich.console import Console
from rich.prompt import Prompt

console = Console()

RETRY_LIMIT = 3
ABORT = "\x03"  # sentinel: user aborted (q / Esc / Ctrl-C)


def _sorted_categories() -> list[str]:
  """Config-driven, call-time category list (config can change at runtime)."""
  return sorted(APPROVED_CATEGORIES)


def _ask_questionary(current: str, allow_blank: bool) -> str | None:
  """Arrow-key selection with type-to-filter. Returns ABORT on cancel."""
  import questionary

  sorted_categories = _sorted_categories()
  choices = [questionary.Choice(title=c, value=c) for c in sorted_categories]
  if allow_blank:
    choices.insert(0, questionary.Choice(title="(leeg — geen categorie)", value=""))
  answer = questionary.select(
    "Categorie",
    choices=choices,
    default=current if current in APPROVED_CATEGORIES else sorted_categories[0],
    use_search_filter=True,
    # j/k vim-navigation conflicts with type-to-prefix-filter (questionary
    # raises ValueError) — arrows remain the navigation, typing filters.
    use_jk_keys=False,
  ).ask()
  # .ask() returns None on Esc/Ctrl-C (KeyboardInterrupt handled inside).
  return ABORT if answer is None else answer


def _ask_rich(current: str, allow_blank: bool) -> str | None:
  """Fallback: plain prompt, silent validation (no category listing)."""
  for _ in range(RETRY_LIMIT):
    text = Prompt.ask("Categorie", default=current).strip()
    if text == "q":
      return ABORT
    if text == "":
      if allow_blank:
        return ""
      console.print("[red]Categorie mag niet leeg zijn.[/red]")
      continue
    if text in APPROVED_CATEGORIES:
      return text
    console.print(f"[red]Categorie {text!r} is niet goedgekeurd.[/red]")
  return None


def ask_category(current: str = "", allow_blank: bool = False) -> str | None:
  """Prompt one approved category, pre-selected on `current`. Returns the
  chosen category, "" (only when allow_blank), or None on abort/limits."""
  try:
    import questionary  # noqa: F401
  except ImportError:
    return _ask_rich(current, allow_blank)
  return _ask_questionary(current, allow_blank)