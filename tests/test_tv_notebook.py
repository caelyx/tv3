"""Tests for tv_notebook.py - note storage, the filesystem watcher and search."""

import os
import threading
from pathlib import Path

import pytest
from watchdog.events import (
    DirDeletedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from tv3 import tv_notebook
from tv3.tv_notebook import FileEventHandler, InvalidNoteTitleError, NoteAlreadyExistsError


def titles(nb):
    return sorted(note.title for note in nb)


def filesystem_is_case_insensitive(directory):
    probe = Path(directory, "CaseProbe")
    probe.write_text("")
    try:
        return Path(directory, "caseprobe").exists()
    finally:
        probe.unlink()


class ChangeRecorder:
    """An `on_change` callback that records calls and whether the lock was free."""

    def __init__(self, nb):
        self.nb = nb
        self.calls = 0
        self.lock_was_free = []

    def __call__(self):
        self.calls += 1
        acquired = self.nb._notes_lock.acquire(blocking=False)
        self.lock_was_free.append(acquired)
        if acquired:
            self.nb._notes_lock.release()


class TestPlainTextNote:
    def test_attributes(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        note = tv_notebook.PlainTextNote("test_note", nb, ".txt")

        assert note.title == "test_note"
        assert note.extension == ".txt"
        assert note.abspath == os.path.join(nb.path, "test_note.txt")

    def test_constructor_does_not_touch_filesystem(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        tv_notebook.PlainTextNote("subdir/note", nb, ".txt")

        assert os.listdir(temp_notes_dir) == []

    def test_contents(self, temp_notes_dir, unwatched_notebook):
        content = "Unicode test: 日本語 中文 🎉\nsecond line"
        Path(temp_notes_dir, "n.txt").write_text(content, encoding="utf-8")
        nb = unwatched_notebook(temp_notes_dir)

        assert tv_notebook.PlainTextNote("n", nb, ".txt").contents == content

    def test_non_utf8_contents_are_replaced_not_dropped(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "latin.txt").write_bytes("café au lait".encode("latin-1"))
        nb = unwatched_notebook(temp_notes_dir)

        assert tv_notebook.PlainTextNote("latin", nb, ".txt").contents == "caf� au lait"

    def test_contents_of_missing_file_is_empty(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)

        assert tv_notebook.PlainTextNote("missing", nb, ".txt").contents == ""

    def test_mtime(self, temp_notes_dir, unwatched_notebook):
        path = Path(temp_notes_dir, "m.txt")
        path.write_text("x")
        os.utime(path, (1234.5, 1234.5))
        nb = unwatched_notebook(temp_notes_dir)

        assert tv_notebook.PlainTextNote("m", nb, ".txt").mtime == 1234.5

    def test_equality_and_hash(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        note1 = tv_notebook.PlainTextNote("same", nb, ".txt")
        note2 = tv_notebook.PlainTextNote("same", nb, ".txt")
        other_ext = tv_notebook.PlainTextNote("same", nb, ".md")

        assert note1 == note2
        assert note1 != other_ext
        assert hash(note1) == hash(note2)
        assert {note1, note2, other_ext} == {note1, other_ext}
        assert len({note1, note2, other_ext}) == 2


class TestInitialScan:
    def test_path_and_extensions(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir, extension="txt", extensions=["md", "txt"])

        assert nb.path == os.path.realpath(temp_notes_dir)
        assert nb.extension == ".txt"
        assert sorted(nb.extensions) == [".md", ".txt"]

    def test_default_extension_is_always_recognised(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "a.rst").write_text("")
        nb = unwatched_notebook(temp_notes_dir, extension=".rst", extensions=[".txt"])

        assert ".rst" in nb.extensions
        assert titles(nb) == ["a"]

    def test_creates_missing_directory(self, tmp_path, unwatched_notebook):
        new_dir = tmp_path / "new_notes_dir"
        unwatched_notebook(new_dir)

        assert new_dir.is_dir()

    def test_loads_existing_notes(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)

        assert len(nb) == 4
        assert titles(nb) == ["first_note", "meeting_notes", "second_note", "todo"]

    def test_loads_nested_notes(self, nested_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(nested_notes_dir)

        assert titles(nb) == [
            "archive/old_note",
            "personal/journal",
            "root_note",
            "work/project_a",
            "work/project_b",
        ]

    def test_respects_extensions(self, temp_notes_dir, unwatched_notebook):
        for name in ["note1.txt", "note2.md", "note3.rst", "note4.doc", "noext"]:
            Path(temp_notes_dir, name).write_text("content")

        nb = unwatched_notebook(temp_notes_dir)

        assert titles(nb) == ["note1", "note2"]

    def test_skips_excluded_hidden_and_editor_files(self, temp_notes_dir, unwatched_notebook):
        for d in ["backup", "keep", "keep/tmp", ".git", "keep/.trash"]:
            os.makedirs(os.path.join(temp_notes_dir, d))
        for name in [
            "backup/old.txt",
            "keep/tmp/temp.txt",
            ".git/HEAD.txt",
            "keep/.trash/gone.txt",
            ".hidden.txt",
            "notes.txt~",
            ".notes.txt.swp",
            "notes.swp",
            "#notes.txt#",
            ".#notes.txt",
            "keep/note.txt",
            "root.txt",
        ]:
            Path(temp_notes_dir, name).write_text("x")

        nb = unwatched_notebook(
            temp_notes_dir, extensions=[".txt", ".swp"], exclude=["backup", "tmp"]
        )

        assert titles(nb) == ["keep/note", "root"]

    def test_scan_does_not_modify_files(self, populated_notes_dir, unwatched_notebook):
        before = {
            p: (p.stat().st_mtime, p.read_bytes()) for p in Path(populated_notes_dir).iterdir()
        }
        unwatched_notebook(populated_notes_dir)
        after = {
            p: (p.stat().st_mtime, p.read_bytes()) for p in Path(populated_notes_dir).iterdir()
        }

        assert before == after

    def test_symlinked_notebook(self, tmp_path, unwatched_notebook):
        real = tmp_path / "real"
        real.mkdir()
        (real / "a.txt").write_text("x")
        link = tmp_path / "link"
        link.symlink_to(real)

        nb = unwatched_notebook(link)

        assert nb.path == os.path.realpath(real)
        assert titles(nb) == ["a"]

    def test_symlink_escaping_notebook_is_skipped(self, tmp_path, unwatched_notebook):
        notes = tmp_path / "notes"
        notes.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        (notes / "escape.txt").symlink_to(outside)

        nb = unwatched_notebook(notes)

        assert len(nb) == 0


class TestContainerProtocol:
    def test_empty_notebook(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)

        assert len(nb) == 0
        assert list(nb) == []
        assert nb.search("anything") == []

    def test_contains(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)

        assert next(iter(nb)) in nb
        assert tv_notebook.PlainTextNote("first_note", nb, ".txt") in nb
        assert tv_notebook.PlainTextNote("first_note", nb, ".md") not in nb
        assert tv_notebook.PlainTextNote("nonexistent", nb, ".txt") not in nb
        assert "first_note" not in nb

    def test_iteration_is_a_snapshot(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)
        it = iter(nb)
        nb.create("added during iteration")

        assert len(list(it)) == 4
        assert len(nb) == 5


class TestCreate:
    def test_appends_default_extension_and_writes_empty_file(
        self, temp_notes_dir, unwatched_notebook
    ):
        nb = unwatched_notebook(temp_notes_dir)
        note = nb.create("new note")

        assert note.title == "new note"
        assert note.extension == ".txt"
        assert Path(note.abspath) == Path(nb.path, "new note.txt")
        assert Path(note.abspath).read_bytes() == b""
        assert note in nb
        assert len(nb) == 1

    @pytest.mark.parametrize(
        ("title", "expected_file"),
        [
            ("ideas.md", "ideas.md"),
            ("ideas.txt", "ideas.txt"),
            ("ideas.rst", "ideas.rst.txt"),
            ("v1.2 plan", "v1.2 plan.txt"),
        ],
    )
    def test_recognised_typed_extension_is_kept(
        self, temp_notes_dir, unwatched_notebook, title, expected_file
    ):
        nb = unwatched_notebook(temp_notes_dir)
        note = nb.create(title)

        assert os.listdir(nb.path) == [expected_file]
        assert note.abspath == os.path.join(nb.path, expected_file)

    def test_creates_subdirectories(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        note = nb.create("work/project/plan")

        assert note.title == os.path.join("work", "project", "plan")
        assert Path(nb.path, "work", "project", "plan.txt").is_file()

    def test_strips_whitespace_before_writing(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        note = nb.create("  foo \t")

        assert note.title == "foo"
        assert os.listdir(nb.path) == ["foo.txt"]
        assert os.path.isfile(note.abspath)

    @pytest.mark.parametrize(
        "title",
        [
            "",
            "   ",
            "/abs/note",
            ".hidden",
            "dir/.hidden",
            ".dir/note",
            "a//b",
            "trailing/",
            "../escape",
            "a/../../escape",
            "a/./b",
            "backup/old",
            "notes~",
            "#notes#",
            "nul\0byte",
        ],
    )
    def test_invalid_titles_write_nothing(self, temp_notes_dir, unwatched_notebook, title):
        nb = unwatched_notebook(temp_notes_dir, exclude=["backup"])

        with pytest.raises(InvalidNoteTitleError) as exc_info:
            nb.create(title)

        message = str(exc_info.value)
        assert message
        assert not message.startswith(("'", '"'))
        assert os.listdir(temp_notes_dir) == []
        assert os.listdir(os.path.dirname(temp_notes_dir)) == ["notes"]
        assert len(nb) == 0

    def test_symlinked_subdirectory_escaping_notebook_is_rejected(
        self, tmp_path, unwatched_notebook
    ):
        notes = tmp_path / "notes"
        notes.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (notes / "out").symlink_to(outside)
        nb = unwatched_notebook(notes)

        with pytest.raises(InvalidNoteTitleError):
            nb.create("out/note")

        assert list(outside.iterdir()) == []

    def test_existing_note_raises_with_note(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        first = nb.create("dup")

        with pytest.raises(NoteAlreadyExistsError) as exc_info:
            nb.create("dup")

        assert exc_info.value.note is first
        assert "dup" in str(exc_info.value)
        assert len(nb) == 1

    def test_unindexed_file_on_disk_is_registered_not_overwritten(
        self, temp_notes_dir, unwatched_notebook
    ):
        nb = unwatched_notebook(temp_notes_dir)
        Path(nb.path, "late.txt").write_text("keep me")

        with pytest.raises(NoteAlreadyExistsError) as exc_info:
            nb.create("late")

        assert exc_info.value.note.title == "late"
        assert exc_info.value.note in nb
        assert Path(nb.path, "late.txt").read_text() == "keep me"

    def test_same_title_different_extension_is_a_different_note(
        self, temp_notes_dir, unwatched_notebook
    ):
        Path(temp_notes_dir, "foo.md").write_text("")
        nb = unwatched_notebook(temp_notes_dir)

        note = nb.create("foo")

        assert note.extension == ".txt"
        assert len(nb) == 2

    def test_case_insensitive_filesystem_duplicate(self, temp_notes_dir, unwatched_notebook):
        if not filesystem_is_case_insensitive(temp_notes_dir):
            pytest.skip("needs a case-insensitive filesystem")
        Path(temp_notes_dir, "foo.txt").write_text("original")
        nb = unwatched_notebook(temp_notes_dir)

        with pytest.raises(NoteAlreadyExistsError) as exc_info:
            nb.create("FOO")

        assert exc_info.value.note.title == "foo"
        assert len(nb) == 1
        assert Path(temp_notes_dir, "foo.txt").read_text() == "original"

    def test_case_sensitive_lookup_on_case_sensitive_filesystem(
        self, temp_notes_dir, unwatched_notebook
    ):
        if filesystem_is_case_insensitive(temp_notes_dir):
            pytest.skip("needs a case-sensitive filesystem")
        Path(temp_notes_dir, "foo.txt").write_text("")
        nb = unwatched_notebook(temp_notes_dir)

        nb.create("FOO")

        assert titles(nb) == ["FOO", "foo"]

    def test_note_registered_by_watcher_first_is_the_existing_note(
        self, temp_notes_dir, unwatched_notebook
    ):
        nb = unwatched_notebook(temp_notes_dir)
        path = os.path.join(nb.path, "race.txt")
        Path(path).write_text("")
        FileEventHandler(nb).on_created(FileCreatedEvent(path))
        registered = next(iter(nb))

        with pytest.raises(NoteAlreadyExistsError) as exc_info:
            nb.create("race")

        assert exc_info.value.note is registered

    def test_concurrent_create_and_watcher(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        handler = FileEventHandler(nb)
        path = os.path.join(nb.path, "contended.txt")
        created, existing, errors = [], [], []
        start = threading.Barrier(9)

        def creator():
            start.wait()
            try:
                created.append(nb.create("contended"))
            except NoteAlreadyExistsError as e:
                existing.append(e.note)
            except Exception as e:
                errors.append(e)

        def watcher():
            start.wait()
            for _ in range(50):
                handler.on_created(FileCreatedEvent(path))

        threads = [threading.Thread(target=creator) for _ in range(8)]
        threads.append(threading.Thread(target=watcher))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(nb) == 1
        (note,) = list(nb)
        assert len(created) <= 1
        assert len(created) + len(existing) == 8
        assert all(n is note for n in created + existing)


class TestFileEventHandler:
    def test_created_registers_existing_file(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        path = Path(nb.path, "external.txt")
        path.write_text("x")

        FileEventHandler(nb).on_created(FileCreatedEvent(str(path)))

        assert titles(nb) == ["external"]

    def test_created_for_deleted_file_does_not_resurrect_it(
        self, populated_notes_dir, unwatched_notebook
    ):
        nb = unwatched_notebook(populated_notes_dir)
        handler = FileEventHandler(nb)
        path = os.path.join(nb.path, "first_note.txt")
        os.remove(path)
        handler.on_deleted(FileDeletedEvent(path))

        handler.on_created(FileCreatedEvent(path))

        assert not os.path.exists(path)
        assert "first_note" not in titles(nb)

    def test_created_for_known_note_is_idempotent_and_does_not_write(
        self, populated_notes_dir, unwatched_notebook
    ):
        nb = unwatched_notebook(populated_notes_dir)
        path = Path(nb.path, "todo.md")
        before = (path.stat().st_mtime, path.read_bytes())

        FileEventHandler(nb).on_created(FileCreatedEvent(str(path)))

        assert len(nb) == 4
        assert (path.stat().st_mtime, path.read_bytes()) == before

    @pytest.mark.parametrize(
        "relpath",
        [
            "backup/old.txt",
            "deep/backup/old.txt",
            ".git/x.txt",
            ".hidden.txt",
            "note.txt~",
            ".note.txt.swp",
            "#note.txt#",
            ".#note.txt",
            "note.rst",
        ],
    )
    def test_created_ignores_unindexable_paths(self, temp_notes_dir, unwatched_notebook, relpath):
        nb = unwatched_notebook(temp_notes_dir, exclude=["backup"])
        path = Path(nb.path, relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
        recorder = ChangeRecorder(nb)
        nb.on_change = recorder

        FileEventHandler(nb).on_created(FileCreatedEvent(str(path)))

        assert len(nb) == 0
        assert recorder.calls == 0

    def test_created_outside_notebook_is_ignored(self, tmp_path, unwatched_notebook):
        notes = tmp_path / "notes"
        nb = unwatched_notebook(notes)
        outside = tmp_path / "outside.txt"
        outside.write_text("x")

        FileEventHandler(nb).on_created(FileCreatedEvent(str(outside)))

        assert len(nb) == 0

    def test_deleted_removes_only_matching_extension(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "foo.md").write_text("")
        Path(temp_notes_dir, "foo.txt").write_text("")
        nb = unwatched_notebook(temp_notes_dir)
        path = os.path.join(nb.path, "foo.txt")
        os.remove(path)

        FileEventHandler(nb).on_deleted(FileDeletedEvent(path))

        assert [(n.title, n.extension) for n in nb] == [("foo", ".md")]

    def test_deleted_nested_note(self, nested_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(nested_notes_dir)
        path = os.path.join(nb.path, "work", "project_a.md")
        os.remove(path)

        FileEventHandler(nb).on_deleted(FileDeletedEvent(path))

        assert "work/project_a" not in titles(nb)
        assert len(nb) == 4

    def test_deleted_directory_removes_its_notes(self, nested_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(nested_notes_dir)
        work = os.path.join(nb.path, "work")
        for name in os.listdir(work):
            os.remove(os.path.join(work, name))
        os.rmdir(work)

        FileEventHandler(nb).on_deleted(DirDeletedEvent(work))

        assert titles(nb) == ["archive/old_note", "personal/journal", "root_note"]

    def test_moved_within_notebook(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)
        src = os.path.join(nb.path, "todo.md")
        dest = os.path.join(nb.path, "done.md")
        os.rename(src, dest)

        FileEventHandler(nb).on_moved(FileMovedEvent(src, dest))

        assert titles(nb) == ["done", "first_note", "meeting_notes", "second_note"]

    def test_moved_into_excluded_directory(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir, exclude=["archive"])
        os.mkdir(os.path.join(nb.path, "archive"))
        src = os.path.join(nb.path, "todo.md")
        dest = os.path.join(nb.path, "archive", "todo.md")
        os.rename(src, dest)

        FileEventHandler(nb).on_moved(FileMovedEvent(src, dest))

        assert "todo" not in titles(nb)
        assert len(nb) == 3

    def test_moved_in_from_outside(self, tmp_path, unwatched_notebook):
        nb = unwatched_notebook(tmp_path / "notes")
        src = tmp_path / "incoming.txt"
        src.write_text("x")
        dest = os.path.join(nb.path, "incoming.txt")
        os.rename(src, dest)

        FileEventHandler(nb).on_moved(FileMovedEvent(str(src), dest))

        assert titles(nb) == ["incoming"]

    def test_moved_out_of_notebook(self, tmp_path, unwatched_notebook):
        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "leaving.txt").write_text("x")
        nb = unwatched_notebook(notes)
        src = os.path.join(nb.path, "leaving.txt")
        dest = str(tmp_path / "leaving.txt")
        os.rename(src, dest)

        FileEventHandler(nb).on_moved(FileMovedEvent(src, dest))

        assert len(nb) == 0

    def test_atomic_save_keeps_note(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)
        handler = FileEventHandler(nb)
        tmp = os.path.join(nb.path, ".todo.md.tmp")
        dest = os.path.join(nb.path, "todo.md")
        Path(tmp).write_text("new contents")
        handler.on_created(FileCreatedEvent(tmp))
        os.replace(tmp, dest)

        handler.on_moved(FileMovedEvent(tmp, dest))

        assert len(nb) == 4
        assert [n.contents for n in nb if n.title == "todo"] == ["new contents"]

    def test_moved_directory(self, nested_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(nested_notes_dir)
        src = os.path.join(nb.path, "work")
        dest = os.path.join(nb.path, "jobs")
        os.rename(src, dest)

        FileEventHandler(nb).on_moved(DirMovedEvent(src, dest))

        assert titles(nb) == [
            "archive/old_note",
            "jobs/project_a",
            "jobs/project_b",
            "personal/journal",
            "root_note",
        ]

    def test_events_through_symlinked_notebook_path(self, tmp_path, unwatched_notebook):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        nb = unwatched_notebook(link)
        handler = FileEventHandler(nb)
        (real / "a.txt").write_text("x")
        (real / "b.txt").write_text("x")

        handler.on_created(FileCreatedEvent(str(real / "a.txt")))
        handler.on_created(FileCreatedEvent(str(link / "b.txt")))

        assert titles(nb) == ["a", "b"]

        os.remove(real / "a.txt")
        handler.on_deleted(FileDeletedEvent(str(link / "a.txt")))

        assert titles(nb) == ["b"]

    def test_modified_is_not_a_set_change(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)
        path = os.path.join(nb.path, "todo.md")
        Path(path).write_text("changed")

        FileEventHandler(nb).on_modified(FileModifiedEvent(path))

        assert len(nb) == 4


class TestOnChange:
    def test_default_is_none(self, temp_notes_dir, unwatched_notebook):
        assert unwatched_notebook(temp_notes_dir).on_change is None

    def test_called_without_lock_for_set_changes_and_modifications(
        self, populated_notes_dir, unwatched_notebook
    ):
        nb = unwatched_notebook(populated_notes_dir)
        recorder = ChangeRecorder(nb)
        nb.on_change = recorder
        handler = FileEventHandler(nb)

        new = os.path.join(nb.path, "new.txt")
        Path(new).write_text("")
        handler.on_created(FileCreatedEvent(new))
        assert recorder.calls == 1

        todo = os.path.join(nb.path, "todo.md")
        Path(todo).write_text("changed")
        handler.on_modified(FileModifiedEvent(todo))
        assert recorder.calls == 2

        os.remove(new)
        handler.on_deleted(FileDeletedEvent(new))
        assert recorder.calls == 3

        dest = os.path.join(nb.path, "renamed.md")
        os.rename(todo, dest)
        handler.on_moved(FileMovedEvent(todo, dest))
        assert recorder.calls == 4

        assert recorder.lock_was_free == [True] * 4

    def test_not_called_for_events_that_change_nothing(
        self, populated_notes_dir, unwatched_notebook
    ):
        nb = unwatched_notebook(populated_notes_dir)
        recorder = ChangeRecorder(nb)
        nb.on_change = recorder
        handler = FileEventHandler(nb)
        gone = os.path.join(nb.path, "never-existed.txt")

        handler.on_created(FileCreatedEvent(os.path.join(nb.path, "todo.md")))
        handler.on_created(FileCreatedEvent(gone))
        handler.on_deleted(FileDeletedEvent(gone))
        handler.on_modified(FileModifiedEvent(gone))

        assert recorder.calls == 0

    def test_handler_survives_errors(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        calls = []

        def broken_callback():
            calls.append(1)
            raise RuntimeError("boom")

        nb.on_change = broken_callback
        path = os.path.join(nb.path, "a.txt")
        Path(path).write_text("")

        FileEventHandler(nb).on_created(FileCreatedEvent(path))

        assert calls == [1]
        assert titles(nb) == ["a"]


class TestBruteForceSearch:
    def test_empty_query_returns_all_notes(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)

        assert len(tv_notebook.brute_force_search(nb, "")) == 4

    def test_search_by_title(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)

        assert [n.title for n in nb.search("first")] == ["first_note"]

    def test_search_by_content(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)

        assert [n.title for n in nb.search("groceries")] == ["todo"]

    def test_lowercase_query_is_case_insensitive(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)

        assert [n.title for n in nb.search("discussed")] == ["meeting_notes"]
        assert nb.search("SECOND") == []
        assert [n.title for n in nb.search("Second")] == ["second_note"]

    def test_all_words_must_match(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)

        assert [n.title for n in nb.search("meeting 2025")] == ["meeting_notes"]
        assert nb.search("meeting xyz") == []


@pytest.mark.slow
class TestRealWatcher:
    """End-to-end tests through the real watchdog observer."""

    def test_detects_new_file(self, temp_notes_dir, notebook, wait_until):
        nb = notebook(temp_notes_dir)
        changed = threading.Event()
        nb.on_change = changed.set

        Path(nb.path, "external_note.txt").write_text("Created externally")

        wait_until(lambda: titles(nb) == ["external_note"])
        assert changed.wait(5)

    def test_detects_deleted_file(self, populated_notes_dir, notebook, wait_until):
        nb = notebook(populated_notes_dir)
        path = os.path.join(nb.path, "first_note.txt")

        os.remove(path)
        wait_until(lambda: "first_note" not in titles(nb))
        Path(nb.path, "sentinel.txt").write_text("")
        wait_until(lambda: "sentinel" in titles(nb))

        assert "first_note" not in titles(nb)
        assert len(nb) == 4
        assert not os.path.lexists(path)

    def test_detects_rename(self, populated_notes_dir, notebook, wait_until):
        nb = notebook(populated_notes_dir)

        os.rename(os.path.join(nb.path, "todo.md"), os.path.join(nb.path, "done.md"))

        wait_until(lambda: titles(nb) == ["done", "first_note", "meeting_notes", "second_note"])

    def test_symlinked_notebook(self, tmp_path, notebook, wait_until):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        nb = notebook(link)

        (link / "first.txt").write_text("x")
        wait_until(lambda: titles(nb) == ["first"])

        (link / "second.txt").write_text("x")
        wait_until(lambda: titles(nb) == ["first", "second"])
