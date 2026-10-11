"""Integration tests: the notebook, the UI and a real editor working together."""

import os
import threading

import pytest

from tests.ui_helpers import (
    highlighted,
    list_lines,
    press,
    run_headless,
    search_line,
    status_line,
    type_text,
)
from tv3 import tv_notebook


class TestEndToEndWorkflow:
    def test_create_edit_and_find_note_through_ui(
        self, make_frame, stub_editor, notes_dir, monkeypatch
    ):
        monkeypatch.setenv("STUB_EDITOR_APPEND", "remember the milk")
        frame = make_frame({"older.txt": "something else"})

        type_text(frame, "shopping")
        press(frame, "enter")
        assert (notes_dir / "shopping.txt").read_text() == "remember the milk"
        assert stub_editor.calls() == [[str(notes_dir / "shopping.txt")]]

        press(frame, "esc", "esc")
        assert list_lines(frame) == ["shopping", "older"]

        type_text(frame, "milk")
        assert list_lines(frame) == ["shopping"]

    def test_search_and_clear(self, make_frame, populated_notes_dir):
        frame = make_frame()
        assert list_lines(frame) == ["todo", "meeting_notes", "second_note", "first_note"]

        type_text(frame, "first")
        assert list_lines(frame) == ["first_note"]
        assert search_line(frame) == "first_note"

        press(frame, "esc", "esc")
        assert list_lines(frame) == ["todo", "meeting_notes", "second_note", "first_note"]

    def test_nested_notes(self, make_frame, nested_notes_dir, stub_editor, notes_dir):
        frame = make_frame()
        assert sorted(note.title for note in frame.tv_notebook) == [
            "archive/old_note",
            "personal/journal",
            "root_note",
            "work/project_a",
            "work/project_b",
        ]

        type_text(frame, "project")
        assert list_lines(frame) == ["work/project_b", "work/project_a"]
        assert highlighted(frame) == []

        press(frame, "esc")
        type_text(frame, "work/new_project.md")
        press(frame, "enter")
        assert (notes_dir / "work" / "new_project.md").is_file()
        assert stub_editor.calls() == [[str(notes_dir / "work" / "new_project.md")]]

    def test_only_configured_extensions_are_listed(self, make_frame, write_notes, notes_dir):
        write_notes(notes_dir, {"note1.txt": "", "note2.md": "", "note3.rst": ""})
        frame = make_frame()
        assert list_lines(frame) == ["note2", "note1"]

    def test_excluded_directories_are_not_listed(self, notebook, notes_dir):
        for directory in ("active", "archive", "backup"):
            (notes_dir / directory).mkdir()
            (notes_dir / directory / "note.txt").write_text("")

        nb = notebook(notes_dir, exclude=["archive", "backup"])

        assert [note.title for note in nb] == ["active/note"]

    def test_new_notes_use_configured_extension(self, make_frame, notes_dir):
        frame = make_frame(extension=".md", extensions=[".txt", ".md"])
        type_text(frame, "new_note")
        press(frame, "enter")
        assert sorted(os.listdir(notes_dir)) == ["new_note.md"]


class TestWatcherIntegration:
    @pytest.mark.slow
    def test_external_notes_appear_in_running_ui(self, make_frame, notes_dir):
        frame = make_frame({"initial.txt": ""})
        expected = {"initial", *(f"note_{i}" for i in range(5))}

        def create(loop, _data):
            for i in range(5):
                (notes_dir / f"note_{i}.txt").write_text(f"Note {i}")

        frame.loop.set_alarm_in(0, create)
        assert run_headless(frame.loop, lambda: set(list_lines(frame)) == expected)

    @pytest.mark.slow
    def test_external_and_api_notes_are_both_tracked(self, notebook, notes_dir, wait_until):
        nb = notebook(notes_dir)
        nb.create("api_note_1")
        nb.create("api_note_2")
        (notes_dir / "external_1.txt").write_text("External 1")
        (notes_dir / "external_2.txt").write_text("External 2")

        wait_until(lambda: len(nb) == 4)
        assert sorted(note.title for note in nb.search("")) == [
            "api_note_1",
            "api_note_2",
            "external_1",
            "external_2",
        ]

    def test_concurrent_creates(self, notebook, notes_dir):
        nb = notebook(notes_dir)
        errors = []

        def create_notes():
            try:
                for i in range(10):
                    nb.create(f"concurrent_{threading.current_thread().name}_{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_notes, name=f"t{i}") for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(nb) == 30
        assert len(os.listdir(notes_dir)) == 30


class TestErrorRecovery:
    def test_invalid_title_then_valid_title(self, notebook, notes_dir):
        nb = notebook(notes_dir)
        with pytest.raises(tv_notebook.InvalidNoteTitleError):
            nb.create("/absolute")

        note = nb.create("valid_note")

        assert nb.search("valid") == [note]
        assert os.listdir(notes_dir) == ["valid_note.txt"]

    def test_duplicate_title_reports_existing_note(self, notebook, notes_dir):
        nb = notebook(notes_dir)
        first = nb.create("duplicate")

        with pytest.raises(tv_notebook.NoteAlreadyExistsError) as excinfo:
            nb.create("duplicate")

        assert excinfo.value.note == first
        assert nb.search("") == [first]

    def test_ui_keeps_working_after_editor_failure(
        self, make_frame, stub_editor, notes_dir, monkeypatch
    ):
        monkeypatch.setenv("STUB_EDITOR_EXIT", "1")
        frame = make_frame({"test.txt": "Test", "other.txt": ""})
        type_text(frame, "test")
        press(frame, "enter")
        assert status_line(frame) == "Editor exited with status 1"
        assert highlighted(frame) == ["test"]

        press(frame, "esc", "esc")
        type_text(frame, "oth")
        assert list_lines(frame) == ["other"]
        assert search_line(frame) == "other"
