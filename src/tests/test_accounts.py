"""Tests for the --accounts per-account summary (accounts.py)."""

from pathlib import Path

import pytest

from claude_monitor.output.accounts import parse_accounts_spec, resolve_accounts


def test_parse_accounts_spec_basic() -> None:
    result = parse_accounts_spec("work=~/.claude-work,personal=~/.claude-personal")
    assert result == {
        "work": str(Path("~/.claude-work").expanduser()),
        "personal": str(Path("~/.claude-personal").expanduser()),
    }


def test_parse_accounts_spec_rejects_blank() -> None:
    with pytest.raises(ValueError):
        parse_accounts_spec("")


def test_parse_accounts_spec_rejects_missing_equals() -> None:
    with pytest.raises(ValueError):
        parse_accounts_spec("work")


def test_parse_accounts_spec_rejects_blank_name() -> None:
    with pytest.raises(ValueError):
        parse_accounts_spec("=~/.claude-work")


def test_parse_accounts_spec_rejects_blank_dir() -> None:
    with pytest.raises(ValueError):
        parse_accounts_spec("work=")


def test_parse_accounts_spec_rejects_duplicate_name() -> None:
    with pytest.raises(ValueError):
        parse_accounts_spec("work=~/.a,work=~/.b")


def test_resolve_accounts_cli_list_wins_over_env() -> None:
    result = resolve_accounts(
        cli_list=["work=~/.claude-work"], env_value="personal=~/.claude-personal"
    )
    assert result == {"work": str(Path("~/.claude-work").expanduser())}


def test_resolve_accounts_uses_env_when_no_cli_list() -> None:
    result = resolve_accounts(cli_list=[], env_value="personal=~/.claude-personal")
    assert result == {"personal": str(Path("~/.claude-personal").expanduser())}


def test_resolve_accounts_raises_when_neither_given() -> None:
    with pytest.raises(ValueError, match="No accounts configured"):
        resolve_accounts(cli_list=[], env_value="")


def test_resolve_accounts_reads_env_var_when_env_value_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_MONITOR_ACCOUNTS", "work=~/.claude-work")
    result = resolve_accounts(cli_list=[])
    assert result == {"work": str(Path("~/.claude-work").expanduser())}
