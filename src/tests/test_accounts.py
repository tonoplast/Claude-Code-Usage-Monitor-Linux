"""Tests for the --accounts per-account summary (accounts.py)."""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

from claude_monitor.output import accounts as accounts_mod
from claude_monitor.output.accounts import (
    build_account_snapshot,
    build_accounts_compact,
    build_accounts_payload,
    build_accounts_table,
    parse_accounts_spec,
    resolve_accounts,
    worst_status_code,
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


def _jsonl_entry(model: str, ts: str, output_tokens: int = 50) -> Dict[str, Any]:
    return {
        "timestamp": ts,
        "message": {
            "usage": {"input_tokens": 100, "output_tokens": output_tokens},
            "model": model,
            "id": f"{model}-{ts}-id",
        },
        "model": model,
        "requestId": f"{model}-{ts}-req",
    }


def _write_account_fixture(
    projects_dir: Path, num_entries: int, output_tokens: int
) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        _jsonl_entry(
            "claude-opus-4-8",
            (now - timedelta(minutes=n + 1)).isoformat(),
            output_tokens=output_tokens,
        )
        for n in range(num_entries)
    ]
    session_dir = projects_dir / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "session.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


def test_build_account_snapshot_real_fixture_files_not_mocked(
    tmp_path: Path,
) -> None:
    """Exercise the real config_dir -> projects -> analyze_usage path with no
    mocking of analyze_usage, using real JSONL fixture files on disk."""
    work_dir = tmp_path / "work"
    personal_dir = tmp_path / "personal"
    _write_account_fixture(work_dir / "projects", num_entries=2, output_tokens=50)
    _write_account_fixture(personal_dir / "projects", num_entries=5, output_tokens=200)

    args = _args(plan="pro")
    with (
        patch.object(accounts_mod, "read_official_limits", return_value=None),
        patch.object(accounts_mod, "read_api_limits", return_value=None),
    ):
        work_row = build_account_snapshot("work", str(work_dir), args, now_epoch=1000)
        personal_row = build_account_snapshot(
            "personal", str(personal_dir), args, now_epoch=1000
        )

    for row, name, config_dir in (
        (work_row, "work", str(work_dir)),
        (personal_row, "personal", str(personal_dir)),
    ):
        assert set(row.keys()) == {"name", "config_dir", "snapshot"}
        assert row["name"] == name
        assert row["config_dir"] == config_dir
        assert row["snapshot"]["local"]["is_active"] is True

    work_tokens = work_row["snapshot"]["local"]["tokens"]["total_tokens"]
    personal_tokens = personal_row["snapshot"]["local"]["tokens"]["total_tokens"]
    assert work_tokens != personal_tokens
    assert personal_tokens > work_tokens


def _row(
    name: str, five_pct: Any, seven_pct: Any = None, code: int = 0
) -> Dict[str, Any]:
    return {
        "name": name,
        "config_dir": f"/tmp/{name}",
        "snapshot": {
            "plan": "pro",
            "limits": {
                "five_hour": {
                    "used_percentage": five_pct,
                    "resets_at": "2026-06-27T17:00:00+00:00",
                    "confidence": "local_estimate",
                },
                "seven_day": {
                    "used_percentage": seven_pct,
                    "resets_at": None,
                    "confidence": "unknown",
                },
            },
            "local": {},
            "pace": {},
            "status": {"code": code, "label": "ok"},
        },
    }


def test_build_accounts_table_has_one_row_per_account() -> None:
    rows = [_row("work", 90.0, code=11), _row("personal", 5.0, code=0)]
    table = build_accounts_table(rows)
    assert table.row_count == 2


def test_build_accounts_table_clamps_left_pct_at_zero_when_over_limit() -> None:
    rows = [_row("work", 120.0)]
    table = build_accounts_table(rows)
    assert table.row_count == 1
    five_h_left_column = table.columns[2]
    assert five_h_left_column.header == "5h Left"
    assert five_h_left_column._cells == ["0.0%"]


def test_build_accounts_payload_keeps_accounts_separate() -> None:
    rows = [_row("work", 90.0), _row("personal", 5.0)]
    payload = build_accounts_payload(rows)
    names = [a["name"] for a in payload["accounts"]]
    assert names == ["work", "personal"]
    assert (
        payload["accounts"][0]["snapshot"]["limits"]["five_hour"]["used_percentage"]
        == 90.0
    )
    assert (
        payload["accounts"][1]["snapshot"]["limits"]["five_hour"]["used_percentage"]
        == 5.0
    )


def test_build_accounts_compact_prefixes_each_line_with_account_name() -> None:
    rows = [_row("work", 90.0), _row("personal", 5.0)]
    out = build_accounts_compact(rows)
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("[work]")
    assert lines[1].startswith("[personal]")


def test_worst_status_code_takes_the_max() -> None:
    rows = [_row("work", 90.0, code=11), _row("personal", 5.0, code=0)]
    assert worst_status_code(rows) == 11


def test_worst_status_code_no_rows_is_error() -> None:
    assert worst_status_code([]) == 30
