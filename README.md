# Terminal Velocity 3

Terminal Velocity 3 is a TUI application for managing plain-text notes.
It combines finding and creating notes in a single minimal interface and
delegates the note-taking itself to your `$EDITOR`.
Titles are fuzzy-matched and matches are highlighted, with a snippet of the
matching line; notes can be renamed (`Ctrl-R`), deleted (`Ctrl-K`) and
previewed (`Ctrl-P`) without leaving the list.

![Terminal Velocity 3 Screencast](./tv3.gif)

The interface concept is taken from [Notational
Velocity](http://notational.net/).

This is an active fork of
[terminal-velocity-notes/terminal_velocity](https://github.com/terminal-velocity-notes/terminal_velocity)
and a port from Legacy Python to **Python 3**.

## Installation

TV3 needs Python 3.11 or newer and runs on Linux and macOS. It is installed as a [uv](https://docs.astral.sh/uv/) tool, which gives it
its own isolated environment and installs the `tv3` executable into uv's tool
bin directory (usually `~/.local/bin`):

```bash
uv tool install git+https://github.com/caelyx/tv3
```

Or from a clone:

```bash
git clone https://github.com/caelyx/tv3
cd tv3
uv tool install .
```

On macOS with Python 3.14 or newer, the `watchdog` dependency has no
prebuilt wheel and is compiled during install, which needs the Xcode
Command Line Tools (`xcode-select --install`). Alternatively, install with
an older Python: `uv tool install --python 3.13 ...`.

If your shell can't find `tv3`, that directory isn't on your `$PATH`. Run
`uv tool update-shell` and restart your shell.

To upgrade a git install:

```bash
uv tool upgrade tv3
```

To upgrade a clone install, from the clone:

```bash
git pull && uv tool upgrade tv3
```

## Usage

Run `tv3 [notes_dir]`; the notes directory defaults to `~/Notes`. Defaults can
be set in `~/.tvrc`; see `tv3 --help` for all options.
The editor is split like a shell command, so quote a path that contains spaces.

Type to search, or to name a new note. Titles that start with what you typed
come first, then fuzzy title matches, then notes that contain every word. The
top note is selected only if its full title starts with what you typed, so
typing `pasta` doesn't select `recipes/Pasta sauce`, and `Enter` creates `pasta`.

| Key                | Action                                                   |
| ------------------ | -------------------------------------------------------- |
| `Enter`            | open the selected note, or create a note titled as typed |
| `Tab`              | complete the search text to the selected note's title    |
| `Up`/`Down`        | move through the list of notes                           |
| `Esc`              | clear the selection, then the search text, then quit     |
| `Ctrl-R`           | rename the selected note                                 |
| `Ctrl-K`           | delete the selected note, after asking (y/N)             |
| `Ctrl-P`           | show or hide the preview pane                            |
| `Ctrl-Q`, `Ctrl-X` | quit, even from a rename or delete prompt                |

`tv3 --help` lists these keys and the command-line options.

## Development

```bash
uv sync          # create .venv with tv3 and the dev tools
uv run tv3       # run the app from the checkout
uv run pytest    # run the test suite
```
