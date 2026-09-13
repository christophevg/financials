"""Tests for the category prompt helper (ask.ask_category)."""

from unittest.mock import MagicMock

from financials import ask


class FakePrompt:
  """Replaces ask.Prompt; pops scripted answers, Enter = default."""

  def __init__(self, answers):
    self.answers = list(answers)

  def ask(self, _label, **kwargs):
    answer = self.answers.pop(0) if self.answers else ""
    return kwargs.get("default", "") if answer == "" else answer


def test_ask_rich_accepts_valid_current(monkeypatch):
  monkeypatch.setattr(ask, "Prompt", FakePrompt(["Eten"]))
  assert ask._ask_rich("Eten", allow_blank=False) == "Eten"


def test_ask_rich_retries_invalid_then_accepts(monkeypatch, capsys):
  monkeypatch.setattr(ask, "Prompt", FakePrompt(["eten", "Eten"]))
  assert ask._ask_rich("Eten", allow_blank=False) == "Eten"
  captured = capsys.readouterr().out
  assert "Categorieën:" not in captured  # no category dump, ever
  assert "eten" in captured  # the rejection message names the bad value


def test_ask_rich_q_aborts(monkeypatch):
  monkeypatch.setattr(ask, "Prompt", FakePrompt(["q"]))
  assert ask._ask_rich("Eten", allow_blank=False) == ask.ABORT


def test_ask_rich_retry_limit_returns_none(monkeypatch):
  monkeypatch.setattr(ask, "Prompt", FakePrompt(["onzin"] * 4))
  assert ask._ask_rich("Eten", allow_blank=False) is None


def test_ask_rich_blank_disallowed_keeps_asking(monkeypatch):
  monkeypatch.setattr(ask, "Prompt", FakePrompt(["", "Eten"]))
  assert ask._ask_rich("Eten", allow_blank=False) == "Eten"


def test_ask_category_falls_back_without_questionary(monkeypatch):
  real_import = __import__

  def fake_import(name, *args, **kwargs):
    if name == "questionary":
      raise ImportError("questionary not installed")
    return real_import(name, *args, **kwargs)

  monkeypatch.setattr("builtins.__import__", fake_import)
  monkeypatch.setattr(ask, "Prompt", FakePrompt(["Eten"]))
  assert ask.ask_category("Eten") == "Eten"


def test_ask_category_uses_questionary_when_available(monkeypatch):
  fake = MagicMock()
  fake.select.return_value.ask.return_value = "Diensten"
  monkeypatch.setitem(__import__("sys").modules, "questionary", fake)
  assert ask.ask_category("Eten") == "Diensten"
  kwargs = fake.select.call_args.kwargs
  assert kwargs["default"] == "Eten"  # current value pre-selected
  assert len(kwargs["choices"]) == len(ask._sorted_categories())


def test_ask_category_questionary_esc_returns_abort(monkeypatch):
  fake = MagicMock()
  fake.select.return_value.ask.return_value = None  # Esc / Ctrl-C
  monkeypatch.setitem(__import__("sys").modules, "questionary", fake)
  assert ask.ask_category("Eten") == ask.ABORT