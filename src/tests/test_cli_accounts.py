"""Integration tests for the --accounts per-account summary CLI (_run_accounts)."""

import argparse
import importlib
import json
from typing import Any, Dict
from unittest.mock import patch

import pytest

cli_main = importlib.import_module("claude_monitor.cli.main")


def _snapshot(pct: float, code: int) -> Dict[str, Any]:
    return {
        "schema_version": "1.0",
        "plan": "pro",
        "limits": {
            "five_hour": {
                "used_percentage": pct,
                "resets_at": "2026-06-27T17:00:00+00:00",
                "confidence": "local_estimate",
            },
            "seven_day": {
                "used_percentage": None,
                "resets_at": None,
                "confidence": "unknown",
            },
        },
        "local": {},
        "pace": {},
        "status": {"code": code, "label": "ok"},
    }


def _args(**overrides: Any) -> argparse.Namespace:
    base: Dict[str, Any] = dict(
        output="json",
        compact=False,
        once=True,
        plan="pro",
        theme="dark",
        refresh_rate=10,
        accounts_list=[],
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _fake_build_account_snapshot(rows_by_name: Dict[str, Dict[str, Any]]):
    def _build(name: str, config_dir: str, args: Any, now_epoch: int) -> Dict[str, Any]:
        return {"name": name, "config_dir": config_dir, "snapshot": rows_by_name[name]}

    return _build


def test_accounts_once_json_lists_each_account_independently(
    capsys: pytest.CaptureFixture,
) -> None:
    rows = {"work": _snapshot(90.0, 11), "personal": _snapshot(5.0, 0)}
    with (
        patch.object(
            cli_main, "resolve_accounts", return_value={"work": "/a", "personal": "/b"}
        ),
        patch.object(
            cli_main,
            "build_account_snapshot",
            side_effect=_fake_build_account_snapshot(rows),
        ),
    ):
        rc = cli_main._run_accounts(_args(output="json"))

    doc = json.loads(capsys.readouterr().out)
    names = [a["name"] for a in doc["accounts"]]
    assert names == ["work", "personal"]
    assert (
        doc["accounts"][0]["snapshot"]["limits"]["five_hour"]["used_percentage"] == 90.0
    )
    assert (
        doc["accounts"][1]["snapshot"]["limits"]["five_hour"]["used_percentage"] == 5.0
    )
    assert rc == 11  # worst-case across accounts


def test_accounts_no_configured_accounts_exits_30(
    capsys: pytest.CaptureFixture,
) -> None:
    with patch.object(
        cli_main, "resolve_accounts", side_effect=ValueError("No accounts configured.")
    ):
        rc = cli_main._run_accounts(_args())

    err = capsys.readouterr().err
    assert rc == 30
    assert "No accounts configured" in err


def test_accounts_compact_prefixes_each_line_with_account_name(
    capsys: pytest.CaptureFixture,
) -> None:
    rows = {"work": _snapshot(90.0, 11)}
    with (
        patch.object(cli_main, "resolve_accounts", return_value={"work": "/a"}),
        patch.object(
            cli_main,
            "build_account_snapshot",
            side_effect=_fake_build_account_snapshot(rows),
        ),
    ):
        rc = cli_main._run_accounts(_args(output="rich", compact=True))

    out = capsys.readouterr().out
    assert out.startswith("[work]")
    assert rc == 11


def test_accounts_csv_output_exits_30(capsys: pytest.CaptureFixture) -> None:
    with (
        patch.object(cli_main, "resolve_accounts", return_value={"work": "/a"}),
        patch.object(
            cli_main,
            "build_account_snapshot",
            side_effect=_fake_build_account_snapshot({"work": _snapshot(1.0, 0)}),
        ),
    ):
        rc = cli_main._run_accounts(_args(output="csv"))

    err = capsys.readouterr().err
    assert rc == 30
    assert "warehouse report" in err.lower()
