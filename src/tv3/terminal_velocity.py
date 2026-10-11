#!/usr/bin/env python3
"""A fast note-taking app for the UNIX terminal."""

import argparse
import configparser
import logging
import logging.handlers
import os
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


def expand_path(path):
    """Return an absolute path with ``~`` and environment variables expanded."""
    return os.path.abspath(os.path.expandvars(os.path.expanduser(path)))


def read_config(parser, config_file, explicit):
    """Read the [DEFAULT] section of the config file into a dict of typed values.

    A missing config file is an error only if it was given explicitly.
    Errors are reported through ``parser.error``, which exits.
    """
    config = configparser.ConfigParser(interpolation=None)
    if not os.path.exists(config_file):
        if explicit:
            parser.error(f"config file not found: {config_file}")
        return {}
    try:
        config.read(config_file)
    except configparser.Error as e:
        parser.error(f"could not parse config file {config_file}: {e}")

    defaults = dict(config.defaults())
    if "debug" in defaults:
        try:
            defaults["debug"] = config.getboolean(configparser.DEFAULTSECT, "debug")
        except ValueError:
            parser.error(
                f"invalid value for 'debug' in {config_file}: {defaults['debug']!r} "
                "(expected true/false, yes/no, on/off or 1/0)"
            )
    return defaults


def setup_logging(log_file, debug):
    """Send all log records to a rotating log file, and nowhere else.

    The handler is attached to the root logger so that records from
    libraries are captured too; without a handler anywhere in the hierarchy
    logging would fall back to writing on stderr, over the terminal UI.
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
        print(f"Warning: Could not create log file {log_file}: {e}", file=sys.stderr)
        root.addHandler(logging.NullHandler())
        return
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)


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
    config_file = expand_path(args.config if explicit_config else DEFAULT_CONFIG_FILE)
    defaults = read_config(parser, config_file, explicit_config)

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
        f"    notes_dir = {DEFAULT_NOTES_DIR}\n"
        "\n"
        "if there is no config file (or an argument is missing from the\n"
        "config file) the default will be used\n"
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
        action="store_true",
        default=defaults.get("debug", False),
        dest="debug",
        help="debug logging on or off (default: off)",
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
        help="print your configuration settings then exit",
    )
    parser.add_argument(
        "notes_dir",
        action="store",
        default=defaults.get("notes_dir", DEFAULT_NOTES_DIR),
        help=f"the notes directory to use (default: {DEFAULT_NOTES_DIR})",
        nargs="?",
    )

    args = parser.parse_args()

    args.config = config_file
    args.notes_dir = expand_path(args.notes_dir)
    args.log_file = expand_path(args.log_file)
    args.extensions = [ext.strip() for ext in args.extensions.split(",") if ext.strip()]
    args.exclude = [name.strip() for name in args.exclude.split(",")]

    if args.print_config:
        print(args)
        sys.exit(0)

    setup_logging(args.log_file, args.debug)
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
        print("See log file for details:", args.log_file, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
