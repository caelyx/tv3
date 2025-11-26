#!/usr/bin/env python3
"""A fast note-taking app for the UNIX terminal."""

import argparse
import configparser
import logging
import logging.handlers
import os
import sys

import urwid_ui

# Configuration constants
DEFAULT_EDITOR = "pico"
DEFAULT_EXTENSION = "txt"
DEFAULT_EXTENSIONS = ".txt, .md, .markdown, .rst"
DEFAULT_NOTES_DIR = "~/Notes"
DEFAULT_EXCLUDE_DIRS = "src, backup, ignore, tmp, old"
DEFAULT_LOG_FILE = "~/.tvlog"
DEFAULT_CONFIG_FILE = "~/.tvrc"

# Logging constants
LOG_MAX_BYTES = 1_000_000  # 1 megabyte
LOG_BACKUP_COUNT = 0


def main():
    """Main entry point for Terminal Velocity."""
    # First pass: get config file location
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "-c",
        "--config",
        action="store",
        default=DEFAULT_CONFIG_FILE,
        dest="config",
        help=f"the config file to use (default: {DEFAULT_CONFIG_FILE})",
    )
    (args, _) = parser.parse_known_args()
    config_file = os.path.abspath(os.path.expanduser(args.config))

    # Read config file
    config = configparser.ConfigParser()
    try:
        config.read(config_file)
        defaults = dict(config.items("DEFAULT"))
    except (configparser.Error, KeyError) as e:
        # Config file doesn't exist or is invalid - use empty defaults
        defaults = {}
        if os.path.exists(config_file):
            print(f"Warning: Could not parse config file {config_file}: {e}", file=sys.stderr)

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
        "if there is no config file (or an argument is missing from the)\n"
        "config file the default default will be used\n"
    )

    # Second pass: full argument parsing with config defaults
    parser = argparse.ArgumentParser(
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
        help=f"the text editor to use (default: $EDITOR or {DEFAULT_EDITOR})",
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

    # Parse extension and exclude lists
    args.extensions = [ext.strip() for ext in args.extensions.split(",")]
    args.exclude = [name.strip() for name in args.exclude.split(",")]

    if args.print_config:
        print(args)
        sys.exit(0)

    # Set up logging
    logger = logging.getLogger("tv3")
    logger.setLevel(logging.DEBUG)

    try:
        log_file_path = os.path.abspath(os.path.expanduser(args.log_file))
        fh = logging.handlers.RotatingFileHandler(
            log_file_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
        )
        if args.debug:
            fh.setLevel(logging.DEBUG)
        else:
            fh.setLevel(logging.WARNING)
        logger.addHandler(fh)
    except OSError as e:
        print(f"Warning: Could not create log file {args.log_file}: {e}", file=sys.stderr)

    # Stream handler for critical errors only
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.CRITICAL)
    logger.addHandler(sh)

    logger.debug(f"Starting Terminal Velocity with args: {args}")

    # Launch the UI with proper error handling
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
