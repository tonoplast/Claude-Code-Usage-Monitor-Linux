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
from typing import Dict, List, Optional, Tuple

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
