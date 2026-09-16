"""Tests for the official statusline limits reader (trust keystone)."""

import json
from pathlib import Path

import pytest

from claude_monitor.output.official import (
    OFFICIAL_TTL_SECONDS,
    account_slug,
    capture_statusline,
    default_statusline_mode_path,
    default_statusline_path,
    format_statusline,
    read_official_limits,
    read_statusline_mode,
    toggle_statusline_mode,
)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_default_path_under_claude_monitor() -> None:
    p = default_statusline_path()
    assert p.name == "latest.json"
    assert p.parent.name == "statusline"
    assert ".claude-monitor" in str(p)


def test_account_slug_sanitizes_path_separators() -> None:
    assert account_slug("/home/tono/.claude-work") == "home_tono_.claude-work"


def test_account_slug_blank_falls_back_to_default() -> None:
    assert account_slug("") == "default"


def test_default_path_no_env_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    p = default_statusline_path()
    assert p.name == "latest.json"
    assert p.parent.name == "statusline"


def test_default_path_uses_config_dir_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/tono/.claude-work")
    p = default_statusline_path()
    assert p.name == "home_tono_.claude-work.json"


def test_default_path_explicit_config_dir_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/tono/.claude-work")
    p = default_statusline_path(config_dir="/home/tono/.claude-personal")
    assert p.name == "home_tono_.claude-personal.json"


def test_default_path_comma_separated_uses_first_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/a/b, /c/d")
    p = default_statusline_path()
    assert p.name == default_statusline_path(config_dir="/a/b").name


def test_read_official_limits_uses_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When no explicit path is given, config_dir picks the right capture file."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    account_dir = str(tmp_path / "acct")
    f = default_statusline_path(config_dir=account_dir)
    _write(
        f,
        {
            "captured_at_epoch": 1000,
            "rate_limits": {"five_hour": {"used_percentage": 5.0, "resets_at": 5000}},
        },
    )
    # A different config_dir must not see this account's capture.
    assert (
        read_official_limits(now_epoch=1100, config_dir=str(tmp_path / "other")) is None
    )


def test_capture_statusline_uses_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    account_dir = str(tmp_path / "acct")
    capture_statusline(
        {"rate_limits": {"five_hour": {"used_percentage": 33.0, "resets_at": 5000}}},
        now_epoch=1,
        config_dir=account_dir,
    )
    out = read_official_limits(now_epoch=2, config_dir=account_dir)
    assert out["five_hour"]["used_percentage"] == 33.0


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_official_limits(tmp_path / "nope.json", now_epoch=1000) is None


def test_reads_both_windows(tmp_path: Path) -> None:
    f = tmp_path / "latest.json"
    _write(
        f,
        {
            "captured_at_epoch": 1000,
            "rate_limits": {
                "five_hour": {"used_percentage": 42.5, "resets_at": 5000},
                "seven_day": {"used_percentage": 18.0, "resets_at": 9000},
            },
        },
    )
    out = read_official_limits(f, now_epoch=1100)

    assert out is not None
    assert out["five_hour"] == {"used_percentage": 42.5, "resets_at_epoch": 5000}
    assert out["seven_day"] == {"used_percentage": 18.0, "resets_at_epoch": 9000}
    assert out["captured_at_epoch"] == 1000
    assert out["stale"] is False


def test_fresh_within_ttl_and_stale_beyond(tmp_path: Path) -> None:
    f = tmp_path / "latest.json"
    _write(
        f,
        {
            "captured_at_epoch": 1000,
            "rate_limits": {"five_hour": {"used_percentage": 10.0, "resets_at": 5000}},
        },
    )
    fresh = read_official_limits(f, now_epoch=1000 + OFFICIAL_TTL_SECONDS)
    stale = read_official_limits(f, now_epoch=1000 + OFFICIAL_TTL_SECONDS + 1)
    assert fresh["stale"] is False
    assert stale["stale"] is True


def test_leak_bug_52326_implausible_percentage_dropped(tmp_path: Path) -> None:
    """used_percentage can carry the resets_at epoch; an epoch-sized value is dropped."""
    f = tmp_path / "latest.json"
    _write(
        f,
        {
            "captured_at_epoch": 1000,
            "rate_limits": {
                "five_hour": {"used_percentage": 1719500000, "resets_at": 5000},
                "seven_day": {"used_percentage": 100.6, "resets_at": 9000},
            },
        },
    )
    out = read_official_limits(f, now_epoch=1100)
    # Epoch leak -> percentage unavailable, but the window/reset still reported.
    assert out["five_hour"]["used_percentage"] is None
    assert out["five_hour"]["resets_at_epoch"] == 5000
    # A small overshoot is a rounding artifact -> clamped to 100.
    assert out["seven_day"]["used_percentage"] == 100.0


def test_no_rate_limits_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "latest.json"
    _write(f, {"captured_at_epoch": 1000, "model": {"id": "x"}})
    assert read_official_limits(f, now_epoch=1100) is None


def test_window_past_reset_drops_percentage(tmp_path: Path) -> None:
    """A window whose reset time has passed has rolled over; its old % is invalid."""
    f = tmp_path / "latest.json"
    _write(
        f,
        {
            "captured_at_epoch": 1000,
            "rate_limits": {"five_hour": {"used_percentage": 99.0, "resets_at": 5000}},
        },
    )
    before = read_official_limits(f, now_epoch=4000)  # still inside the window
    after = read_official_limits(f, now_epoch=6000)  # past the reset
    assert before["five_hour"]["used_percentage"] == 99.0
    assert after["five_hour"]["used_percentage"] is None
    assert after["five_hour"]["resets_at_epoch"] == 5000


def test_window_expires_exactly_at_reset(tmp_path: Path) -> None:
    """At the reset instant (now == resets_at) the window has already rolled over."""
    f = tmp_path / "latest.json"
    _write(
        f,
        {
            "captured_at_epoch": 1000,
            "rate_limits": {"five_hour": {"used_percentage": 50.0, "resets_at": 5000}},
        },
    )
    out = read_official_limits(f, now_epoch=5000)
    assert out["five_hour"]["used_percentage"] is None


def test_capture_non_dict_stdin_is_safe(tmp_path: Path) -> None:
    """A bare JSON array/scalar on stdin must not crash the hook."""
    f = tmp_path / "statusline" / "latest.json"
    assert capture_statusline([], path=f, now_epoch=1) is None  # type: ignore[arg-type]
    assert read_official_limits(f, now_epoch=2) is None


def test_only_one_window_present(tmp_path: Path) -> None:
    f = tmp_path / "latest.json"
    _write(
        f,
        {
            "captured_at_epoch": 1000,
            "rate_limits": {"five_hour": {"used_percentage": 30.0, "resets_at": 5000}},
        },
    )
    out = read_official_limits(f, now_epoch=1100)
    assert out["five_hour"]["used_percentage"] == 30.0
    assert out["seven_day"] is None


def test_corrupt_json_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "latest.json"
    f.write_text("{not json")
    assert read_official_limits(f, now_epoch=1100) is None


def test_non_finite_values_do_not_crash(tmp_path: Path) -> None:
    """JSON permits Infinity/NaN; they must not crash the reader or render as truth."""
    f = tmp_path / "latest.json"
    # Written by hand because these are the literals json.loads accepts.
    f.write_text(
        '{"captured_at_epoch": NaN, "rate_limits": {'
        '"five_hour": {"used_percentage": NaN, "resets_at": Infinity},'
        '"seven_day": {"used_percentage": 20.0, "resets_at": 9000}}}'
    )
    out = read_official_limits(f, now_epoch=1100)
    assert out is not None
    assert out["five_hour"]["used_percentage"] is None  # NaN dropped
    assert out["five_hour"]["resets_at_epoch"] is None  # Infinity dropped
    assert out["seven_day"]["used_percentage"] == 20.0  # finite window survives
    assert out["captured_at_epoch"] is None  # NaN dropped, no crash


# --- writer (the --statusline hook) ------------------------------------------


def test_capture_round_trips_through_reader(tmp_path: Path) -> None:
    """capture_statusline writes a file read_official_limits can consume."""
    f = tmp_path / "statusline" / "latest.json"
    stdin_payload = {
        "model": {"display_name": "Opus 4.8"},
        "rate_limits": {
            "five_hour": {"used_percentage": 55.0, "resets_at": 5000},
            "seven_day": {"used_percentage": 20.0, "resets_at": 9000},
        },
    }
    written = capture_statusline(stdin_payload, path=f, now_epoch=1000)
    assert written["captured_at_epoch"] == 1000

    out = read_official_limits(f, now_epoch=1050)
    assert out["five_hour"]["used_percentage"] == 55.0
    assert out["seven_day"]["used_percentage"] == 20.0


def test_capture_no_rate_limits_tombstones(tmp_path: Path) -> None:
    """No rate_limits -> write a tombstone (not nothing) so old official data clears."""
    f = tmp_path / "statusline" / "latest.json"
    assert (
        capture_statusline({"model": {"display_name": "x"}}, path=f, now_epoch=1)
        is None
    )
    assert f.exists()
    assert read_official_limits(f, now_epoch=2) is None


def test_tombstone_clears_prior_official(tmp_path: Path) -> None:
    """A plan downgrade (rate_limits disappears) must not keep serving stale official."""
    f = tmp_path / "statusline" / "latest.json"
    capture_statusline(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 99.0, "resets_at": 9999999999}
            }
        },
        path=f,
        now_epoch=1,
    )
    assert read_official_limits(f, now_epoch=2)["five_hour"]["used_percentage"] == 99.0
    capture_statusline({"model": {"display_name": "x"}}, path=f, now_epoch=3)
    assert read_official_limits(f, now_epoch=4) is None


def test_capture_is_atomic_leaves_no_tmp(tmp_path: Path) -> None:
    f = tmp_path / "statusline" / "latest.json"
    capture_statusline(
        {"rate_limits": {"five_hour": {"used_percentage": 1.0, "resets_at": 5}}},
        path=f,
        now_epoch=1,
    )
    assert f.exists()
    assert list(f.parent.glob("*.tmp")) == []


def test_format_statusline_shows_official_percentages_used_mode() -> None:
    payload = {"model": {"display_name": "Opus 4.8"}}
    capture = {
        "rate_limits": {
            "five_hour": {"used_percentage": 42.0},
            "seven_day": {"used_percentage": 18.0},
        }
    }
    line = format_statusline(payload, capture, mode="used")
    assert "Opus 4.8" in line
    assert "5h [42%]" in line
    assert "7d [18%]" in line


def test_format_statusline_shows_left_percentages_by_default() -> None:
    payload = {"model": {"display_name": "Opus 4.8"}}
    capture = {
        "rate_limits": {
            "five_hour": {"used_percentage": 42.0},
            "seven_day": {"used_percentage": 18.0},
        }
    }
    line = format_statusline(payload, capture)
    assert "Opus 4.8" in line
    assert "5h [58%↓]" in line
    assert "7d [82%↓]" in line


def test_format_statusline_fallback_when_no_limits() -> None:
    assert format_statusline({}, None) == "claude-monitor"


def test_format_statusline_ignores_leaked_percentage() -> None:
    capture = {"rate_limits": {"five_hour": {"used_percentage": 1719500000}}}
    line = format_statusline({"model": {"display_name": "Opus"}}, capture)
    assert "5h" not in line
    assert line == "Opus"


def test_format_statusline_survives_nondict_shapes() -> None:
    """Valid JSON with unexpected types must not raise (the hook can't crash)."""
    line = format_statusline({"model": "Opus"}, {"rate_limits": {"five_hour": "bad"}})
    assert line == "claude-monitor"


# --- Used/Left display toggle --------------------------------------------------


def test_read_statusline_mode_defaults_to_left_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert read_statusline_mode() == "left"


def test_read_statusline_mode_reads_persisted_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write(default_statusline_mode_path(), {"mode": "left"})
    assert read_statusline_mode() == "left"


def test_read_statusline_mode_tolerates_corrupt_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    p = default_statusline_mode_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert read_statusline_mode() == "left"


def test_read_statusline_mode_tolerates_unknown_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write(default_statusline_mode_path(), {"mode": "sideways"})
    assert read_statusline_mode() == "left"


def test_toggle_statusline_mode_flips_left_to_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert read_statusline_mode() == "left"
    assert toggle_statusline_mode() == "used"
    assert read_statusline_mode() == "used"


def test_toggle_statusline_mode_flips_used_to_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    toggle_statusline_mode()  # left -> used
    assert toggle_statusline_mode() == "left"
    assert read_statusline_mode() == "left"


def test_toggle_statusline_mode_is_atomic_leaves_no_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    toggle_statusline_mode()
    assert list(default_statusline_mode_path().parent.glob("*.tmp")) == []


def test_format_statusline_left_mode_shows_percentage_remaining() -> None:
    capture = {"rate_limits": {"five_hour": {"used_percentage": 1.0}}}
    line = format_statusline({}, capture, mode="left")
    assert "5h [99%↓]" in line


def test_format_statusline_left_mode_marks_values_distinctly() -> None:
    capture = {"rate_limits": {"five_hour": {"used_percentage": 1.0}}}
    used_line = format_statusline({}, capture, mode="used")
    left_line = format_statusline({}, capture, mode="left")
    assert used_line != left_line
    assert "↓" in left_line
    assert "↓" not in used_line


def test_format_statusline_left_mode_clamps_at_zero_when_used_at_limit() -> None:
    """_clean_pct already clamps an official overshoot (e.g. 100.6) to 100.0 --
    left-mode's own max(0, ...) must not turn that into a negative percentage."""
    capture = {"rate_limits": {"five_hour": {"used_percentage": 100.6}}}
    line = format_statusline({}, capture, mode="left")
    assert "5h [0%↓]" in line


def test_format_statusline_left_mode_is_default() -> None:
    capture = {"rate_limits": {"five_hour": {"used_percentage": 42.0}}}
    assert format_statusline({}, capture) == format_statusline({}, capture, mode="left")


# --- Time-until-reset ---------------------------------------------------------


def test_format_statusline_shows_time_until_reset() -> None:
    capture = {
        "rate_limits": {
            "five_hour": {"used_percentage": 30.0, "resets_at": 10000},
        }
    }
    # 10000 - 7000 = 3000s = 50m
    line = format_statusline({}, capture, now_epoch=7000)
    assert "↺50m" in line


def test_format_statusline_time_until_reset_hours_and_minutes() -> None:
    capture = {
        "rate_limits": {
            "five_hour": {"used_percentage": 50.0, "resets_at": 10000 + 2 * 3600 + 15 * 60},
        }
    }
    line = format_statusline({}, capture, now_epoch=10000)
    assert "↺2h15m" in line


def test_format_statusline_no_reset_when_past_epoch() -> None:
    capture = {
        "rate_limits": {
            "five_hour": {"used_percentage": 50.0, "resets_at": 5000},
        }
    }
    line = format_statusline({}, capture, now_epoch=6000)
    assert "↺" not in line


def test_format_statusline_no_reset_when_no_resets_at() -> None:
    capture = {"rate_limits": {"five_hour": {"used_percentage": 50.0}}}
    line = format_statusline({}, capture, now_epoch=1000)
    assert "↺" not in line


def test_format_statusline_time_until_reset_shows_days_for_7d_window() -> None:
    capture = {
        "rate_limits": {
            "seven_day": {
                "used_percentage": 20.0,
                "resets_at": 10000 + 4 * 86400 + 3 * 3600,
            },
        }
    }
    line = format_statusline({}, capture, now_epoch=10000)
    assert "↺4d3h" in line


def test_format_statusline_reset_shown_inline_with_window_percentage() -> None:
    """The ↺ timer for each window appears in the same segment as its percentage."""
    capture = {
        "rate_limits": {
            "five_hour": {"used_percentage": 40.0, "resets_at": 10000 + 3600},
            "seven_day": {"used_percentage": 10.0, "resets_at": 10000 + 2 * 86400},
        }
    }
    line = format_statusline({}, capture, now_epoch=10000, mode="used")
    # Each window's % and ↺ are in the same segment (no · between them).
    assert "5h [40% ↺1h00m]" in line
    assert "7d [10% ↺2d0h]" in line


def test_format_statusline_reset_from_resets_at_epoch_key() -> None:
    """Countdown works when window uses resets_at_epoch (API data) instead of resets_at."""
    capture = {
        "rate_limits": {
            "five_hour": {"used_percentage": 30.0, "resets_at_epoch": 10000 + 3600},
            "seven_day": {"used_percentage": 10.0, "resets_at_epoch": 10000 + 2 * 86400},
        }
    }
    line = format_statusline({}, capture, now_epoch=10000, mode="used")
    assert "5h [30% ↺1h00m]" in line
    assert "7d [10% ↺2d0h]" in line
