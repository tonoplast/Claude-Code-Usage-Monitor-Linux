"""Tests for the opt-in experimental Anthropic OAuth usage API reader."""

import json
from pathlib import Path
from typing import Any

import pytest

from claude_monitor.output.api_usage import (
    API_TTL_SECONDS,
    default_api_cache_path,
    read_api_limits,
    read_oauth_token,
)


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class _Opener:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: int = 10) -> _Response:
        _ = timeout
        self.requests.append(request)
        return _Response(self.payload)


def _cached_limits(captured_at_epoch: int = 1000) -> dict[str, Any]:
    return {
        "five_hour": {"used_percentage": 17.0, "resets_at_epoch": 1300},
        "seven_day": None,
        "captured_at_epoch": captured_at_epoch,
        "stale": False,
        "source": {"kind": "anthropic_oauth_usage_api"},
    }


def test_read_api_limits_disabled_does_not_call_network(tmp_path: Path) -> None:
    opener = _Opener({})

    out = read_api_limits(
        enabled=False,
        cache_path=tmp_path / "api.json",
        now_epoch=1000,
        opener=opener,
    )

    assert out is None
    assert opener.requests == []


def test_read_api_limits_uses_fresh_cache_without_network(tmp_path: Path) -> None:
    cache = tmp_path / "api.json"
    limits = _cached_limits(captured_at_epoch=1000)
    cache.write_text(json.dumps({"captured_at_epoch": 1000, "limits": limits}))
    opener = _Opener({})

    out = read_api_limits(
        enabled=True,
        cache_path=cache,
        now_epoch=1000 + API_TTL_SECONDS,
        opener=opener,
    )

    assert out == limits
    assert opener.requests == []


def test_read_api_limits_sanitizes_cached_non_finite_values(tmp_path: Path) -> None:
    cache = tmp_path / "api.json"
    cache.write_text(
        json.dumps(
            {
                "captured_at_epoch": 1000,
                "limits": {
                    "five_hour": {
                        "used_percentage": float("nan"),
                        "resets_at_epoch": 1300,
                    },
                    "seven_day": None,
                    "captured_at_epoch": 1000,
                    "stale": False,
                    "source": {"kind": "anthropic_oauth_usage_api"},
                },
            }
        )
    )

    out = read_api_limits(
        enabled=True,
        cache_path=cache,
        now_epoch=1100,
        opener=_Opener({}),
    )

    assert out is not None
    assert out["five_hour"]["used_percentage"] is None
    assert out["five_hour"]["resets_at_epoch"] == 1300


def test_read_api_limits_normalizes_response_and_writes_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "api.json"
    opener = _Opener(
        {
            "five_hour": {
                "utilization": 0.42,
                "resets_at": "2026-06-27T17:00:00Z",
            },
            "seven_day": {
                "utilization": 0.91,
                "resets_at": "2026-07-04T17:00:00Z",
            },
        }
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    monkeypatch.setenv("CLAUDE_CODE_VERSION", "2.1.80")

    out = read_api_limits(
        enabled=True,
        cache_path=cache,
        now_epoch=2000,
        opener=opener,
    )

    assert out is not None
    assert out["source"]["kind"] == "anthropic_oauth_usage_api"
    assert out["five_hour"]["used_percentage"] == 42.0
    assert out["five_hour"]["resets_at_epoch"] == 1782579600
    assert out["seven_day"]["used_percentage"] == 91.0
    assert out["stale"] is False

    request = opener.requests[0]
    headers = {key.lower(): value for key, value in request.headers.items()}
    assert request.full_url == "https://api.anthropic.com/api/oauth/usage"
    assert headers["authorization"] == "Bearer test-token"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert headers["user-agent"] == "claude-code/2.1.80"
    assert headers["content-type"] == "application/json"

    cached = json.loads(cache.read_text())
    assert cached["captured_at_epoch"] == 2000
    assert cached["limits"] == out


def test_retry_after_blocks_fetch_and_marks_old_cache_stale(tmp_path: Path) -> None:
    cache = tmp_path / "api.json"
    limits = _cached_limits(captured_at_epoch=1000)
    cache.write_text(
        json.dumps(
            {
                "captured_at_epoch": 1000,
                "retry_after_epoch": 2000,
                "limits": limits,
            }
        )
    )
    opener = _Opener({})

    out = read_api_limits(
        enabled=True,
        cache_path=cache,
        now_epoch=1500,
        ttl_seconds=180,
        opener=opener,
    )

    assert opener.requests == []
    assert out is not None
    assert out["stale"] is True
    assert out["five_hour"]["used_percentage"] is None
    assert out["five_hour"]["resets_at_epoch"] == 1300


def test_default_api_cache_path_no_env_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    p = default_api_cache_path()
    assert p.name == "latest.json"
    assert p.parent.name == "api"


def test_default_api_cache_path_uses_config_dir_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/tono/.claude-work")
    p = default_api_cache_path()
    assert p.name == "home_tono_.claude-work.json"


def test_read_oauth_token_prefers_env_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-token")
    assert read_oauth_token(config_dir=str(tmp_path)) == "env-token"


def test_read_oauth_token_reads_config_dir_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"accessToken": "work-token"})
    )
    assert read_oauth_token(config_dir=str(tmp_path)) == "work-token"


def test_read_oauth_token_explicit_path_wins_over_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    wrong_dir = tmp_path / "wrong"
    wrong_dir.mkdir()
    (wrong_dir / ".credentials.json").write_text(
        json.dumps({"accessToken": "wrong-token"})
    )
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({"accessToken": "explicit-token"}))

    token = read_oauth_token(credentials_path=explicit, config_dir=str(wrong_dir))
    assert token == "explicit-token"


def test_read_oauth_token_falls_back_to_global_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_text(
        json.dumps({"accessToken": "global-token"})
    )
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert read_oauth_token() == "global-token"


def test_read_api_limits_passes_config_dir_to_token_and_cache_path(
    tmp_path: Path,
) -> None:
    """read_api_limits(config_dir=...) must reach both the cache path and the token lookup."""
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"accessToken": "acct-token"})
    )
    opener = _Opener(
        {"five_hour": {"utilization": 0.1, "resets_at": "2026-06-27T17:00:00Z"}}
    )

    out = read_api_limits(
        enabled=True,
        now_epoch=2000,
        opener=opener,
        config_dir=str(tmp_path),
    )

    assert out is not None
    request = opener.requests[0]
    headers = {key.lower(): value for key, value in request.headers.items()}
    assert headers["authorization"] == "Bearer acct-token"
    cache_file = default_api_cache_path(config_dir=str(tmp_path))
    assert cache_file.exists()
