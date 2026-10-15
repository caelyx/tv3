#!/usr/bin/env python3
"""A fast note-taking app for the UNIX terminal."""

import argparse
import configparser
import logging
import logging.handlers
import os
import re
import sys

from . import urwid_ui

# Configuration constants
DEFAULT_EDITOR = "pico"
DEFAULT_EXTENSION = ".txt"
DEFAULT_EXTENSIONS = ".txt, .md, .markdown, .rst"
DEFAULT_NOTES_DIR = "~/Notes"
DEFAULT_EXCLUDE_DIRS = "src, backup, ignore, tmp, old"
DEFAULT_LOG_FILE = "~/.tvlog"
DEFAULT_CONFIG_FILE = "~/.tvrc"

# Logging constants
LOG_MAX_BYTES = 1_000_000  # 1 megabyte
LOG_BACKUP_COUNT = 1
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# The settings a config file may contain, in the order -p prints them.
CONFIG_KEYS = ("editor", "extension", "extensions", "exclude", "notes_dir", "log_file", "debug")

# Matches the variable references os.path.expandvars expands when defined.
UNEXPANDED_VARIABLE = re.compile(r"\$(\w+|\{[^}]*\})", re.ASCII)


class ConfigError(Exception):
    """An unusable configuration value or config file."""


def fail(message):
    """Report a configuration error on stderr and exit with status 2."""
    print(f"tv3: error: {message}", file=sys.stderr)
    sys.exit(2)


def expand_path(path):
    """Return an absolute path with ``~`` and environment variables expanded.

    Raises ConfigError if the path refers to an undefined environment variable.
    """
    expanded = os.path.expandvars(os.path.expanduser(path))
    undefined = UNEXPANDED_VARIABLE.search(expanded)
    if undefined:
        raise ConfigError(f"undefined environment variable {undefined.group(0)} in {path}")
    return os.path.abspath(expanded)


def split_list(value):
    """Split a comma-separated setting into its non-empty, stripped entries."""
    return [item.strip() for item in value.split(",") if item.strip()]


def dotted(extension):
    """Return `extension` with a leading dot."""
    return extension if not extension or extension.startswith(".") else "." + extension


def read_config(config_file, explicit):
    """Read the [DEFAULT] section of the config file into a dict of typed values.

    A missing config file is an error only if it was given explicitly.
    Raises ConfigError for any other problem with the file.
    """
    config = configparser.ConfigParser(interpolation=None)
    try:
        with open(config_file, encoding="utf-8") as f:
            config.read_file(f)
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"config file not found: {config_file}") from None
        return {}
    except OSError as e:
        raise ConfigError(f"could not read config file {config_file}: {e.strerror}") from None
    except UnicodeDecodeError as e:
        raise ConfigError(f"config file {config_file} is not valid UTF-8: {e}") from None
    except configparser.Error as e:
        raise ConfigError(f"could not parse config file {config_file}: {e}") from None

    if config.sections():
        raise ConfigError(
            f"unknown section [{config.sections()[0]}] in {config_file} "
            f"(settings belong in [{configparser.DEFAULTSECT}])"
        )
    defaults = dict(config.defaults())
    for key in defaults:
        if key not in CONFIG_KEYS:
            raise ConfigError(f"unknown setting {key!r} in {config_file}")
    if "debug" in defaults:
        try:
            defaults["debug"] = config.getboolean(configparser.DEFAULTSECT, "debug")
        except ValueError:
            raise ConfigError(
                f"invalid value for 'debug' in {config_file}: {defaults['debug']!r} "
                "(expected true/false, yes/no, on/off or 1/0)"
            ) from None
    return defaults


def format_config(args):
    """Return the effective settings as the contents of a config file."""
    values = {
        "editor": args.editor,
        "extension": args.extension,
        "extensions": ", ".join(args.extensions),
        "exclude": ", ".join(args.exclude),
        "notes_dir": args.notes_dir,
        "log_file": args.log_file,
        "debug": args.debug,
    }
    lines = [f"[{configparser.DEFAULTSECT}]"]
    lines += [f"{key} = {values[key]}" for key in CONFIG_KEYS]
    return "\n".join(lines)


def setup_logging(log_file, debug):
    """Send all log records to a rotating log file, and nowhere else.

    The handler is attached to the root logger so that records from
    libraries are captured too; without a handler anywhere in the hierarchy
    logging would fall back to writing on stderr, over the terminal UI.

    Returns True if the log file was opened. Otherwise a warning is printed
    on stderr and records are discarded.
    """
    root = logging.getLogger()
    logging.getLogger("tv3").setLevel(logging.DEBUG if debug else logging.WARNING)
    try:
        handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
        )
    except OSError as e:
        print(
            f"tv3: warning: could not open log file {log_file}: {e.strerror}",
            file=sys.stderr,
            flush=True,
        )
        root.addHandler(logging.NullHandler())
        return False
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    return True


def main():
    """Main entry point for Terminal Velocity."""
    # First pass: get config file location
    parser = argparse.ArgumentParser(prog="tv3", add_help=False)
    parser.add_argument(
        "-c",
        "--config",
        action="store",
        default=None,
        dest="config",
        help=f"the config file to use (default: {DEFAULT_CONFIG_FILE})",
    )
    (args, _) = parser.parse_known_args()
    explicit_config = args.config is not None
    try:
        config_file = expand_path(args.config if explicit_config else DEFAULT_CONFIG_FILE)
        defaults = read_config(config_file, explicit_config)
    except ConfigError as e:
        fail(e)

    description = __doc__
    epilog = (
        "the config file can be used to override the defaults for the\n"
        "optional arguments, example config file contents:\n"
        "\n"
        "    [DEFAULT]\n"
        f"    editor = {DEFAULT_EDITOR}\n"
        "    # The filename extension to use for new files.\n"
        f"    extension = {DEFAULT_EXTENSION}\n"
        "    # The filename extensions to recognize in the notes dir.\n"
        f"    extensions = {DEFAULT_EXTENSIONS}\n"
        f"    exclude = {DEFAULT_EXCLUDE_DIRS}\n"
        f"    notes_dir = {DEFAULT_NOTES_DIR}\n"
        f"    log_file = {DEFAULT_LOG_FILE}\n"
        "    debug = false\n"
        "\n"
        "settings must be in the [DEFAULT] section; other sections and\n"
        "unknown settings are errors. a setting missing from the config\n"
        "file (or no config file) means the default is used. the output\n"
        "of --print-config is a valid config file.\n"
        "\n"
        "keys:\n"
        "  Enter           open the selected note, or create a note titled as typed\n"
        "  Tab             complete the selected note's title, one folder at a time\n"
        "  Up/Down         move through the list of notes\n"
        "  Esc             clear the selection, then the search text, then quit\n"
        "  Ctrl-R          rename the selected note\n"
        "  Ctrl-K          delete the selected note, after asking (y/N)\n"
        "  Ctrl-P          show or hide the preview pane\n"
        "  Ctrl-Q, Ctrl-X  quit, even from a rename or delete prompt\n"
    )

    # Second pass: full argument parsing with config defaults
    parser = argparse.ArgumentParser(
        prog="tv3",
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[parser],
    )
    parser.add_argument(
        "-e",
        "--editor",
        action="store",
        default=defaults.get("editor", os.getenv("EDITOR", DEFAULT_EDITOR)),
        dest="editor",
        help=(
            "the text editor to use; precedence: command line > config file > "
            f"$EDITOR > {DEFAULT_EDITOR}"
        ),
    )
    parser.add_argument(
        "-x",
        "--extension",
        action="store",
        default=defaults.get("extension", DEFAULT_EXTENSION),
        dest="extension",
        help=f"the filename extension for new notes (default: {DEFAULT_EXTENSION})",
    )
    parser.add_argument(
        "--extensions",
        action="store",
        default=defaults.get("extensions", DEFAULT_EXTENSIONS),
        dest="extensions",
        help=(
            "the filename extensions to recognize in the notes dir, a "
            f"comma-separated list (default: {DEFAULT_EXTENSIONS})"
        ),
    )
    parser.add_argument(
        "--exclude",
        action="store",
        default=defaults.get("exclude", DEFAULT_EXCLUDE_DIRS),
        dest="exclude",
        help=(
            "the file/directory names to skip while recursively searching "
            "the notes dir for notes, a comma-separated list "
            f"(default: {DEFAULT_EXCLUDE_DIRS})"
        ),
    )
    parser.add_argument(
        "-d",
        "--debug",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="debug",
        help="log debug messages to the log file, or not (default: the config file, else off)",
    )
    parser.add_argument(
        "-l",
        "--log-file",
        action="store",
        default=defaults.get("log_file", DEFAULT_LOG_FILE),
        dest="log_file",
        help=f"the file to log to (default: {DEFAULT_LOG_FILE})",
    )
    parser.add_argument(
        "-p",
        "--print-config",
        action="store_true",
        default=False,
        dest="print_config",
        help="print the effective configuration, in config file format, then exit",
    )
    parser.add_argument(
        "notes_dir",
        action="store",
        default=defaults.get("notes_dir", DEFAULT_NOTES_DIR),
        help=f"the notes directory to use (default: {DEFAULT_NOTES_DIR})",
        nargs="?",
    )

    args = parser.parse_args()

    if args.debug is None:
        args.debug = defaults.get("debug", False)
    for key in ("notes_dir", "log_file"):
        try:
            setattr(args, key, expand_path(getattr(args, key)))
        except ConfigError as e:
            fail(f"{key}: {e}")
    args.extension = dotted(args.extension.strip())
    args.extensions = [dotted(ext) for ext in split_list(args.extensions)]
    args.exclude = split_list(args.exclude)

    if args.print_config:
        print(format_config(args))
        sys.exit(0)

    logging_to_file = setup_logging(args.log_file, args.debug)
    logger = logging.getLogger("tv3")
    logger.debug(f"Starting Terminal Velocity with args: {args}")

    try:
        urwid_ui.launch(
            notes_dir=args.notes_dir,
            editor=args.editor,
            extension=args.extension,
            extensions=args.extensions,
            exclude=args.exclude,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        print(f"\nFatal error: {e}", file=sys.stderr)
        if logging_to_file:
            print("See log file for details:", args.log_file, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
