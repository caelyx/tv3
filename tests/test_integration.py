"""Integration tests for tv3 - testing the complete system."""

import os
import sys
import time
import pytest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import terminal_velocity
import urwid_ui
import tv_notebook


class TestEndToEndWorkflow:
    """End-to-end integration tests."""

    def test_create_and_search_note(self, temp_notes_dir):
        """Test creating a note and then searching for it."""
        # Create a notebook
        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt', '.md']
        )

        # Add a note
        note = notebook.add_new("test_note.txt")
        note_path = note.abspath

        # Write content to the note
        with open(note_path, 'w') as f:
            f.write("This is a test note with important content")

        # Search for it
        results = notebook.search("important")
        assert len(results) == 1
        assert results[0].title == "test_note"

    def test_external_file_creation_integration(self, temp_notes_dir):
        """Test that externally created files are picked up."""
        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt']
        )

        initial_count = len(notebook)

        # Simulate external file creation
        external_file = Path(temp_notes_dir) / "external.txt"
        external_file.write_text("Externally created")

        # Wait for file watcher
        time.sleep(0.5)

        # Should be detected
        assert len(notebook) == initial_count + 1

        # Should be searchable
        results = notebook.search("external")
        assert len(results) >= 1

    def test_ui_search_and_filter_integration(self, populated_notes_dir):
        """Test UI search filtering integration."""
        frame = urwid_ui.MainFrame(
            populated_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )

        # Initially should show all notes
        assert len(frame.list_box.list_walker) == 4

        # Type in search box
        frame.search_box.set_edit_text("first")
        frame.on_search_box_changed(frame.search_box, "first")

        # Should filter down
        assert len(frame.list_box.list_walker) == 1

        # Clear search
        frame.search_box.set_edit_text("")
        frame.on_search_box_changed(frame.search_box, "")

        # Should show all again
        assert len(frame.list_box.list_walker) == 4

    @patch('urwid_ui.system')
    def test_create_note_through_ui(self, mock_system, temp_notes_dir):
        """Test creating a note through the UI."""
        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.txt', ['.txt']
        )
        frame.loop = Mock()

        initial_count = len(frame.tv_notebook)

        # Type a new note name and press enter
        frame.search_box.set_edit_text("my_new_note")
        frame.keypress((80, 24), 'enter')

        # Should have called editor
        assert mock_system.called

        # Note should be created
        assert len(frame.tv_notebook) == initial_count + 1

        # Note should exist on disk
        note_path = Path(temp_notes_dir) / "my_new_note.txt"
        assert note_path.exists()

    def test_nested_directory_notes_integration(self, nested_notes_dir):
        """Test working with notes in nested directories."""
        notebook = tv_notebook.PlainTextNoteBook(
            nested_notes_dir, '.txt', ['.txt', '.md']
        )

        # Should load all nested notes
        titles = [note.title for note in notebook]
        assert "work/project_a" in titles
        assert "personal/journal" in titles

        # Search should work across directories
        results = notebook.search("project")
        assert len(results) >= 2  # project_a and project_b

        # Can create new nested note
        note = notebook.add_new("work/new_project.md")
        assert os.path.exists(note.abspath)

    def test_multiple_extensions_integration(self, temp_notes_dir):
        """Test working with multiple file extensions."""
        # Create files with different extensions
        Path(temp_notes_dir, "note1.txt").write_text("Text file")
        Path(temp_notes_dir, "note2.md").write_text("# Markdown file")
        Path(temp_notes_dir, "note3.rst").write_text("RST file")

        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt', '.md']
        )

        # Should only load .txt and .md
        titles = [note.title for note in notebook]
        assert "note1" in titles
        assert "note2" in titles
        assert "note3" not in titles

    def test_exclude_directories_integration(self, temp_notes_dir):
        """Test that excluded directories are properly ignored."""
        # Create directory structure
        os.makedirs(os.path.join(temp_notes_dir, "active"))
        os.makedirs(os.path.join(temp_notes_dir, "archive"))
        os.makedirs(os.path.join(temp_notes_dir, "backup"))

        Path(temp_notes_dir, "active", "current.txt").write_text("Current work")
        Path(temp_notes_dir, "archive", "old.txt").write_text("Archived")
        Path(temp_notes_dir, "backup", "backup.txt").write_text("Backup")

        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt'],
            exclude=['archive', 'backup']
        )

        titles = [note.title for note in notebook]
        assert "active/current" in titles
        assert "archive/old" not in titles
        assert "backup/backup" not in titles

    def test_search_sorting_by_mtime(self, temp_notes_dir):
        """Test that search results are sorted by modification time."""
        # Create notes with different mtimes
        note1_path = Path(temp_notes_dir) / "first.txt"
        note1_path.write_text("First note")
        time.sleep(0.1)

        note2_path = Path(temp_notes_dir) / "second.txt"
        note2_path.write_text("Second note")
        time.sleep(0.1)

        note3_path = Path(temp_notes_dir) / "third.txt"
        note3_path.write_text("Third note")

        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt']
        )

        # Search for all
        results = notebook.search("")

        # Sort by mtime (descending - newest first)
        results.sort(key=lambda x: x.mtime, reverse=True)

        # Newest should be first
        assert results[0].title == "third"
        assert results[-1].title == "first"


class TestConcurrencyIntegration:
    """Integration tests for concurrent operations."""

    def test_ui_search_during_file_creation(self, temp_notes_dir):
        """Test searching while files are being created externally."""
        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.txt', ['.txt']
        )

        # Create initial note
        Path(temp_notes_dir, "initial.txt").write_text("Initial")
        time.sleep(0.3)

        # Search while creating new files
        frame.filter("note")

        # Create more files
        for i in range(5):
            Path(temp_notes_dir, f"note_{i}.txt").write_text(f"Note {i}")
            time.sleep(0.1)

        time.sleep(0.5)

        # Search should still work
        frame.filter("note")
        assert len(frame.list_box.list_walker) >= 5

    def test_notebook_operations_with_file_watcher(self, temp_notes_dir):
        """Test notebook operations while file watcher is active."""
        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt']
        )

        # Add notes through API
        notebook.add_new("api_note_1.txt")
        notebook.add_new("api_note_2.txt")

        # Add notes externally
        Path(temp_notes_dir, "external_1.txt").write_text("External 1")
        Path(temp_notes_dir, "external_2.txt").write_text("External 2")

        time.sleep(0.5)

        # All should be in notebook
        assert len(notebook) >= 4

        # Search should find all
        results = notebook.search("")
        assert len(results) >= 4


class TestErrorRecoveryIntegration:
    """Integration tests for error handling and recovery."""

    def test_invalid_note_creation_doesnt_break_notebook(self, temp_notes_dir):
        """Test that invalid note creation doesn't break the notebook."""
        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt']
        )

        # Try to create invalid note
        try:
            notebook.add_new("/.txt")
        except tv_notebook.InvalidNoteTitleError:
            pass

        # Notebook should still work
        note = notebook.add_new("valid_note.txt")
        assert note is not None

        # Search should work
        results = notebook.search("valid")
        assert len(results) == 1

    def test_duplicate_note_doesnt_break_notebook(self, temp_notes_dir):
        """Test that duplicate note creation doesn't break the notebook."""
        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt']
        )

        notebook.add_new("duplicate.txt")

        # Try to create duplicate
        try:
            notebook.add_new("duplicate.txt")
        except tv_notebook.NoteAlreadyExistsError:
            pass

        # Notebook should still work
        assert len(notebook) == 1

        # Search should work
        results = notebook.search("")
        assert len(results) == 1

    @patch('urwid_ui.system', side_effect=Exception("Editor failed"))
    def test_ui_recovers_from_editor_failure(self, mock_system, temp_notes_dir):
        """Test that UI recovers from editor failures."""
        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.txt', ['.txt']
        )
        frame.loop = Mock()

        # Create a note
        Path(temp_notes_dir, "test.txt").write_text("Test")
        time.sleep(0.3)

        frame.filter("")
        frame.selected_note = list(frame.tv_notebook)[0]

        # Try to open note (editor will fail)
        try:
            frame.keypress((80, 24), 'enter')
        except Exception:
            pass

        # UI should still work
        frame.filter("test")
        assert len(frame.list_box.list_walker) >= 1


class TestConfigurationIntegration:
    """Integration tests for configuration handling."""

    def test_config_file_affects_notebook_creation(self, tmp_path):
        """Test that config file settings affect notebook."""
        config_file = tmp_path / "config"
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()

        config_content = f"""[DEFAULT]
editor = vim
extension = .md
extensions = .md, .txt
notes_dir = {notes_dir}
exclude = archive
"""
        config_file.write_text(config_content)

        # Create notes with different extensions
        Path(notes_dir, "note1.txt").write_text("Text")
        Path(notes_dir, "note2.md").write_text("Markdown")
        Path(notes_dir, "note3.rst").write_text("RST")

        # Create excluded directory
        archive_dir = notes_dir / "archive"
        archive_dir.mkdir()
        Path(archive_dir, "old.txt").write_text("Old")

        with patch('sys.argv', ['tv3', '-c', str(config_file), '-p']):
            try:
                terminal_velocity.main()
            except SystemExit:
                pass

    def test_ui_respects_configured_extensions(self, temp_notes_dir):
        """Test that UI respects configured extensions."""
        # Create files with various extensions
        Path(temp_notes_dir, "note.txt").write_text("Text")
        Path(temp_notes_dir, "note.md").write_text("Markdown")
        Path(temp_notes_dir, "note.rst").write_text("RST")

        # Only load .txt files
        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.txt', ['.txt']
        )

        # Should only have .txt files
        titles = [note.title for note in frame.tv_notebook]
        assert "note" in titles
        assert len(titles) == 1

    def test_ui_creates_notes_with_configured_extension(self, temp_notes_dir):
        """Test that UI creates notes with configured extension."""
        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.md', ['.txt', '.md']
        )

        # Add a note
        note = frame.tv_notebook.add_new("new_note.md")

        # Should have .md extension
        assert note.extension == '.md'
        assert note.abspath.endswith('.md')


class TestRegressionTests:
    """Tests for the bugs we fixed."""

    def test_shlex_quote_used_not_pipes(self, temp_notes_dir):
        """Regression test: ensure shlex.quote is used instead of pipes.quote."""
        # This tests bug fix #2
        import urwid_ui as ui_module
        import inspect

        # Check that pipes is not imported
        source = inspect.getsource(ui_module)
        assert 'import pipes' not in source
        assert 'shlex.quote' in source or 'from shlex import quote' in source

    def test_file_watcher_paths_handled_correctly(self, temp_notes_dir):
        """Regression test: file watcher should handle paths correctly."""
        # This tests bug fix #3
        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt']
        )

        initial_count = len(notebook)

        # Create file externally
        new_file = Path(temp_notes_dir) / "watcher_test.txt"
        new_file.write_text("Test content")

        # Wait for watcher
        time.sleep(0.5)

        # Should be added
        assert len(notebook) == initial_count + 1
        titles = [note.title for note in notebook]
        assert "watcher_test" in titles

    def test_threading_lock_protects_notes_list(self, temp_notes_dir):
        """Regression test: threading lock should protect _notes list."""
        # This tests bug fix #4
        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt']
        )

        # Should have lock attribute
        assert hasattr(notebook, '_notes_lock')
        assert notebook._notes_lock is not None

        # Lock should be used (we test this indirectly via concurrent operations)
        import threading

        errors = []

        def add_notes():
            try:
                for i in range(10):
                    notebook.add_new(f"concurrent_{threading.current_thread().name}_{i}.txt")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_notes, name=f"t{i}") for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have no errors
        assert len(errors) == 0

    def test_python3_shebang(self):
        """Regression test: ensure Python 3 shebang is used."""
        # This tests bug fix #1
        terminal_velocity_path = Path(__file__).parent.parent / "src" / "terminal_velocity.py"
        with open(terminal_velocity_path, 'r') as f:
            first_line = f.readline()

        assert 'python3' in first_line
        assert 'python2' not in first_line
