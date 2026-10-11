# Terminal Velocity 3

Terminal Velocity 3 is a TUI application for managing plain-text notes.
It combines finding and creating notes in a single minimal interface and
delegates the note-taking itself to your `$EDITOR`.

![Terminal Velocity 3 Screencast](./tv3.gif)

The interface concept is taken from [Notational
Velocity](http://notational.net/).

This is an active fork of
[terminal-velocity-notes/terminal_velocity](https://github.com/terminal-velocity-notes/terminal_velocity)
and a port from Legacy Python to **Python 3**.

## Installation

TV3 is installed as a [uv](https://docs.astral.sh/uv/) tool, which gives it
its own isolated environment and puts `tv3` on your `$PATH`:

```bash
uv tool install git+https://github.com/caelyx/tv3
```

Or from a clone:

```bash
git clone https://github.com/caelyx/tv3
cd tv3
uv tool install .
```

To upgrade later:

```bash
uv tool upgrade tv3
```

## Development

```bash
uv sync          # create .venv with tv3 and the dev tools
uv run tv3       # run the app from the checkout
uv run pytest    # run the test suite
```
