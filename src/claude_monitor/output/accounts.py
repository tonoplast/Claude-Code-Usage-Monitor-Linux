"""Per-account usage summary — the ``--accounts`` view.

Builds one independent snapshot per named account (a Claude Code
``CLAUDE_CONFIG_DIR``) by reusing ``analyze_usage``/``build_snapshot`` exactly
as the single-account views do, so the same official > experimental-api >
local-estimate precedence and confidence labeling applies per account. Rows
are never merged or summed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.table import Table

from claude_monitor.core.plans import get_token_limit
from claude_monitor.data.analysis import analyze_usage
from claude_monitor.output.api_usage import read_api_limits
from claude_monitor.output.formatters import format_compact
from claude_monitor.output.official import read_official_limits
from claude_monitor.output.snapshots import build_snapshot

_ACCOUNTS_ENV_VAR = "CLAUDE_MONITOR_ACCOUNTS"


def _parse_entry(entry: str) -> Tuple[str, str]:
    entry = entry.strip()
    if "=" not in entry:
        raise ValueError(f"Invalid account entry {entry!r}: expected name=dir")
    name, _, raw_dir = entry.partition("=")
    name = name.strip()
    raw_dir = raw_dir.strip()
    if not name or not raw_dir:
        raise ValueError(
            f"Invalid account entry {entry!r}: name and dir must not be blank"
        )
    return name, str(Path(raw_dir).expanduser())


def _accounts_from_entries(entries: List[str]) -> Dict[str, str]:
    accounts: Dict[str, str] = {}
    for raw_entry in entries:
        if not raw_entry.strip():
            continue
        name, resolved_dir = _parse_entry(raw_entry)
        if name in accounts:
            raise ValueError(f"Duplicate account name: {name!r}")
        accounts[name] = resolved_dir
    if not accounts:
        raise ValueError("accounts spec must not be blank")
    return accounts


def parse_accounts_spec(spec: str) -> Dict[str, str]:
    """Parse ``name=dir,name=dir`` into ``{name: dir}``, expanding ``~``."""
    if not spec or not spec.strip():
        raise ValueError("accounts spec must not be blank")
    return _accounts_from_entries(spec.split(","))


def resolve_accounts(
    cli_list: Optional[List[str]], env_value: Optional[str] = None
) -> Dict[str, str]:
    """Resolve the configured accounts: ``cli_list`` wins, else the env var.

    ``cli_list`` is the ``--accounts-list`` values as pydantic-settings hands
    them for a ``List[str]`` CLI field (already split on commas and/or
    repeated flags). ``env_value`` defaults to
    ``os.environ.get(CLAUDE_MONITOR_ACCOUNTS)`` when not given explicitly (the
    parameter exists so callers/tests don't have to touch the real
    environment).
    """
    if cli_list:
        return _accounts_from_entries(cli_list)

    if env_value is None:
        env_value = os.environ.get(_ACCOUNTS_ENV_VAR, "")

    if not env_value or not env_value.strip():
        raise ValueError(
            "No accounts configured. Set CLAUDE_MONITOR_ACCOUNTS "
            "(e.g. work=~/.claude-work,personal=~/.claude-personal) or pass "
            "--accounts-list."
        )
    return parse_accounts_spec(env_value)


def _has_fresh_limit_percentage(limits: Optional[Dict[str, Any]]) -> bool:
    """Mirrors cli.main._has_fresh_limit_percentage (kept local to avoid a
    circular import between cli.main and this module)."""
    if not limits or limits.get("stale"):
        return False
    for key in ("five_hour", "seven_day"):
        window = limits.get(key)
        if isinstance(window, dict) and window.get("used_percentage") is not None:
            return True
    return False


def build_account_snapshot(
    name: str, config_dir: str, args: Any, now_epoch: int
) -> Dict[str, Any]:
    """Build one account's snapshot, reusing the same pipeline as --once.

    Mutates ``args.data_path``/``args.data_paths`` to point at this account
    (mirroring how ``cli.main._run_once`` sets them) so ``build_snapshot``'s
    ``source.data_paths`` reflects the right account. An explicit
    ``--api-cache-file`` is intentionally not honored here (one override
    can't apply to N accounts); each account always gets its own
    auto-derived cache file.
    """
    projects_path = str(Path(config_dir) / "projects")
    args.data_path = projects_path
    args.data_paths = [projects_path]

    data = analyze_usage(
        data_path=projects_path,
        hours_back=96 * 2,
        use_cache=False,
        filter_models=getattr(args, "filter_models", "all"),
    )
    blocks = data.get("blocks", []) or []

    if getattr(args, "plan", None) == "custom" and getattr(
        args, "custom_limit_tokens", None
    ):
        token_limit = int(args.custom_limit_tokens)
    else:
        token_limit = get_token_limit(getattr(args, "plan", "custom"), blocks)

    official = read_official_limits(now_epoch=now_epoch, config_dir=config_dir)

    api_limits = None
    if getattr(args, "api", False) and not _has_fresh_limit_percentage(official):
        api_limits = read_api_limits(
            enabled=True,
            now_epoch=now_epoch,
            ttl_seconds=getattr(args, "api_ttl_seconds", 180),
            config_dir=config_dir,
        )

    snapshot = build_snapshot(
        data, args, token_limit, official=official, api_limits=api_limits
    )
    return {"name": name, "config_dir": config_dir, "snapshot": snapshot}


def _pct_str(value: Optional[float]) -> str:
    return f"{value:.1f}%" if value is not None else "--"


def _left_str(value: Optional[float]) -> str:
    return f"{100 - value:.1f}%" if value is not None else "--"


def _resets_str(iso: Optional[str]) -> str:
    return iso[11:16] if isinstance(iso, str) and len(iso) >= 16 else "--:--"


def build_accounts_table(rows: List[Dict[str, Any]]) -> Table:
    """One row per account: nothing merged or summed across accounts."""
    table = Table(
        title="Claude Monitor — Accounts",
        title_style="bold cyan",
        show_header=True,
        header_style="bold",
        border_style="bright_blue",
        expand=True,
        show_lines=True,
    )
    table.add_column("Account", style="cyan")
    table.add_column("Plan", style="white")
    table.add_column("5h Left", style="yellow", justify="right")
    table.add_column("5h Used", style="white", justify="right")
    table.add_column("5h Resets", style="white", justify="right")
    table.add_column("Weekly Left", style="yellow", justify="right")
    table.add_column("Weekly Used", style="white", justify="right")
    table.add_column("Weekly Resets", style="white", justify="right")
    table.add_column("Confidence", style="white")

    for row in rows:
        snapshot = row["snapshot"]
        limits = snapshot.get("limits", {})
        five = limits.get("five_hour") or {}
        seven = limits.get("seven_day") or {}
        five_pct = five.get("used_percentage")
        seven_pct = seven.get("used_percentage")
        confidence = "/".join(
            filter(None, [five.get("confidence"), seven.get("confidence")])
        )
        table.add_row(
            row["name"],
            snapshot.get("plan", "--"),
            _left_str(five_pct),
            _pct_str(five_pct),
            _resets_str(five.get("resets_at")),
            _left_str(seven_pct),
            _pct_str(seven_pct),
            _resets_str(seven.get("resets_at")),
            confidence or "--",
        )
    return table


def build_accounts_payload(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Machine-readable payload for --output json/text: one row per account, unmerged."""
    return {
        "schema_version": "1.0",
        "accounts": [
            {
                "name": row["name"],
                "config_dir": row["config_dir"],
                "snapshot": row["snapshot"],
            }
            for row in rows
        ],
    }


def build_accounts_compact(rows: List[Dict[str, Any]]) -> str:
    """One glanceable line per account, prefixed with its name."""
    return "\n".join(
        f"[{row['name']}] {format_compact(row['snapshot'])}" for row in rows
    )


def worst_status_code(rows: List[Dict[str, Any]]) -> int:
    """Worst-case automation exit code across all account rows."""
    codes = [row["snapshot"]["status"]["code"] for row in rows]
    return max(codes) if codes else 30
