"""Tests for Claude caching, budget and JSON handling using a fake client (no network)."""
from types import SimpleNamespace

import pytest

from app import claude_utils, db


class FakeClient:
    """Pretends to be anthropic.Anthropic and returns scripted answers."""

    def __init__(self, answers, stop_reason="end_turn"):
        self.answers = list(answers)
        self.calls = 0
        self.messages = self
        self.stop_reason = stop_reason

    def create(self, **kwargs):
        self.calls += 1
        text = self.answers.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
            stop_reason=self.stop_reason)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(db, "get_db_path", lambda: path)
    db.create_tables(path)
    return monkeypatch


def use_client(monkeypatch, client):
    monkeypatch.setattr(claude_utils, "get_client", lambda: client)


def test_json_call_is_cached_on_second_run(setup):
    client = FakeClient(['{"a": 1}'])
    use_client(setup, client)
    args = ("t", "claude-haiku-5-5", "sys", "text")
    assert claude_utils.call_claude_json(*args) == {"a": 1}
    assert claude_utils.call_claude_json(*args) == {"a": 1}
    assert client.calls == 1  # second call came from the cache


def test_bad_json_is_retried_once_then_succeeds(setup):
    client = FakeClient(["not json", '```json\n{"ok": true}\n```'])
    use_client(setup, client)
    assert claude_utils.call_claude_json("t", "claude-haiku-5-5", "s", "x") == {"ok": True}
    assert client.calls == 2


def test_two_bad_answers_return_none(setup):
    use_client(setup, FakeClient(["nope", "still nope"]))
    assert claude_utils.call_claude_json("t", "claude-haiku-5-5", "s", "x") is None


def test_usage_is_logged_with_cost(setup):
    use_client(setup, FakeClient(['{"a": 1}']))
    claude_utils.call_claude_json("t", "claude-haiku-5-5", "s", "x")
    # 1000 in * $0.10/M + 200 out * $0.50/M = $0.0002
    assert round(claude_utils.get_todays_spend(), 6) == 0.0002


def test_budget_blocks_calls(setup):
    client = FakeClient(['{"a": 1}'])
    use_client(setup, client)
    setup.setattr(claude_utils, "load_config",
                  lambda: {"claude": {"daily_budget_usd": 0.0, "max_tokens_default": 100}})
    assert claude_utils.call_claude_json("t", "claude-haiku-5-5", "s", "x") is None
    assert client.calls == 0


def test_refusal_returns_none(setup):
    use_client(setup, FakeClient(['{"a": 1}'], stop_reason="refusal"))
    assert claude_utils.call_claude_json("t", "claude-haiku-5-5", "s", "x") is None


def test_text_call_cached(setup):
    client = FakeClient(["# memo"])
    use_client(setup, client)
    assert claude_utils.call_claude_text("m", "claude-sonnet-5-5", "s", "x") == "# memo"
    assert claude_utils.call_claude_text("m", "claude-sonnet-5-5", "s", "x") == "# memo"
    assert client.calls == 1


def test_cost_estimates():
    assert claude_utils.estimate_cost("claude-sonnet-5-5", 1_000_000, 0) == 2.0
    assert claude_utils.estimate_cost("claude-haiku-5-5", 200_000, 0) == 0.1  # long-prompt tier
