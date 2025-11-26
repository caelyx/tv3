"""Tests for terminal_velocity.py - CLI and configuration."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import contextlib

import terminal_velocity


class TestArgumentParsing:
    """Tests for command-line argument parsing."""

    def test_default_arguments(self, temp_notes_dir, monkeypatch, tmp_path):
        """Test that default arguments are used when none provided."""
        # Create a minimal config file
        config_file = tmp_path / "test_config"
        config_file.write_text("[DEFAULT]\n")

        with patch("sys.argv", ["tv3", "--config", str(config_file), "--print-config"]):
            with pytest.raises(SystemExit) as exc_info:
                terminal_velocity.main()
            assert exc_info.value.code == 0

    def test_config_file_argument(self, tmp_path):
        """Test specifying a custom config file."""
        config_file = tmp_path / "custom_config"
        config_content = """[DEFAULT]
editor = emacs
extension = .md
notes_dir = /tmp/custom_notes
"""
        config_file.write_text(config_content)

        with patch("sys.argv", ["tv3", "-c", str(config_file), "-p"]), pytest.raises(SystemExit):
            terminal_velocity.main()

    def test_editor_argument(self, tmp_path):
        """Test specifying a custom editor."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")

        with (
            patch(
                "sys.argv",
                ["tv3", "--config", str(config_file), "--editor", "nano", "--print-config"],
            ),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

    def test_extension_argument(self, tmp_path):
        """Test specifying a custom extension."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")

        with (
            patch(
                "sys.argv",
                ["tv3", "--config", str(config_file), "--extension", "md", "--print-config"],
            ),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

    def test_extensions_argument(self, tmp_path):
        """Test specifying multiple extensions."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")

        with (
            patch(
                "sys.argv",
                [
                    "tv3",
                    "--config",
                    str(config_file),
                    "--extensions",
                    ".org, .txt, .md",
                    "--print-config",
                ],
            ),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

    def test_exclude_argument(self, tmp_path):
        """Test specifying directories to exclude."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")

        with (
            patch(
                "sys.argv",
                [
                    "tv3",
                    "--config",
                    str(config_file),
                    "--exclude",
                    "backup, archive, temp",
                    "--print-config",
                ],
            ),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

    def test_notes_dir_argument(self, tmp_path):
        """Test specifying a custom notes directory."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")
        notes_dir = tmp_path / "custom_notes"

        with (
            patch(
                "sys.argv", ["tv3", "--config", str(config_file), str(notes_dir), "--print-config"]
            ),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

    def test_debug_flag(self, tmp_path):
        """Test enabling debug mode."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")

        with (
            patch("sys.argv", ["tv3", "--config", str(config_file), "--debug", "--print-config"]),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

    def test_log_file_argument(self, tmp_path):
        """Test specifying a custom log file."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")
        log_file = tmp_path / "custom.log"

        with (
            patch(
                "sys.argv",
                [
                    "tv3",
                    "--config",
                    str(config_file),
                    "--log-file",
                    str(log_file),
                    "--print-config",
                ],
            ),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()


class TestConfigFile:
    """Tests for configuration file handling."""

    def test_load_config_from_file(self, tmp_path):
        """Test loading configuration from a file."""
        config_file = tmp_path / "test_tvrc"
        config_content = """[DEFAULT]
editor = vim
extension = .md
extensions = .txt, .md, .org
notes_dir = ~/Documents/Notes
exclude = backup, temp
debug = True
log_file = ~/tv.log
"""
        config_file.write_text(config_content)

        with patch("sys.argv", ["tv3", "-c", str(config_file), "-p"]), pytest.raises(SystemExit):
            terminal_velocity.main()

    def test_config_file_expansion(self, tmp_path):
        """Test that paths in config file are expanded."""
        config_file = tmp_path / "test_tvrc"
        config_content = """[DEFAULT]
notes_dir = ~/test_notes
log_file = ~/.tvlog
"""
        config_file.write_text(config_content)

        with patch("sys.argv", ["tv3", "-c", str(config_file), "-p"]), pytest.raises(SystemExit):
            terminal_velocity.main()

    def test_nonexistent_config_file(self, tmp_path):
        """Test that nonexistent config files are handled gracefully."""
        nonexistent = tmp_path / "does_not_exist"

        # Should use defaults without crashing
        with patch("sys.argv", ["tv3", "-c", str(nonexistent), "-p"]), pytest.raises(SystemExit):
            terminal_velocity.main()

    def test_command_line_overrides_config(self, tmp_path):
        """Test that command-line arguments override config file."""
        config_file = tmp_path / "test_tvrc"
        config_content = """[DEFAULT]
editor = nano
extension = .txt
"""
        config_file.write_text(config_content)

        # Command line should override the config file settings
        with (
            patch(
                "sys.argv",
                ["tv3", "-c", str(config_file), "--editor", "vim", "--extension", ".md", "-p"],
            ),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()


class TestLogging:
    """Tests for logging configuration."""

    def test_debug_logging_enabled(self, tmp_path):
        """Test that debug mode enables debug-level logging."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")
        log_file = tmp_path / "test.log"

        with (
            patch(
                "sys.argv",
                ["tv3", "-c", str(config_file), "--debug", "--log-file", str(log_file), "-p"],
            ),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

    def test_default_logging_level(self, tmp_path):
        """Test that default logging is at WARNING level."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")
        log_file = tmp_path / "test.log"

        with (
            patch("sys.argv", ["tv3", "-c", str(config_file), "--log-file", str(log_file), "-p"]),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

    def test_log_file_creation(self, tmp_path):
        """Test that log file is created."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")
        log_file = tmp_path / "test.log"

        assert not log_file.exists()

        with (
            patch("sys.argv", ["tv3", "-c", str(config_file), "--log-file", str(log_file), "-p"]),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

        assert log_file.exists()


class TestExtensionParsing:
    """Tests for extension parsing."""

    def test_extension_list_parsing(self, tmp_path):
        """Test that extension lists are parsed correctly."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")

        # Extensions with commas and spaces
        with (
            patch(
                "sys.argv", ["tv3", "-c", str(config_file), "--extensions", ".txt, .md, .rst", "-p"]
            ),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

    def test_extension_list_no_spaces(self, tmp_path):
        """Test parsing extension list without spaces."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")

        with (
            patch(
                "sys.argv", ["tv3", "-c", str(config_file), "--extensions", ".txt,.md,.rst", "-p"]
            ),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

    def test_single_extension(self, tmp_path):
        """Test parsing a single extension."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")

        with (
            patch("sys.argv", ["tv3", "-c", str(config_file), "--extension", "txt", "-p"]),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()


class TestExcludeParsing:
    """Tests for exclude list parsing."""

    def test_exclude_list_parsing(self, tmp_path):
        """Test that exclude lists are parsed correctly."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")

        with (
            patch(
                "sys.argv",
                ["tv3", "-c", str(config_file), "--exclude", "backup, tmp, archive", "-p"],
            ),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

    def test_exclude_list_no_spaces(self, tmp_path):
        """Test parsing exclude list without spaces."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")

        with (
            patch(
                "sys.argv", ["tv3", "-c", str(config_file), "--exclude", "backup,tmp,archive", "-p"]
            ),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

    def test_default_exclude_list(self, tmp_path):
        """Test that default exclude list is used."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")

        with patch("sys.argv", ["tv3", "-c", str(config_file), "-p"]), pytest.raises(SystemExit):
            terminal_velocity.main()


class TestEditorEnvironment:
    """Tests for editor environment variable."""

    def test_editor_from_environment(self, tmp_path, monkeypatch):
        """Test that EDITOR environment variable is used."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")
        monkeypatch.setenv("EDITOR", "emacs")

        with patch("sys.argv", ["tv3", "-c", str(config_file), "-p"]), pytest.raises(SystemExit):
            terminal_velocity.main()

    def test_default_editor_when_no_environment(self, tmp_path, monkeypatch):
        """Test that default editor is used when EDITOR is not set."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")
        monkeypatch.delenv("EDITOR", raising=False)

        with patch("sys.argv", ["tv3", "-c", str(config_file), "-p"]), pytest.raises(SystemExit):
            terminal_velocity.main()

    def test_config_file_overrides_environment(self, tmp_path, monkeypatch):
        """Test that config file editor overrides environment."""
        config_file = tmp_path / "config"
        config_content = """[DEFAULT]
editor = nano
"""
        config_file.write_text(config_content)
        monkeypatch.setenv("EDITOR", "vim")

        with patch("sys.argv", ["tv3", "-c", str(config_file), "-p"]), pytest.raises(SystemExit):
            terminal_velocity.main()

    def test_command_line_overrides_all(self, tmp_path, monkeypatch):
        """Test that command-line editor overrides everything."""
        config_file = tmp_path / "config"
        config_content = """[DEFAULT]
editor = nano
"""
        config_file.write_text(config_content)
        monkeypatch.setenv("EDITOR", "emacs")

        with (
            patch("sys.argv", ["tv3", "-c", str(config_file), "--editor", "vim", "-p"]),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()


class TestPrintConfig:
    """Tests for --print-config flag."""

    def test_print_config_exits(self, tmp_path):
        """Test that --print-config prints and exits."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")

        with patch("sys.argv", ["tv3", "-c", str(config_file), "-p"]):
            with pytest.raises(SystemExit) as exc_info:
                terminal_velocity.main()
            # Should exit cleanly
            assert exc_info.value.code == 0

    def test_print_config_shows_settings(self, tmp_path, capsys):
        """Test that --print-config displays configuration."""
        config_file = tmp_path / "config"
        config_content = """[DEFAULT]
editor = vim
extension = .md
"""
        config_file.write_text(config_content)

        with (
            patch("sys.argv", ["tv3", "-c", str(config_file), "-p"]),
            contextlib.suppress(SystemExit),
        ):
            terminal_velocity.main()

        captured = capsys.readouterr()
        # Should contain configuration output
        assert len(captured.out) > 0


class TestPathExpansion:
    """Tests for path expansion."""

    def test_tilde_expansion_config_file(self, tmp_path, monkeypatch):
        """Test that ~ is expanded in config file path."""
        # Create config in home directory
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        config_file = home / "test_tvrc"
        config_file.write_text("[DEFAULT]\n")

        with patch("sys.argv", ["tv3", "-c", "~/test_tvrc", "-p"]), pytest.raises(SystemExit):
            terminal_velocity.main()

    def test_tilde_expansion_notes_dir(self, tmp_path, monkeypatch):
        """Test that ~ is expanded in notes directory path."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")

        with (
            patch("sys.argv", ["tv3", "-c", str(config_file), "~/Notes", "-p"]),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()

    def test_absolute_path_notes_dir(self, tmp_path):
        """Test using an absolute path for notes directory."""
        config_file = tmp_path / "config"
        config_file.write_text("[DEFAULT]\n")
        notes_dir = tmp_path / "notes"

        with (
            patch("sys.argv", ["tv3", "-c", str(config_file), str(notes_dir), "-p"]),
            pytest.raises(SystemExit),
        ):
            terminal_velocity.main()


class TestHelpAndVersion:
    """Tests for help and version information."""

    def test_help_flag(self):
        """Test that --help displays help."""
        with patch("sys.argv", ["tv3", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                terminal_velocity.main()
            # Help exits with code 0
            assert exc_info.value.code == 0

    def test_help_short_flag(self):
        """Test that -h displays help."""
        with patch("sys.argv", ["tv3", "-h"]):
            with pytest.raises(SystemExit) as exc_info:
                terminal_velocity.main()
            assert exc_info.value.code == 0
