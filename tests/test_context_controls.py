from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from projectmem.cli import app
from projectmem.commands import context as context_command
from projectmem.commands.context import (
    ContextTokenizerUnavailable,
    count_context_tokens,
    generate_context,
)
from projectmem.models import Event
from projectmem.storage import append_event, initialize

runner = CliRunner()


def _configure_budget(root, budget: int) -> None:
    config = root / ".projectmem" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"context_token_budget = {budget}\n",
        encoding="utf-8",
    )


def test_context_uses_project_budget_and_respects_estimated_cap(tmp_path) -> None:
    initialize(tmp_path)
    _configure_budget(tmp_path, 100)
    events = [
        Event(
            type="attempt",
            summary="x" * 1_000,
            outcome="failed",
            location="src/example.py",
        )
    ]

    result = generate_context(events, root=tmp_path)

    assert result["token_budget"] == 100
    assert result["tokens_used"] <= 100
    assert len(result["markdown"]) <= 400


def test_context_omitted_cli_budget_uses_config_but_explicit_value_wins(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    initialize(tmp_path)
    _configure_budget(tmp_path, 1400)
    append_event(Event(type="note", summary="CLI context fixture"), tmp_path)

    configured = runner.invoke(app, ["context", "--format", "json"])
    explicit = runner.invoke(
        app, ["context", "--tokens", "300", "--format", "json"]
    )

    assert configured.exit_code == 0
    assert explicit.exit_code == 0
    assert json.loads(configured.output)["token_budget"] == 1400
    assert json.loads(explicit.output)["token_budget"] == 300


def test_context_does_not_reinject_superseded_decision(tmp_path) -> None:
    initialize(tmp_path)
    retired = Event(type="decision", summary="retired decision must stay out")
    current = Event(
        type="decision",
        summary="current decision remains available",
        supersedes=retired.id,
    )

    result = generate_context(
        [retired, current], token_budget=1000, root=tmp_path, use_config=False
    )

    assert "current decision remains available" in result["markdown"]
    assert "retired decision must stay out" not in result["markdown"]


def test_context_uses_exact_encoding_for_mixed_language_and_emoji(tmp_path) -> None:
    initialize(tmp_path)
    events = [
        Event(
            type="attempt",
            summary="中文 English 🚀 😀 — mixed tokenizer input " * 80,
            outcome="failed",
            location="src/example.py",
        )
    ]

    result = generate_context(
        events, token_budget=100, root=tmp_path, use_config=False
    )

    assert result["tokenizer"] == "tiktoken/cl100k_base"
    assert result["token_count_method"] == "exact"
    assert result["tokens_used"] == count_context_tokens(result["markdown"])
    assert result["tokens_used"] <= result["token_budget"]


def test_explicit_generate_context_budget_overrides_project_config(tmp_path) -> None:
    initialize(tmp_path)
    _configure_budget(tmp_path, 100)

    result = generate_context(
        [Event(type="note", summary="explicit budget")],
        token_budget=2000,
        root=tmp_path,
    )

    assert result["token_budget"] == 2000


def test_missing_context_tokenizer_is_an_observable_error(
    tmp_path, monkeypatch
) -> None:
    initialize(tmp_path)
    monkeypatch.setattr(context_command, "tiktoken", None)
    context_command._get_context_encoding.cache_clear()

    try:
        with pytest.raises(
            ContextTokenizerUnavailable, match="tokenization unavailable"
        ):
            generate_context(
                [Event(type="note", summary="must not estimate")],
                token_budget=100,
                root=tmp_path,
                use_config=False,
            )
    finally:
        context_command._get_context_encoding.cache_clear()


def test_precheck_reports_corrupt_memory_instead_of_passing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    initialize(tmp_path)
    (tmp_path / ".projectmem" / "events.jsonl").write_text("{bad json}\n")

    result = runner.invoke(app, ["precheck", "README.md"])

    assert result.exit_code == 1
    assert "memory unavailable or corrupt" in result.output
