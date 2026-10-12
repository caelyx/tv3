"""Pytest configuration and shared fixtures for tv3 tests."""

import json
import os
import shlex
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.ui_helpers import SIZE, RecordingScreen
from tv3 import tv_notebook, urwid_ui

BASE_MTIME = 1_700_000_000


def _write_notes(notes_dir, notes):
    """Write `notes` ({relpath: content}) with strictly increasing mtimes."""
    for i, (relpath, content) in enumerate(notes.items()):
        filepath = os.path.join(notes_dir, relpath)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w") as f:
            f.write(content)
        os.utime(filepath, (BASE_MTIME + i, BASE_MTIME + i))


@pytest.fixture
def temp_notes_dir(tmp_path):
    """Create a temporary directory for notes and clean it up after the test."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    yield str(notes_dir)
    # Cleanup is automatic with tmp_path


@pytest.fixture
def populated_notes_dir(temp_notes_dir):
    """Create a notes directory with some sample notes."""
    notes = {
        "first_note.txt": "This is the first note.\nIt has multiple lines.",
        "second_note.md": "# Second Note\nThis is markdown.",
        "meeting_notes.txt": "Meeting on 2025-01-15\nDiscussed project timeline.",
        "todo.md": "- Buy groceries\n- Call mom\n- Fix bug",
    }
    _write_notes(temp_notes_dir, notes)
    return temp_notes_dir


@pytest.fixture
def nested_notes_dir(temp_notes_dir):
    """Create a notes directory with nested subdirectories."""
    # Create subdirectories
    os.makedirs(os.path.join(temp_notes_dir, "work"))
    os.makedirs(os.path.join(temp_notes_dir, "personal"))
    os.makedirs(os.path.join(temp_notes_dir, "archive"))

    notes = {
        "root_note.txt": "Root level note",
        "work/project_a.md": "Project A details",
        "work/project_b.md": "Project B details",
        "personal/journal.txt": "Personal journal entry",
        "archive/old_note.txt": "Archived note",
    }
    _write_notes(temp_notes_dir, notes)
    return temp_notes_dir


STUB_EDITOR_SCRIPT = """#!{python}
import json
import os
import sys

with open({log!r}, "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
text = os.environ.get("STUB_EDITOR_APPEND")
if text:
    with open(sys.argv[-1], "a") as note:
        note.write(text)
signum = os.environ.get("STUB_EDITOR_SIGNAL")
if signum:
    os.kill(os.getpid(), int(signum))
sys.exit(int(os.environ.get("STUB_EDITOR_EXIT", "0")))
"""


@dataclass
class StubEditor:
    """A real executable standing in for $EDITOR.

    Each run appends its argv (without the program name) as a JSON line to
    `log`. Environment: STUB_EDITOR_EXIT sets its exit status,
    STUB_EDITOR_APPEND is appended to the file named by its last argument,
    STUB_EDITOR_SIGNAL is a signal number the editor sends itself.
    """

    command: str
    log: Path

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


def _make_stub_editor(directory: Path) -> StubEditor:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "stub-editor"
    log = directory / "editor-calls.jsonl"
    script.write_text(STUB_EDITOR_SCRIPT.format(python=sys.executable, log=str(log)))
    script.chmod(0o755)
    return StubEditor(command=shlex.quote(str(script)), log=log)


@pytest.fixture
def stub_editor(tmp_path):
    """A StubEditor whose script lives in tmp_path/editor."""
    return _make_stub_editor(tmp_path / "editor")


@pytest.fixture
def make_stub_editor():
    """Factory: StubEditor with its script in the given directory."""
    return _make_stub_editor


@pytest.fixture
def write_notes():
    """The helper `write_notes(notes_dir, {relpath: content})`; later notes are newer."""
    return _write_notes


@pytest.fixture
def notebook():
    """Factory for PlainTextNoteBooks that are always closed at teardown.

    Defaults: extension ".txt", extensions [".txt", ".md"]. `cls` makes an
    instance of a PlainTextNoteBook subclass instead.
    """
    notebooks = []

    def make(
        path,
        extension=".txt",
        extensions=(".txt", ".md"),
        cls=tv_notebook.PlainTextNoteBook,
        **kwargs,
    ):
        nb = cls(str(path), extension, list(extensions), **kwargs)
        notebooks.append(nb)
        return nb

    yield make
    for nb in notebooks:
        nb.close()


@pytest.fixture
def unwatched_notebook(notebook):
    """Factory for notebooks whose filesystem observer is already stopped.

    Tests drive `FileEventHandler` directly with watchdog event objects, so
    the outcome does not depend on the timing of real filesystem events.
    """

    def make(path, **kwargs):
        nb = notebook(path, **kwargs)
        nb.close()
        return nb

    return make


def _wait_until(predicate: Callable[[], bool], timeout: float = 5, interval: float = 0.01):
    """Poll `predicate` until it returns truthy; fail the test after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"condition not met within {timeout}s")
        time.sleep(interval)


@pytest.fixture
def wait_until():
    """The polling helper `wait_until(predicate, timeout=5)`."""
    return _wait_until


@pytest.fixture
def notes_dir(temp_notes_dir):
    """`temp_notes_dir` as a Path."""
    return Path(temp_notes_dir)


@pytest.fixture
def make_frame(notes_dir, stub_editor, write_notes):
    """Factory: a MainFrame run by a headless urwid_ui.MainLoop, closed at teardown.

    `notes` ({relpath: content}) are written first, later ones newer. The
    screen is `size` (columns, rows). Unless
    `wait_for_read` is false, the notebook has read every note before the
    frame is returned.
    """
    frames = []

    def make(
        notes=None,
        editor=None,
        extension=".txt",
        extensions=(".txt", ".md"),
        wait_for_read=True,
        size=SIZE,
    ):
        write_notes(notes_dir, notes or {})
        frame = urwid_ui.MainFrame(
            str(notes_dir),
            stub_editor.command if editor is None else editor,
            extension,
            list(extensions),
        )
        frames.append(frame)
        if wait_for_read:
            _wait_until(lambda: frame.tv_notebook.loaded)
        urwid_ui.MainLoop(frame, urwid_ui.palette, screen=RecordingScreen(size), handle_mouse=False)
        return frame

    yield make
    for frame in frames:
        frame.close()
