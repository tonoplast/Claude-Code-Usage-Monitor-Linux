"""Tests for the --accounts per-account summary (accounts.py)."""

import argparse
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

from claude_monitor.output import accounts as accounts_mod
from claude_monitor.output.accounts import (
    build_account_snapshot,
    parse_accounts_spec,
    resolve_accounts,
)


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


def _args(**overrides: Any) -> argparse.Namespace:
    base: Dict[str, Any] = dict(
        plan="pro",
        filter_models="all",
        api=False,
        api_ttl_seconds=180,
        custom_limit_tokens=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _blocks_payload(total: int = 1000) -> Dict[str, Any]:
    return {
        "blocks": [
            {
                "id": "b1",
                "isActive": True,
                "isGap": False,
                "startTime": "2026-06-27T12:00:00+00:00",
                "endTime": "2026-06-27T17:00:00+00:00",
                "tokenCounts": {
                    "inputTokens": 700,
                    "outputTokens": 300,
                    "cacheCreationInputTokens": 0,
                    "cacheReadInputTokens": 0,
                },
                "totalTokens": total,
                "costUSD": 0.5,
                "perModelStats": {},
                "sentMessagesCount": 5,
                "burnRate": {"tokensPerMinute": 10.0, "costPerHour": 0.1},
            }
        ]
    }


def test_build_account_snapshot_uses_local_estimate_by_default() -> None:
    args = _args(plan="pro")
    with (
        patch.object(accounts_mod, "analyze_usage", return_value=_blocks_payload()),
        patch.object(accounts_mod, "read_official_limits", return_value=None),
    ):
        row = build_account_snapshot("work", "/tmp/fake-work", args, now_epoch=1000)

    assert row["name"] == "work"
    assert row["config_dir"] == "/tmp/fake-work"
    assert row["snapshot"]["limits"]["five_hour"]["confidence"] == "local_estimate"


def test_build_account_snapshot_prefers_official() -> None:
    args = _args(plan="pro")
    official = {
        "five_hour": {"used_percentage": 88.0, "resets_at_epoch": 2000},
        "seven_day": {"used_percentage": 40.0, "resets_at_epoch": 9000},
        "captured_at_epoch": 500,
        "stale": False,
    }
    with (
        patch.object(accounts_mod, "analyze_usage", return_value=_blocks_payload()),
        patch.object(accounts_mod, "read_official_limits", return_value=official),
    ):
        row = build_account_snapshot("work", "/tmp/fake-work", args, now_epoch=1000)

    limits = row["snapshot"]["limits"]
    assert limits["five_hour"]["confidence"] == "official"
    assert limits["five_hour"]["used_percentage"] == 88.0
    assert limits["seven_day"]["used_percentage"] == 40.0


def test_build_account_snapshot_passes_config_dir_to_official_and_api() -> None:
    args = _args(plan="pro", api=True)
    with (
        patch.object(accounts_mod, "analyze_usage", return_value=_blocks_payload()),
        patch.object(
            accounts_mod, "read_official_limits", return_value=None
        ) as read_official,
        patch.object(accounts_mod, "read_api_limits", return_value=None) as read_api,
    ):
        build_account_snapshot("personal", "/tmp/fake-personal", args, now_epoch=1000)

    assert read_official.call_args.kwargs["config_dir"] == "/tmp/fake-personal"
    assert read_api.call_args.kwargs["config_dir"] == "/tmp/fake-personal"


def test_build_account_snapshot_skips_api_when_official_is_fresh() -> None:
    args = _args(plan="pro", api=True)
    official = {
        "five_hour": {"used_percentage": 20.0, "resets_at_epoch": 2000},
        "seven_day": None,
        "captured_at_epoch": 500,
        "stale": False,
    }
    with (
        patch.object(accounts_mod, "analyze_usage", return_value=_blocks_payload()),
        patch.object(accounts_mod, "read_official_limits", return_value=official),
        patch.object(accounts_mod, "read_api_limits") as read_api,
    ):
        build_account_snapshot("work", "/tmp/fake-work", args, now_epoch=1000)

    assert read_api.call_count == 0


def test_two_accounts_stay_independent_not_merged() -> None:
    args = _args(plan="pro")
    work_official = {
        "five_hour": {"used_percentage": 90.0, "resets_at_epoch": 2000},
        "seven_day": None,
        "captured_at_epoch": 500,
        "stale": False,
    }

    def fake_official(now_epoch: int, config_dir: str = "") -> Any:
        return work_official if config_dir == "/tmp/work" else None

    with (
        patch.object(
            accounts_mod, "analyze_usage", return_value=_blocks_payload(total=100)
        ),
        patch.object(accounts_mod, "read_official_limits", side_effect=fake_official),
    ):
        work_row = build_account_snapshot("work", "/tmp/work", args, now_epoch=1000)
        personal_row = build_account_snapshot(
            "personal", "/tmp/personal", args, now_epoch=1000
        )

    assert work_row["snapshot"]["limits"]["five_hour"]["used_percentage"] == 90.0
    assert (
        personal_row["snapshot"]["limits"]["five_hour"]["confidence"]
        == "local_estimate"
    )
