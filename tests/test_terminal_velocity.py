"""Tests for terminal_velocity.py - CLI and configuration.

``main()`` is run for real; only ``urwid_ui.launch`` (the process boundary
where the terminal UI would take over) is replaced by a recorder, and the
assertions are made on the arguments it received.
"""

import logging
import os
import re
import subprocess
import sys

import pytest

from tv3 import terminal_velocity, urwid_ui


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated $HOME with no $EDITOR, so ~/.tvrc and ~/.tvlog land in tmp."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("EDITOR", raising=False)
    return home


@pytest.fixture(autouse=True)
def reset_logging():
    """Remove and close any handlers main() attached, so tests don't leak files."""
    root = logging.getLogger()
    tv3_logger = logging.getLogger("tv3")
    before = list(root.handlers)
    root_level, tv3_level = root.level, tv3_logger.level
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    for handler in list(tv3_logger.handlers):
        tv3_logger.removeHandler(handler)
        handler.close()
    root.setLevel(root_level)
    tv3_logger.setLevel(tv3_level)


@pytest.fixture
def launched(monkeypatch):
    """Replace urwid_ui.launch with a recorder; returns the list of kwargs received."""
    calls = []

    def fake_launch(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(urwid_ui, "launch", fake_launch)
    return calls


def run_main(monkeypatch, *args):
    """Run main() with the given command-line arguments; return its exit code."""
    monkeypatch.setattr(sys, "argv", ["tv3", *args])
    try:
        terminal_velocity.main()
    except SystemExit as e:
        return e.code
    return 0


def write_config(path, body):
    path.write_text("[DEFAULT]\n" + body)
    return path


class TestEditorPrecedence:
    """Editor is chosen by: command line > config file > $EDITOR > pico."""

    def test_default_editor(self, home, launched, monkeypatch):
        assert run_main(monkeypatch) == 0
        assert launched[0]["editor"] == "pico"

    def test_editor_environment_variable(self, home, launched, monkeypatch):
        monkeypatch.setenv("EDITOR", "emacs")
        run_main(monkeypatch)
        assert launched[0]["editor"] == "emacs"

    def test_config_file_overrides_environment(self, home, launched, monkeypatch):
        write_config(home / ".tvrc", "editor = nano\n")
        monkeypatch.setenv("EDITOR", "emacs")
        run_main(monkeypatch)
        assert launched[0]["editor"] == "nano"

    def test_command_line_overrides_config_and_environment(self, home, launched, monkeypatch):
        write_config(home / ".tvrc", "editor = nano\n")
        monkeypatch.setenv("EDITOR", "emacs")
        run_main(monkeypatch, "--editor", "vim")
        assert launched[0]["editor"] == "vim"


class TestConfigValues:
    """Values from the config file reach the UI with the right types."""

    def test_defaults_without_config(self, home, launched, monkeypatch):
        assert run_main(monkeypatch) == 0
        assert launched == [
            {
                "notes_dir": str(home / "Notes"),
                "editor": "pico",
                "extension": ".txt",
                "extensions": [".txt", ".md", ".markdown", ".rst"],
                "exclude": ["src", "backup", "ignore", "tmp", "old"],
            }
        ]

    def test_config_values_are_used(self, home, launched, monkeypatch, tmp_path):
        config = write_config(
            tmp_path / "tvrc",
            "editor = vim\n"
            "extension = .md\n"
            "extensions = .md,.org\n"
            "exclude = archive , drafts\n"
            "notes_dir = /srv/notes\n",
        )
        run_main(monkeypatch, "-c", str(config))
        assert launched == [
            {
                "notes_dir": "/srv/notes",
                "editor": "vim",
                "extension": ".md",
                "extensions": [".md", ".org"],
                "exclude": ["archive", "drafts"],
            }
        ]

    def test_command_line_overrides_config(self, home, launched, monkeypatch, tmp_path):
        config = write_config(
            home / ".tvrc",
            "extension = .md\nextensions = .md\nexclude = archive\nnotes_dir = /srv/notes\n",
        )
        notes = tmp_path / "other-notes"
        run_main(
            monkeypatch,
            "-x",
            ".rst",
            "--extensions",
            ".rst, .txt",
            "--exclude",
            "old",
            str(notes),
        )
        assert config.exists()
        assert launched[0]["extension"] == ".rst"
        assert launched[0]["extensions"] == [".rst", ".txt"]
        assert launched[0]["exclude"] == ["old"]
        assert launched[0]["notes_dir"] == str(notes)

    def test_empty_extension_entries_are_dropped(self, home, launched, monkeypatch):
        write_config(home / ".tvrc", "extensions = .md, .txt,\n")
        run_main(monkeypatch)
        assert launched[0]["extensions"] == [".md", ".txt"]
        run_main(monkeypatch, "--extensions", ",.rst,,")
        assert launched[1]["extensions"] == [".rst"]

    def test_percent_sign_in_value_is_literal(self, home, launched, monkeypatch):
        write_config(home / ".tvrc", "editor = vim -c 'set tw=80%'\nextension = .md\n")
        run_main(monkeypatch)
        assert launched[0]["editor"] == "vim -c 'set tw=80%'"
        assert launched[0]["extension"] == ".md"

    @pytest.mark.parametrize("value", ["False", "false", "no", "off", "0"])
    def test_debug_false_in_config_disables_debug_logging(self, home, launched, monkeypatch, value):
        write_config(home / ".tvrc", f"debug = {value}\n")
        run_main(monkeypatch)
        assert launched
        assert "Starting Terminal Velocity" not in (home / ".tvlog").read_text()

    @pytest.mark.parametrize("value", ["True", "yes", "on", "1"])
    def test_debug_true_in_config_enables_debug_logging(self, home, launched, monkeypatch, value):
        write_config(home / ".tvrc", f"debug = {value}\n")
        run_main(monkeypatch)
        assert "Starting Terminal Velocity" in (home / ".tvlog").read_text()

    def test_debug_flag_overrides_config(self, home, launched, monkeypatch):
        write_config(home / ".tvrc", "debug = false\n")
        run_main(monkeypatch, "--debug")
        assert "Starting Terminal Velocity" in (home / ".tvlog").read_text()

    def test_invalid_boolean_is_an_error(self, home, launched, monkeypatch, capsys):
        config = write_config(home / ".tvrc", "debug = maybe\n")
        assert run_main(monkeypatch) == 2
        err = capsys.readouterr().err
        assert "debug" in err
        assert "maybe" in err
        assert str(config) in err
        assert launched == []

    def test_unparseable_config_is_an_error(self, home, launched, monkeypatch, capsys):
        config = home / ".tvrc"
        config.write_text("editor = vim\n")
        assert run_main(monkeypatch) == 2
        assert str(config) in capsys.readouterr().err
        assert launched == []


class TestConfigFileLocation:
    def test_missing_explicit_config_is_an_error(self, home, launched, monkeypatch, capsys):
        missing = home / "nope.ini"
        assert run_main(monkeypatch, "-c", str(missing)) == 2
        err = capsys.readouterr().err
        assert str(missing) in err
        assert launched == []

    def test_missing_default_config_uses_defaults(self, home, launched, monkeypatch):
        assert not (home / ".tvrc").exists()
        assert run_main(monkeypatch) == 0
        assert launched[0]["editor"] == "pico"

    def test_config_path_tilde_is_expanded(self, home, launched, monkeypatch):
        write_config(home / "my_tvrc", "editor = nano\n")
        run_main(monkeypatch, "-c", "~/my_tvrc")
        assert launched[0]["editor"] == "nano"

    def test_config_path_environment_variable_is_expanded(self, home, launched, monkeypatch):
        write_config(home / "my_tvrc", "editor = nano\n")
        run_main(monkeypatch, "--config", "$HOME/my_tvrc")
        assert launched[0]["editor"] == "nano"


class TestPathExpansion:
    def test_notes_dir_and_log_file_expand_tilde_in_config(self, home, launched, monkeypatch):
        write_config(home / ".tvrc", "notes_dir = ~/Documents/Notes\nlog_file = ~/logs/tv.log\n")
        (home / "logs").mkdir()
        run_main(monkeypatch)
        assert launched[0]["notes_dir"] == str(home / "Documents" / "Notes")
        assert (home / "logs" / "tv.log").exists()

    def test_notes_dir_and_log_file_expand_environment_variables(self, home, launched, monkeypatch):
        write_config(home / ".tvrc", "notes_dir = $HOME/Notes2\nlog_file = ${HOME}/tv2.log\n")
        run_main(monkeypatch)
        assert launched[0]["notes_dir"] == str(home / "Notes2")
        assert (home / "tv2.log").exists()

    def test_command_line_notes_dir_expands_tilde(self, home, launched, monkeypatch):
        run_main(monkeypatch, "~/Elsewhere")
        assert launched[0]["notes_dir"] == str(home / "Elsewhere")


class TestLogging:
    def test_log_rotates(self, home, monkeypatch):
        monkeypatch.setattr(terminal_velocity, "LOG_MAX_BYTES", 500)

        def noisy_launch(**kwargs):
            for i in range(50):
                logging.getLogger("tv3").warning("message number %d", i)

        monkeypatch.setattr(urwid_ui, "launch", noisy_launch)
        run_main(monkeypatch)
        assert (home / ".tvlog").exists()
        assert (home / ".tvlog.1").exists()
        assert os.path.getsize(home / ".tvlog") <= 500

    def test_logging_while_ui_runs_goes_only_to_log_file(self, home, monkeypatch, capsys):
        def logging_launch(**kwargs):
            logging.getLogger("tv3").critical("tv3 critical message")
            logging.getLogger("watchdog.observers").warning("library warning message")

        monkeypatch.setattr(urwid_ui, "launch", logging_launch)
        assert run_main(monkeypatch) == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        log = (home / ".tvlog").read_text()
        assert "tv3 critical message" in log
        assert "library warning message" in log

    def test_log_records_have_time_level_and_logger_name(self, home, monkeypatch):
        def logging_launch(**kwargs):
            logging.getLogger("watchdog.observers").warning("library warning message")

        monkeypatch.setattr(urwid_ui, "launch", logging_launch)
        run_main(monkeypatch)
        line = (home / ".tvlog").read_text().splitlines()[-1]
        assert re.fullmatch(
            r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} WARNING watchdog\.observers: "
            r"library warning message",
            line,
        )

    def test_fatal_error_is_reported_on_stderr_after_ui_exits(self, home, monkeypatch, capsys):
        def failing_launch(**kwargs):
            raise RuntimeError("notes dir exploded")

        monkeypatch.setattr(urwid_ui, "launch", failing_launch)
        assert run_main(monkeypatch) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "notes dir exploded" in captured.err
        assert str(home / ".tvlog") in captured.err
        log = (home / ".tvlog").read_text()
        assert "Traceback" in log
        assert "notes dir exploded" in log

    def test_keyboard_interrupt_exits_cleanly(self, home, monkeypatch, capsys):
        def interrupted_launch(**kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(urwid_ui, "launch", interrupted_launch)
        assert run_main(monkeypatch) == 0
        assert capsys.readouterr().err == ""

    def test_print_config_does_not_create_log_file(self, home, launched, monkeypatch):
        assert run_main(monkeypatch, "-p") == 0
        assert not (home / ".tvlog").exists()
        assert launched == []

    def test_help_does_not_create_log_file(self, home, launched, monkeypatch):
        assert run_main(monkeypatch, "--help") == 0
        assert not (home / ".tvlog").exists()
        assert launched == []


class TestPrintConfigAndHelp:
    def test_print_config_shows_resolved_settings(self, home, launched, monkeypatch, capsys):
        write_config(home / ".tvrc", "editor = vim\nnotes_dir = ~/N\n")
        assert run_main(monkeypatch, "-p") == 0
        out = capsys.readouterr().out
        assert "editor='vim'" in out
        assert "extension='.txt'" in out
        assert f"notes_dir='{home / 'N'}'" in out
        assert "debug=False" in out

    def test_help_text(self, home, monkeypatch, capsys):
        assert run_main(monkeypatch, "--help") == 0
        out = capsys.readouterr().out
        assert out.startswith("usage: tv3 ")
        assert "(default: .txt)" in out
        assert "missing from the)" not in out
        assert "command line > config file > $EDITOR > pico" in " ".join(out.split())

    def test_module_entry_point_help(self, home):
        env = {**os.environ, "HOME": str(home)}
        result = subprocess.run(
            [sys.executable, "-m", "tv3", "--help"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert result.stdout.startswith("usage: tv3 ")
        assert not (home / ".tvlog").exists()
