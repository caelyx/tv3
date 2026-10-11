"""Pytest configuration and shared fixtures for tv3 tests."""

import os
import time
from collections.abc import Callable

import pytest

from tv3 import tv_notebook

BASE_MTIME = 1_700_000_000


def _write_notes(notes_dir, notes):
    """Write `notes` ({relpath: content}) with strictly increasing mtimes."""
    for i, (relpath, content) in enumerate(notes.items()):
        filepath = os.path.join(notes_dir, relpath)
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


@pytest.fixture
def sample_config(temp_notes_dir, tmp_path):
    """Create a sample configuration file."""
    config_file = tmp_path / "test_tvrc"
    config_content = f"""[DEFAULT]
editor = vim
extension = .txt
extensions = .txt, .md, .markdown
notes_dir = {temp_notes_dir}
exclude = backup, tmp
debug = False
log_file = {tmp_path}/test_tv.log
"""
    config_file.write_text(config_content)
    return str(config_file)


@pytest.fixture
def mock_editor(tmp_path, monkeypatch):
    """Create a mock editor script that just touches a file."""
    editor_script = tmp_path / "mock_editor.sh"
    editor_script.write_text(
        """#!/bin/bash
# Mock editor that just creates/touches the file
touch "$1"
echo "Mock editor called with: $1" >> {}
""".format(tmp_path / "editor_log.txt")
    )
    editor_script.chmod(0o755)

    # Set it as the default editor
    monkeypatch.setenv("EDITOR", str(editor_script))

    return str(editor_script)


@pytest.fixture
def notebook():
    """Factory for PlainTextNoteBooks that are always closed at teardown.

    Defaults: extension ".txt", extensions [".txt", ".md"].
    """
    notebooks = []

    def make(path, extension=".txt", extensions=(".txt", ".md"), **kwargs):
        nb = tv_notebook.PlainTextNoteBook(str(path), extension, list(extensions), **kwargs)
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
