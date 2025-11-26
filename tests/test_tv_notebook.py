"""Tests for tv_notebook.py - note storage and search functionality."""

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import tv_notebook


class TestPlainTextNote:
    """Tests for the PlainTextNote class."""

    def test_note_creation(self, temp_notes_dir):
        """Test creating a new note."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])
        note = tv_notebook.PlainTextNote("test_note", notebook, ".txt")

        assert note.title == "test_note"
        assert note.extension == ".txt"
        assert note.abspath == os.path.join(temp_notes_dir, "test_note.txt")

    def test_note_creates_nested_directory(self, temp_notes_dir):
        """Test that notes create nested directories if needed."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])
        note = tv_notebook.PlainTextNote("subdir/note", notebook, ".txt")

        assert os.path.exists(os.path.join(temp_notes_dir, "subdir"))
        assert note.title == "subdir/note"

    def test_note_contents(self, temp_notes_dir):
        """Test reading note contents."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        # Create a note with content
        note_path = os.path.join(temp_notes_dir, "content_test.txt")
        content = "This is test content\nWith multiple lines"
        with open(note_path, "w") as f:
            f.write(content)

        note = tv_notebook.PlainTextNote("content_test", notebook, ".txt")
        assert note.contents == content

    def test_note_contents_utf8(self, temp_notes_dir):
        """Test reading note with UTF-8 characters."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        note_path = os.path.join(temp_notes_dir, "unicode_test.txt")
        content = "Unicode test: 日本語 中文 🎉"
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(content)

        note = tv_notebook.PlainTextNote("unicode_test", notebook, ".txt")
        assert note.contents == content

    def test_note_mtime(self, temp_notes_dir):
        """Test getting note modification time."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        note_path = os.path.join(temp_notes_dir, "mtime_test.txt")
        with open(note_path, "w") as f:
            f.write("test")

        note = tv_notebook.PlainTextNote("mtime_test", notebook, ".txt")
        assert note.mtime > 0
        assert isinstance(note.mtime, float)

    def test_note_equality(self, temp_notes_dir):
        """Test note equality comparison."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        note1 = tv_notebook.PlainTextNote("same_note", notebook, ".txt")
        note2 = tv_notebook.PlainTextNote("same_note", notebook, ".txt")
        note3 = tv_notebook.PlainTextNote("different_note", notebook, ".txt")

        assert note1 == note2
        assert note1 != note3


class TestPlainTextNoteBook:
    """Tests for the PlainTextNoteBook class."""

    def test_notebook_creation(self, temp_notes_dir):
        """Test creating a new notebook."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt", ".md"])

        assert notebook.path == temp_notes_dir
        assert notebook.extension == ".txt"
        assert ".txt" in notebook.extensions
        assert ".md" in notebook.extensions

    def test_notebook_creates_directory(self, tmp_path):
        """Test that notebook creates its directory if it doesn't exist."""
        nonexistent_dir = tmp_path / "new_notes_dir"
        assert not nonexistent_dir.exists()

        notebook = tv_notebook.PlainTextNoteBook(str(nonexistent_dir), ".txt", [".txt"])

        assert nonexistent_dir.exists()
        assert os.path.isdir(nonexistent_dir)

    def test_notebook_loads_existing_notes(self, populated_notes_dir):
        """Test that notebook loads existing notes on init."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        assert len(notebook) == 4
        titles = [note.title for note in notebook]
        assert "first_note" in titles
        assert "second_note" in titles
        assert "meeting_notes" in titles
        assert "todo" in titles

    def test_notebook_respects_extensions(self, temp_notes_dir):
        """Test that notebook only loads files with specified extensions."""
        # Create notes with various extensions
        Path(temp_notes_dir, "note1.txt").write_text("content")
        Path(temp_notes_dir, "note2.md").write_text("content")
        Path(temp_notes_dir, "note3.rst").write_text("content")
        Path(temp_notes_dir, "note4.doc").write_text("content")

        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt", ".md"])

        titles = [note.title for note in notebook]
        assert "note1" in titles
        assert "note2" in titles
        assert "note3" not in titles
        assert "note4" not in titles

    def test_notebook_excludes_directories(self, temp_notes_dir):
        """Test that notebook excludes specified directories."""
        # Create excluded directories
        os.makedirs(os.path.join(temp_notes_dir, "backup"))
        os.makedirs(os.path.join(temp_notes_dir, "tmp"))
        os.makedirs(os.path.join(temp_notes_dir, "keep"))

        # Add notes in each
        Path(temp_notes_dir, "backup", "old.txt").write_text("backup")
        Path(temp_notes_dir, "tmp", "temp.txt").write_text("temp")
        Path(temp_notes_dir, "keep", "note.txt").write_text("keep")
        Path(temp_notes_dir, "root.txt").write_text("root")

        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, ".txt", [".txt"], exclude=["backup", "tmp"]
        )

        titles = [note.title for note in notebook]
        assert "root" in titles
        assert "keep/note" in titles
        assert "backup/old" not in titles
        assert "tmp/temp" not in titles

    def test_notebook_ignores_dotfiles(self, temp_notes_dir):
        """Test that notebook ignores dotfiles and backup files."""
        Path(temp_notes_dir, ".hidden.txt").write_text("hidden")
        Path(temp_notes_dir, "backup~").write_text("backup")
        Path(temp_notes_dir, "normal.txt").write_text("normal")

        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        titles = [note.title for note in notebook]
        assert "normal" in titles
        assert ".hidden" not in titles
        assert "backup~" not in titles

    def test_add_new_note(self, temp_notes_dir):
        """Test adding a new note to the notebook."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        initial_count = len(notebook)
        note = notebook.add_new("new_note.txt")

        assert note is not None
        assert note.title == "new_note"
        assert len(notebook) == initial_count + 1
        assert os.path.exists(note.abspath)

    def test_add_new_note_with_subdirectory(self, temp_notes_dir):
        """Test adding a note in a subdirectory."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        note = notebook.add_new("subdir/nested_note.txt")

        assert note.title == "subdir/nested_note"
        assert os.path.exists(note.abspath)
        assert os.path.exists(os.path.join(temp_notes_dir, "subdir"))

    def test_add_duplicate_note_raises_error(self, temp_notes_dir):
        """Test that adding a duplicate note raises an error."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        notebook.add_new("duplicate.txt")

        with pytest.raises(tv_notebook.NoteAlreadyExistsError):
            notebook.add_new("duplicate.txt")

    def test_add_invalid_note_title_raises_error(self, temp_notes_dir):
        """Test that invalid note titles raise errors."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        with pytest.raises(tv_notebook.InvalidNoteTitleError):
            notebook.add_new("/.txt")

        with pytest.raises(tv_notebook.InvalidNoteTitleError):
            notebook.add_new(".txt")

    def test_remove_note(self, populated_notes_dir):
        """Test removing a note from the notebook."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        initial_count = len(notebook)
        notebook.remove("first_note.txt")

        assert len(notebook) == initial_count - 1
        titles = [note.title for note in notebook]
        assert "first_note" not in titles

    def test_remove_nested_note(self, nested_notes_dir):
        """Test removing a note from a subdirectory."""
        notebook = tv_notebook.PlainTextNoteBook(nested_notes_dir, ".txt", [".txt", ".md"])

        initial_count = len(notebook)
        notebook.remove("project_a.md", root=os.path.join(nested_notes_dir, "work"))

        assert len(notebook) == initial_count - 1
        titles = [note.title for note in notebook]
        assert "work/project_a" not in titles

    def test_notebook_contains(self, populated_notes_dir):
        """Test the __contains__ method."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        note = list(notebook)[0]
        assert note in notebook

        other_notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])
        other_note = tv_notebook.PlainTextNote("nonexistent", other_notebook, ".txt")
        assert other_note not in notebook

    def test_notebook_iteration(self, populated_notes_dir):
        """Test iterating over notebook."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        notes = list(notebook)
        assert len(notes) == 4
        for note in notes:
            assert isinstance(note, tv_notebook.PlainTextNote)

    def test_notebook_reversed(self, populated_notes_dir):
        """Test reversed iteration."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        forward = list(notebook)
        backward = list(reversed(notebook))

        assert forward == list(reversed(backward))

    def test_notebook_getitem(self, populated_notes_dir):
        """Test accessing notes by index."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        note = notebook[0]
        assert isinstance(note, tv_notebook.PlainTextNote)

        # Test negative indexing
        last_note = notebook[-1]
        assert isinstance(last_note, tv_notebook.PlainTextNote)


class TestBruteForceSearch:
    """Tests for the brute_force_search function."""

    def test_search_empty_query(self, populated_notes_dir):
        """Test searching with an empty query returns all notes."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        results = tv_notebook.brute_force_search(notebook, "")
        assert len(results) == 4

    def test_search_by_title(self, populated_notes_dir):
        """Test searching by note title."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        results = tv_notebook.brute_force_search(notebook, "first")
        assert len(results) == 1
        assert results[0].title == "first_note"

    def test_search_by_content(self, populated_notes_dir):
        """Test searching by note content."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        results = tv_notebook.brute_force_search(notebook, "groceries")
        assert len(results) == 1
        assert results[0].title == "todo"

    def test_search_case_insensitive_lowercase_query(self, populated_notes_dir):
        """Test that lowercase queries are case-insensitive."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        results = tv_notebook.brute_force_search(notebook, "second")
        assert len(results) == 1

        results = tv_notebook.brute_force_search(notebook, "SECOND")
        assert len(results) == 0  # Case-sensitive for uppercase query

    def test_search_multiple_words(self, populated_notes_dir):
        """Test searching with multiple words."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        # Both words must match
        results = tv_notebook.brute_force_search(notebook, "meeting 2025")
        assert len(results) == 1
        assert results[0].title == "meeting_notes"

        # If one word doesn't match, no results
        results = tv_notebook.brute_force_search(notebook, "meeting xyz")
        assert len(results) == 0

    def test_search_no_results(self, populated_notes_dir):
        """Test searching with no matching notes."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        results = tv_notebook.brute_force_search(notebook, "nonexistent")
        assert len(results) == 0


class TestFileWatching:
    """Tests for file system watching functionality."""

    def test_file_watcher_detects_new_file(self, temp_notes_dir):
        """Test that new files are detected by the file watcher."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        initial_count = len(notebook)

        # Create a new file externally
        new_file = os.path.join(temp_notes_dir, "external_note.txt")
        with open(new_file, "w") as f:
            f.write("Created externally")

        # Give watchdog time to detect the change
        time.sleep(0.5)

        assert len(notebook) == initial_count + 1
        titles = [note.title for note in notebook]
        assert "external_note" in titles

    def test_file_watcher_detects_deleted_file(self, populated_notes_dir):
        """Test that deleted files are detected by the file watcher."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        initial_count = len(notebook)

        # Delete a file externally
        file_to_delete = os.path.join(populated_notes_dir, "first_note.txt")
        os.remove(file_to_delete)

        # Give watchdog time to detect the change
        time.sleep(0.5)

        assert len(notebook) == initial_count - 1
        titles = [note.title for note in notebook]
        assert "first_note" not in titles

    def test_file_watcher_handles_duplicate_creation(self, temp_notes_dir):
        """Test that file watcher handles creation of already-tracked files gracefully."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        # Add a note through the notebook
        note = notebook.add_new("test_note.txt")
        initial_count = len(notebook)

        # Try to trigger a duplicate event (this shouldn't crash)
        # The file already exists, so touching it shouldn't create a duplicate
        Path(note.abspath).touch()

        time.sleep(0.5)

        # Count should remain the same
        assert len(notebook) == initial_count


class TestThreadSafety:
    """Tests for thread-safety of the notebook."""

    def test_concurrent_additions(self, temp_notes_dir):
        """Test that concurrent note additions are thread-safe."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        errors = []
        created_notes = []

        def add_notes(start_idx, count):
            try:
                for i in range(start_idx, start_idx + count):
                    note = notebook.add_new(f"note_{i}.txt")
                    if note:
                        created_notes.append(note)
            except Exception as e:
                errors.append(e)

        # Create multiple threads adding notes concurrently
        threads = []
        for i in range(5):
            t = threading.Thread(target=add_notes, args=(i * 10, 10))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Check that no errors occurred
        assert len(errors) == 0

        # Check that all notes were added
        assert len(created_notes) == 50
        assert len(notebook) >= 50  # Might be more due to file watcher

    def test_concurrent_iteration(self, populated_notes_dir):
        """Test that iterating while modifying is safe."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        errors = []
        iteration_counts = []

        def iterate_notes():
            try:
                count = 0
                for note in notebook:
                    count += 1
                    time.sleep(0.01)
                iteration_counts.append(count)
            except Exception as e:
                errors.append(e)

        def add_notes():
            try:
                for i in range(5):
                    notebook.add_new(f"concurrent_{i}.txt")
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        # Start iteration and addition concurrently
        t1 = threading.Thread(target=iterate_notes)
        t2 = threading.Thread(target=add_notes)

        t1.start()
        time.sleep(0.005)  # Start adding shortly after iteration starts
        t2.start()

        t1.join()
        t2.join()

        # No errors should occur
        assert len(errors) == 0
        # Iteration should have completed
        assert len(iteration_counts) == 1

    def test_concurrent_search(self, populated_notes_dir):
        """Test that searching while modifying is safe."""
        notebook = tv_notebook.PlainTextNoteBook(populated_notes_dir, ".txt", [".txt", ".md"])

        errors = []

        def search_notes():
            try:
                for _ in range(10):
                    notebook.search("note")
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        def modify_notes():
            try:
                for i in range(5):
                    notebook.add_new(f"search_test_{i}.txt")
                    time.sleep(0.02)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=search_notes),
            threading.Thread(target=modify_notes),
            threading.Thread(target=search_notes),
        ]

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        assert len(errors) == 0


class TestEdgeCases:
    """Tests for edge cases and error conditions."""

    def test_empty_notebook(self, temp_notes_dir):
        """Test operations on an empty notebook."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        assert len(notebook) == 0
        assert list(notebook) == []
        assert tv_notebook.brute_force_search(notebook, "anything") == []

    def test_note_with_special_characters(self, temp_notes_dir):
        """Test notes with special characters in title."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        # These should work
        note = notebook.add_new("note-with-dashes.txt")
        assert note is not None

        note = notebook.add_new("note_with_underscores.txt")
        assert note is not None

        note = notebook.add_new("note with spaces.txt")
        assert note is not None

    def test_very_long_note_title(self, temp_notes_dir):
        """Test handling of very long note titles."""
        notebook = tv_notebook.PlainTextNoteBook(temp_notes_dir, ".txt", [".txt"])

        long_title = "a" * 200 + ".txt"
        try:
            note = notebook.add_new(long_title)
            # On some systems this might fail due to filesystem limits
            # If it succeeds, that's fine too
            if note:
                assert len(note.title) > 0
        except OSError:
            # Expected on systems with filename length limits
            pass

    def test_extension_normalization(self, temp_notes_dir):
        """Test that extensions are normalized with dots."""
        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir,
            "txt",
            ["md", "txt"],  # Without dots
        )

        assert notebook.extension == ".txt"
        assert ".txt" in notebook.extensions
        assert ".md" in notebook.extensions
