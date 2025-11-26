"""Pytest configuration and shared fixtures for tv3 tests."""

import os
import time

import pytest


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

    for filename, content in notes.items():
        filepath = os.path.join(temp_notes_dir, filename)
        with open(filepath, "w") as f:
            f.write(content)
        # Sleep briefly to ensure different mtimes
        time.sleep(0.01)

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

    for filepath, content in notes.items():
        full_path = os.path.join(temp_notes_dir, filepath)
        with open(full_path, "w") as f:
            f.write(content)
        time.sleep(0.01)

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
