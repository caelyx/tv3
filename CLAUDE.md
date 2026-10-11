# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Terminal Velocity 3 (tv3) is a TUI (Text User Interface) application for managing plain-text notes. It's a Python 3 port of terminal-velocity-notes/terminal_velocity, inspired by Notational Velocity. The app combines finding and creating notes in a single minimal interface and delegates actual note editing to the user's `$EDITOR`.

## Architecture

The codebase is the `tv3` package in `src/tv3/`, organized into four modules
(`__main__.py` makes `python -m tv3` run the CLI):

1. **src/tv3/terminal_velocity.py** - Entry point and CLI argument handling
   - Parses command-line arguments and config file (`~/.tvrc`)
   - Sets up logging to `~/.tvlog`
   - Launches the urwid UI with configured parameters

2. **src/tv3/tv_notebook.py** - Data layer for note storage and search
   - `PlainTextNoteBook`: Manages a directory of plain text notes
   - `PlainTextNote`: Represents individual note files
   - `brute_force_search()`: Implements case-sensitive/insensitive search across note titles and contents
   - `PlainTextNoteBook.find()`: Returns ranked `Result`s carrying title highlight spans and a matching-line snippet
   - `PlainTextNoteBook.rename()` / `delete()`: Rename or delete a note's file; both refuse a note that has changed on disk since it was listed
   - Content cache: each note's first 1 MiB and mtime are kept in memory and re-read on watcher events, so searches read no files
   - `loaded`: A background thread reads every note at startup; `loaded` is true once it finishes, and searches with words wait for it
   - `FileEventHandler`: Uses watchdog library to monitor filesystem changes and automatically sync notes in real-time

3. **src/tv3/urwid_ui.py** - Terminal UI layer
   - `MainFrame`: Top-level widget coordinating the UI
   - `AutocompleteWidget`: Search box with autocomplete functionality
   - `NoteFilterListBox`: Filterable list of notes
   - `NoteWidget`: Individual note display in the list
   - All UI logic including keyboard shortcuts and editor launching

4. **src/tv3/ranking.py** - Pure functions for ranking and highlighting
   - Fuzzy title matching with pfzy's fzy scorer
   - Match spans for highlighting, and the matching-line snippet shown under a note
   - Snippets are cut to a width in terminal columns, measured with wcwidth as urwid measures them, so wide characters (CJK, emoji) never push the match or the closing `…` off-screen
   - `printable()`: Replaces control characters with spaces (used for snippets and the preview)

### Key Architecture Notes

- The notebook automatically watches the notes directory for external changes using watchdog's Observer pattern
- Result order (`PlainTextNoteBook.find()`). An exact match has every query word in its title or contents (smart case):
  - Empty query: every note, newest first
  - 1. Exact matches whose title equals the query (ignoring case), newest first
  - 2. Other exact matches whose full title starts with the query (ignoring case), newest first
  - 3. Fuzzy title matches, best score first. Fuzzy matching needs a query of 3+ characters and is against the title's last path component, or the full title if the query contains `/`
  - 4. The remaining exact matches, newest first
- The selection is always the top row, and is set automatically only when that row is an exact match whose full title starts with the typed text (NV prefix semantics): typing `pasta` does not select `recipes/Pasta sauce`, so `Enter` creates `pasta` at the root. A note found only by fuzzy matching is never selected automatically
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

All settings must be in the `[DEFAULT]` section; any other section, or an unknown setting, is an error. `tv3 -p` prints the effective configuration in this format, so its output can be pasted back into `~/.tvrc`.

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
- **watchdog 6.x**: Filesystem monitoring for automatic note syncing
- **pfzy 0.3.x**: fzy fuzzy scoring for title ranking
- **wcwidth**: Terminal column widths for snippets (also a dependency of urwid)

## UI Keyboard Shortcuts

- `Enter`: Open selected note in editor (or create new note if typed a new title)
- `Esc`, `Ctrl-D`, `Ctrl-U`: Clear selection/search or quit
- `Ctrl-Q`, `Ctrl-X`, `Ctrl-C`: Quit application, also from the rename and delete prompts (while the editor runs, `Ctrl-C` goes to the editor only)
- `Tab`, `Left`, `Right`: Accept autocomplete suggestion
- `Up`/`Down`, `Page Up`/`Page Down`: Navigate note list
- `Backspace`: Delete search text or clear autocomplete
- Typing or `Backspace` while a navigated-to note's title replaces the search text edits that title
- `Ctrl-R`: Rename the selected note in a status-line prompt prefilled with its title; `Ctrl-U` clears it, `Enter` applies, `Esc` cancels. If the rename fails, the prompt stays open with the error above it
- `Ctrl-K`: Delete the selected note after a `y/N` confirmation; `y` or `Y` deletes and shows "Deleted '…'", any other key cancels
- `Ctrl-P`: Toggle a preview of the selected note, or of the top note when none is selected (beside the list at 100+ columns, below it otherwise)

`tv3 --help` lists the main keys; keep its list (in `terminal_velocity.py`) and the README's in step.

## Code Notes

- `terminal_velocity.py`, `tv_notebook.py` and `urwid_ui.py` use the same logger: `logging.getLogger("tv3")`
- Note titles can include directory separators for organizing notes in subdirectories
- The `PlainTextNote.contents` property handles UTF-8 decoding with error tolerance
- Search is case-insensitive if search terms are lowercase, case-sensitive otherwise
- Only the first 1 MiB of a note is searchable; the rest is not read
- Changes made to a network-mounted notes folder by other machines may not be seen until the file is changed again locally or tv3 restarts: the watcher only reports local filesystem events
