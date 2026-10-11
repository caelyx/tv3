# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Terminal Velocity 3 (tv3) is a TUI (Text User Interface) application for managing plain-text notes. It's a Python 3 port of terminal-velocity-notes/terminal_velocity, inspired by Notational Velocity. The app combines finding and creating notes in a single minimal interface and delegates actual note editing to the user's `$EDITOR`.

## Architecture

The codebase is the `tv3` package in `src/tv3/`, organized into three main modules
(`__main__.py` makes `python -m tv3` run the CLI):

1. **src/tv3/terminal_velocity.py** - Entry point and CLI argument handling
   - Parses command-line arguments and config file (`~/.tvrc`)
   - Sets up logging to `~/.tvlog`
   - Launches the urwid UI with configured parameters

2. **src/tv3/tv_notebook.py** - Data layer for note storage and search
   - `PlainTextNoteBook`: Manages a directory of plain text notes
   - `PlainTextNote`: Represents individual note files
   - `brute_force_search()`: Implements case-sensitive/insensitive search across note titles and contents
   - `FileEventHandler`: Uses watchdog library to monitor filesystem changes and automatically sync notes in real-time

3. **src/tv3/urwid_ui.py** - Terminal UI layer
   - `MainFrame`: Top-level widget coordinating the UI
   - `AutocompleteWidget`: Search box with autocomplete functionality
   - `NoteFilterListBox`: Filterable list of notes
   - `NoteWidget`: Individual note display in the list
   - All UI logic including keyboard shortcuts and editor launching

### Key Architecture Notes

- The notebook automatically watches the notes directory for external changes using watchdog's Observer pattern
- Notes are filtered and sorted by modification time (newest first)
- The UI provides synchronized autocomplete between the search box and filtered note list
- Editor integration uses subprocess to launch external editors and returns to the UI when done

## Installation and Development

Packaging is pyproject-only with the `uv_build` backend; dependencies are locked in `uv.lock`.

The development Python is pinned in `.python-version` (3.14). Run project commands through `uv run` so they use the project's `.venv`.

### Set up the development environment:
```bash
uv sync
```

### Run the application from the checkout:
```bash
uv run tv3 [options] [notes_dir]
uv run python -m tv3 [options] [notes_dir]
```

### Install as a tool:
```bash
uv tool install .
tv3 [options] [notes_dir]
```

### Test, lint, type-check:
```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Tests live in `tests/`; see `TESTING.md` for testing guidance and `./run_tests.sh help` for the convenience runner.

### Configuration

The app reads from `~/.tvrc` by default. Example config:
```ini
[DEFAULT]
editor = vim
extension = .md
extensions = .txt, .md, .markdown, .rst
notes_dir = ~/Notes
exclude = src, backup, ignore, tmp, old
debug = False
log_file = ~/.tvlog
```

### Command-line options:
- `-c, --config`: Specify config file path (default: `~/.tvrc`)
- `-e, --editor`: Text editor to use (default: `$EDITOR` environment variable, or `pico` if not set)
- `-x, --extension`: Extension for new notes (default: `.txt`)
- `--extensions`: Comma-separated list of extensions to recognize
- `--exclude`: Comma-separated list of directories/files to skip
- `-d, --debug` / `--no-debug`: Enable or disable debug logging (overrides the config file)
- `-l, --log-file`: Log file path (default: `~/.tvlog`)
- `-p, --print-config`: Print the effective configuration in config-file format and exit

## Dependencies

- **urwid 4.x**: Terminal UI framework
- **watchdog**: Filesystem monitoring for automatic note syncing

## UI Keyboard Shortcuts

- `Enter`: Open selected note in editor (or create new note if typed a new title)
- `Esc`, `Ctrl-D`, `Ctrl-U`: Clear selection/search or quit
- `Ctrl-Q`, `Ctrl-X`: Quit application
- `Tab`, `Left`, `Right`: Accept autocomplete suggestion
- `Up`/`Down`, `Page Up`/`Page Down`: Navigate note list
- `Backspace`: Delete search text or clear autocomplete

## Code Notes

- All three modules use the same logger: `logging.getLogger("tv3")`
- Note titles can include directory separators for organizing notes in subdirectories
- The `PlainTextNote.contents` property handles UTF-8 decoding with error tolerance
- Search is case-insensitive if search terms are lowercase, case-sensitive otherwise
