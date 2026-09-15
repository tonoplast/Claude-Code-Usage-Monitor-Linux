"""Simplified tests for CLI main module."""

import argparse
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from claude_monitor.cli.main import main


class TestMain:
    """Test cases for main function."""

    def test_version_flag(self) -> None:
        """Test --version flag returns 0 and prints version."""
        with patch("builtins.print") as mock_print:
            result = main(["--version"])
            assert result == 0
            mock_print.assert_called_once()
            assert "claude-monitor" in mock_print.call_args[0][0]

    def test_v_flag(self) -> None:
        """Test -v flag returns 0 and prints version."""
        with patch("builtins.print") as mock_print:
            result = main(["-v"])
            assert result == 0
            mock_print.assert_called_once()
            assert "claude-monitor" in mock_print.call_args[0][0]

    @patch("claude_monitor.core.settings.Settings.load_with_last_used")
    def test_keyboard_interrupt_handling(self, mock_load: Mock) -> None:
        """Test keyboard interrupt returns 0."""
        mock_load.side_effect = KeyboardInterrupt()
        with patch("builtins.print") as mock_print:
            result = main(["--plan", "pro"])
            assert result == 0
            mock_print.assert_called_once_with("\n\nMonitoring stopped by user.")

    @patch("claude_monitor.core.settings.Settings.load_with_last_used")
    def test_exception_handling(self, mock_load_settings: Mock) -> None:
        """Test exception handling returns 1."""
        mock_load_settings.side_effect = Exception("Test error")

        with patch("builtins.print"), patch("traceback.print_exc"):
            result = main(["--plan", "pro"])
            assert result == 1

    @patch("claude_monitor.core.settings.Settings.load_with_last_used")
    def test_successful_main_execution(self, mock_load_settings: Mock) -> None:
        """Test successful main execution by mocking core components."""
        mock_args = Mock()
        mock_args.theme = None
        mock_args.plan = "pro"
        mock_args.timezone = "UTC"
        mock_args.refresh_per_second = 1.0
        mock_args.refresh_rate = 10

        mock_settings = Mock()
        mock_settings.log_file = None
        mock_settings.log_level = "INFO"
        mock_settings.timezone = "UTC"
        mock_settings.once = False  # live path, not one-shot (#126)
        mock_settings.accounts = False  # single-account live path, not --accounts
        mock_settings.to_namespace.return_value = mock_args

        mock_load_settings.return_value = mock_settings

        # Get the actual module to avoid Python version compatibility issues with mock.patch
        import sys

        actual_module = sys.modules["claude_monitor.cli.main"]

        # Manually replace the function - this works across all Python versions
        original_discover = actual_module.discover_claude_data_paths
        actual_module.discover_claude_data_paths = Mock(
            return_value=[Path("/test/path")]
        )

        try:
            with (
                patch("claude_monitor.terminal.manager.setup_terminal"),
                patch("claude_monitor.terminal.themes.get_themed_console"),
                patch("claude_monitor.ui.display_controller.DisplayController"),
                patch(
                    "claude_monitor.monitoring.orchestrator.MonitoringOrchestrator"
                ) as mock_orchestrator,
                patch("signal.pause", side_effect=KeyboardInterrupt()),
                patch("time.sleep", side_effect=KeyboardInterrupt()),
                patch("sys.exit"),
            ):  # Don't actually exit
                # Configure mocks to not interfere with the KeyboardInterrupt
                mock_orchestrator.return_value.wait_for_initial_data.return_value = True
                mock_orchestrator.return_value.start.return_value = None
                mock_orchestrator.return_value.stop.return_value = None

                result = main(["--plan", "pro"])
                assert result == 0
        finally:
            # Restore the original function
            actual_module.discover_claude_data_paths = original_discover


class TestFunctions:
    """Test module functions."""

    def test_get_standard_claude_paths(self) -> None:
        """Test getting standard Claude paths."""
        from claude_monitor.cli.main import get_standard_claude_paths

        paths = get_standard_claude_paths()
        assert isinstance(paths, list)
        assert len(paths) > 0
        assert "~/.claude/projects" in paths

    def test_discover_claude_data_paths_no_paths(self) -> None:
        """Test discover with no existing paths."""
        from claude_monitor.cli.main import discover_claude_data_paths

        with patch("pathlib.Path.exists", return_value=False):
            paths = discover_claude_data_paths()
            assert paths == []

    def test_discover_claude_data_paths_with_custom(self) -> None:
        """Test discover with custom paths."""
        from claude_monitor.cli.main import discover_claude_data_paths

        custom_paths = ["/custom/path", "/other/path"]
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.is_dir", return_value=True),
        ):
            paths = discover_claude_data_paths(custom_paths)
            assert len(paths) == 2
            assert paths[0].name == "path"
            assert paths[1].name == "path"

    def test_discover_adds_wsl_paths_after_standard_paths(self, tmp_path: Path) -> None:
        """WSL discovery is additive; it must not replace normal data paths (#92)."""
        import importlib

        cli_main = importlib.import_module("claude_monitor.cli.main")
        standard = tmp_path / "standard" / "projects"
        wsl = tmp_path / "wsl" / "projects"
        standard.mkdir(parents=True)
        wsl.mkdir(parents=True)

        with (
            patch.object(cli_main, "_env_claude_paths", return_value=[]),
            patch.object(
                cli_main, "get_standard_claude_paths", return_value=[str(standard)]
            ),
            patch.object(cli_main, "_wsl_claude_paths", return_value=[str(wsl)]),
        ):
            paths = cli_main.discover_claude_data_paths()

        assert paths == [standard.resolve(), wsl.resolve()]

    def test_validate_cli_environment_requires_python_39(self) -> None:
        """Python below 3.9 is rejected with a clear message (issue #172)."""
        import importlib

        cli_main = importlib.import_module("claude_monitor.cli.main")

        with patch.object(cli_main.sys, "version_info", (3, 8, 0)):
            message = cli_main.validate_cli_environment()

        assert message is not None
        assert "3.9" in message

    def test_validate_cli_environment_accepts_supported_python(self) -> None:
        """Python 3.9 passes the version check."""
        import importlib

        cli_main = importlib.import_module("claude_monitor.cli.main")

        with patch.object(cli_main.sys, "version_info", (3, 9, 0)):
            assert cli_main.validate_cli_environment() is None

    def test_main_aborts_on_environment_error(self) -> None:
        """main() checks the environment first and aborts before doing any work (#172).

        Patches via the imported module object rather than the dotted string, because
        ``claude_monitor.cli.main`` is shadowed by the ``main`` function and the string
        form resolves to the function (not the module) on Python 3.9/3.10's mock.
        """
        import importlib

        cli_main = importlib.import_module("claude_monitor.cli.main")
        with (
            patch.object(
                cli_main,
                "validate_cli_environment",
                return_value="Python 3.9+ required, found 3.7",
            ) as mock_validate,
            patch(
                "claude_monitor.core.settings.Settings.load_with_last_used"
            ) as mock_load,
            patch.object(cli_main, "_run_monitoring") as mock_run,
            patch("sys.stderr"),
        ):
            result = cli_main.main([])

        mock_validate.assert_called_once()
        assert result == 1
        mock_load.assert_not_called()
        mock_run.assert_not_called()

    def test_no_data_diagnostic_names_searched_paths_and_hint(self) -> None:
        """No-data message lists the (expanded) searched paths and gives guidance (#110)."""
        from claude_monitor.cli.main import _no_data_diagnostic

        message = _no_data_diagnostic(
            ["~/.claude/projects", "~/.config/claude/projects"]
        )

        assert str(Path("~/.claude/projects").expanduser()) in message
        assert str(Path("~/.config/claude/projects").expanduser()) in message
        assert "Claude Code" in message

    def test_env_claude_paths_handles_comma_separated(self) -> None:
        """CLAUDE_CONFIG_DIR may list several dirs; each gets a 'projects' suffix (#116)."""
        import importlib

        cli_main = importlib.import_module("claude_monitor.cli.main")

        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": "/a, /b"}, clear=False):
            paths = cli_main._env_claude_paths()

        assert paths == [str(Path("/a") / "projects"), str(Path("/b") / "projects")]

    def test_env_claude_paths_empty_when_unset(self) -> None:
        """No CLAUDE_CONFIG_DIR -> no env-derived paths."""
        import importlib

        cli_main = importlib.import_module("claude_monitor.cli.main")

        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        with patch.dict("os.environ", env, clear=True):
            assert cli_main._env_claude_paths() == []

    def test_discover_includes_claude_config_dir(self) -> None:
        """A CLAUDE_CONFIG_DIR/projects directory is discovered (#116)."""
        import importlib

        cli_main = importlib.import_module("claude_monitor.cli.main")

        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            projects.mkdir()
            with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": tmp}, clear=False):
                paths = cli_main.discover_claude_data_paths()

            assert projects.resolve() in paths

    def test_statusline_captures_rate_limits_and_prints_line(
        self, tmp_path: Path, capsys
    ) -> None:
        """`--statusline` stashes official rate_limits and prints the status bar."""
        import importlib
        import io
        import json

        cli_main = importlib.import_module("claude_monitor.cli.main")
        official = importlib.import_module("claude_monitor.output.official")
        state = tmp_path / "statusline" / "latest.json"
        stdin = io.StringIO(
            json.dumps(
                {
                    "model": {"display_name": "Opus 4.8"},
                    "rate_limits": {
                        "five_hour": {"used_percentage": 73.0, "resets_at": 5000}
                    },
                }
            )
        )

        with (
            patch.object(official.Path, "home", return_value=tmp_path),
            patch.object(official, "default_statusline_path", return_value=state),
            patch.object(cli_main.sys, "stdin", stdin),
        ):
            rc = cli_main.main(["--statusline"])

        out = capsys.readouterr().out
        assert rc == 0
        assert "Opus 4.8" in out and "5h 73%" in out
        assert (
            json.loads(state.read_text())["rate_limits"]["five_hour"]["used_percentage"]
            == 73.0
        )

    def test_statusline_survives_garbage_stdin(self, tmp_path: Path, capsys) -> None:
        """A non-JSON stdin must not crash the hook (it runs on every refresh)."""
        import importlib
        import io

        cli_main = importlib.import_module("claude_monitor.cli.main")
        official = importlib.import_module("claude_monitor.output.official")
        state = tmp_path / "statusline" / "latest.json"
        with (
            patch.object(official.Path, "home", return_value=tmp_path),
            patch.object(official, "default_statusline_path", return_value=state),
            patch.object(cli_main.sys, "stdin", io.StringIO("not json at all")),
        ):
            rc = cli_main.main(["--statusline"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "claude-monitor"

    def test_statusline_falls_back_to_previous_capture_when_payload_is_bare(
        self, tmp_path: Path, capsys
    ) -> None:
        """A refresh with no rate_limits in the payload keeps showing the last
        known-good official numbers instead of going blank."""
        import importlib
        import io
        import json

        cli_main = importlib.import_module("claude_monitor.cli.main")
        official = importlib.import_module("claude_monitor.output.official")
        state = tmp_path / "statusline" / "latest.json"

        with (
            patch.object(official.Path, "home", return_value=tmp_path),
            patch.object(official, "default_statusline_path", return_value=state),
            patch.object(
                cli_main.sys,
                "stdin",
                io.StringIO(
                    json.dumps(
                        {
                            "model": {"display_name": "Opus 4.8"},
                            "rate_limits": {
                                "five_hour": {
                                    "used_percentage": 73.0,
                                    "resets_at": 9999999999,
                                }
                            },
                        }
                    )
                ),
            ),
        ):
            cli_main.main(["--statusline"])
        capsys.readouterr()  # discard the first refresh's own output

        # A later refresh carries no rate_limits at all (Claude Code omitted it).
        with (
            patch.object(official.Path, "home", return_value=tmp_path),
            patch.object(official, "default_statusline_path", return_value=state),
            patch.object(
                cli_main.sys,
                "stdin",
                io.StringIO(json.dumps({"model": {"display_name": "Opus 4.8"}})),
            ),
        ):
            rc = cli_main.main(["--statusline"])

        out = capsys.readouterr().out
        assert rc == 0
        assert "5h 73%" in out

    def test_statusline_toggle_flips_mode_and_next_refresh_reflects_it(
        self, tmp_path: Path, capsys
    ) -> None:
        import importlib
        import io
        import json

        cli_main = importlib.import_module("claude_monitor.cli.main")
        official = importlib.import_module("claude_monitor.output.official")

        with patch.object(official.Path, "home", return_value=tmp_path):
            rc = cli_main.main(["--statusline-toggle"])
            assert rc == 0
            assert "Left" in capsys.readouterr().out

            state = tmp_path / ".claude-monitor" / "statusline" / "latest.json"
            with (
                patch.object(official, "default_statusline_path", return_value=state),
                patch.object(
                    cli_main.sys,
                    "stdin",
                    io.StringIO(
                        json.dumps(
                            {"rate_limits": {"five_hour": {"used_percentage": 1.0}}}
                        )
                    ),
                ),
            ):
                cli_main.main(["--statusline"])

        out = capsys.readouterr().out
        assert "5h 99%" in out

    def test_statusline_refresh_flag_uses_active_api_data_when_official_absent(
        self, tmp_path: Path, capsys
    ) -> None:
        """--statusline-refresh actively refreshes via the experimental API when
        this call's payload has no official rate_limits."""
        import importlib
        import io

        cli_main = importlib.import_module("claude_monitor.cli.main")
        official = importlib.import_module("claude_monitor.output.official")
        state = tmp_path / "statusline" / "latest.json"

        with (
            patch.object(official.Path, "home", return_value=tmp_path),
            patch.object(official, "default_statusline_path", return_value=state),
            patch.object(cli_main.sys, "stdin", io.StringIO("{}")),
            patch.object(
                cli_main,
                "read_api_limits",
                return_value={
                    "five_hour": {"used_percentage": 64.0, "resets_at_epoch": None},
                    "seven_day": None,
                    "captured_at_epoch": 1,
                    "stale": False,
                },
            ) as mock_api,
        ):
            rc = cli_main.main(["--statusline", "--statusline-refresh", "60"])

        out = capsys.readouterr().out
        assert rc == 0
        assert "5h 64%" in out
        assert mock_api.call_args.kwargs["ttl_seconds"] == 60

    def test_statusline_refresh_does_not_blank_good_capture_when_api_unusable(
        self, tmp_path: Path, capsys
    ) -> None:
        """read_api_limits can return a non-None dict whose windows are both
        None (stale cache, no OAuth token) -- that must not override an
        already-good official capture with nothing."""
        import importlib
        import io
        import json

        cli_main = importlib.import_module("claude_monitor.cli.main")
        official = importlib.import_module("claude_monitor.output.official")
        state = tmp_path / "statusline" / "latest.json"

        with (
            patch.object(official.Path, "home", return_value=tmp_path),
            patch.object(official, "default_statusline_path", return_value=state),
            patch.object(
                cli_main.sys,
                "stdin",
                io.StringIO(
                    json.dumps(
                        {
                            "rate_limits": {
                                "five_hour": {
                                    "used_percentage": 73.0,
                                    "resets_at": 9999999999,
                                }
                            }
                        }
                    )
                ),
            ),
            patch.object(
                cli_main,
                "read_api_limits",
                return_value={
                    "five_hour": {"used_percentage": None, "resets_at_epoch": None},
                    "seven_day": None,
                    "captured_at_epoch": 1,
                    "stale": True,
                },
            ),
        ):
            rc = cli_main.main(["--statusline", "--statusline-refresh", "60"])

        out = capsys.readouterr().out
        assert rc == 0
        assert "5h 73%" in out

    def test_statusline_refresh_merges_api_window_alongside_good_official_window(
        self, tmp_path: Path, capsys
    ) -> None:
        """A usable API window for one range fills in what official is missing,
        without discarding official's own good window."""
        import importlib
        import io
        import json

        cli_main = importlib.import_module("claude_monitor.cli.main")
        official = importlib.import_module("claude_monitor.output.official")
        state = tmp_path / "statusline" / "latest.json"

        with (
            patch.object(official.Path, "home", return_value=tmp_path),
            patch.object(official, "default_statusline_path", return_value=state),
            patch.object(
                cli_main.sys,
                "stdin",
                io.StringIO(
                    json.dumps(
                        {
                            "rate_limits": {
                                "five_hour": {
                                    "used_percentage": 73.0,
                                    "resets_at": 9999999999,
                                }
                            }
                        }
                    )
                ),
            ),
            patch.object(
                cli_main,
                "read_api_limits",
                return_value={
                    "five_hour": None,
                    "seven_day": {
                        "used_percentage": 40.0,
                        "resets_at_epoch": 9999999999,
                    },
                    "captured_at_epoch": 1,
                    "stale": False,
                },
            ),
        ):
            rc = cli_main.main(["--statusline", "--statusline-refresh", "60"])

        out = capsys.readouterr().out
        assert rc == 0
        assert "5h 73%" in out
        assert "7d 40%" in out

    def test_statusline_refresh_survives_multiple_consecutive_bare_refreshes(
        self, tmp_path: Path, capsys
    ) -> None:
        """The one-cycle official-capture fallback alone can't survive a
        sustained gap (the shared capture file gets tombstoned each bare
        call) -- --statusline-refresh's own API cache is what makes repeated
        bare refreshes keep showing real numbers, for as many cycles as the
        cache stays usable."""
        import importlib
        import io

        cli_main = importlib.import_module("claude_monitor.cli.main")
        official = importlib.import_module("claude_monitor.output.official")
        state = tmp_path / "statusline" / "latest.json"

        api_result = {
            "five_hour": {"used_percentage": 55.0, "resets_at_epoch": None},
            "seven_day": None,
            "captured_at_epoch": 1,
            "stale": False,
        }

        for _ in range(3):
            with (
                patch.object(official.Path, "home", return_value=tmp_path),
                patch.object(official, "default_statusline_path", return_value=state),
                patch.object(cli_main.sys, "stdin", io.StringIO("{}")),
                patch.object(cli_main, "read_api_limits", return_value=api_result),
            ):
                rc = cli_main.main(["--statusline", "--statusline-refresh", "60"])
            out = capsys.readouterr().out
            assert rc == 0
            assert "5h 55%" in out

    def test_statusline_refresh_flag_equals_form(self) -> None:
        import importlib

        cli_main = importlib.import_module("claude_monitor.cli.main")
        assert (
            cli_main._extract_statusline_refresh_seconds(
                ["--statusline", "--statusline-refresh=90"]
            )
            == 90
        )

    def test_statusline_refresh_flag_malformed_value_disables_refresh(self) -> None:
        import importlib

        cli_main = importlib.import_module("claude_monitor.cli.main")
        assert (
            cli_main._extract_statusline_refresh_seconds(
                ["--statusline", "--statusline-refresh", "soon"]
            )
            is None
        )

    def test_statusline_refresh_flag_clamps_below_floor(self) -> None:
        import importlib

        cli_main = importlib.import_module("claude_monitor.cli.main")
        assert (
            cli_main._extract_statusline_refresh_seconds(
                ["--statusline", "--statusline-refresh", "5"]
            )
            == cli_main.STATUSLINE_REFRESH_MIN_SECONDS
        )

    def test_statusline_refresh_flag_absent_returns_none(self) -> None:
        import importlib

        cli_main = importlib.import_module("claude_monitor.cli.main")
        assert cli_main._extract_statusline_refresh_seconds(["--statusline"]) is None

    def test_effective_token_limit_honors_explicit_custom(self) -> None:
        """An explicit --custom-limit-tokens wins over a P90/computed base (#65)."""
        import argparse
        import importlib

        cli_main = importlib.import_module("claude_monitor.cli.main")
        args = argparse.Namespace(plan="custom", custom_limit_tokens=44000)

        # Base is whatever the orchestrator computed (e.g. a P90 estimate).
        assert cli_main._effective_token_limit(args, 19000) == 44000

    def test_effective_token_limit_falls_back_to_base(self) -> None:
        """Without an explicit custom limit, the computed base is used unchanged."""
        import argparse
        import importlib

        cli_main = importlib.import_module("claude_monitor.cli.main")

        assert (
            cli_main._effective_token_limit(
                argparse.Namespace(plan="pro", custom_limit_tokens=None), 19000
            )
            == 19000
        )
        # Custom plan but no explicit token count -> still the base.
        assert (
            cli_main._effective_token_limit(
                argparse.Namespace(plan="custom", custom_limit_tokens=None), 7000
            )
            == 7000
        )

    def test_main_routes_accounts_flag_to_run_accounts(self) -> None:
        """settings.accounts routes to _run_accounts before the once/monitoring branches."""
        import importlib
        from unittest.mock import MagicMock

        cli_main = importlib.import_module("claude_monitor.cli.main")

        fake_settings = MagicMock()
        fake_settings.view = "realtime"
        fake_settings.accounts = True
        fake_settings.once = False
        fake_settings.log_file = None
        fake_settings.log_level = "INFO"
        fake_settings.timezone = "UTC"
        fake_settings.to_namespace.return_value = argparse.Namespace(accounts=True)

        with (
            patch(
                "claude_monitor.core.settings.Settings.load_with_last_used",
                return_value=fake_settings,
            ),
            patch.object(cli_main, "setup_environment"),
            patch.object(cli_main, "ensure_directories"),
            patch.object(cli_main, "setup_logging"),
            patch.object(cli_main, "init_timezone"),
            patch.object(
                cli_main, "_run_accounts", return_value=0
            ) as mock_run_accounts,
        ):
            rc = cli_main.main(["--accounts"])

        mock_run_accounts.assert_called_once()
        assert rc == 0
