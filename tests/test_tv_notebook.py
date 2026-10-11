"""Tests for tv_notebook.py - note storage, the filesystem watcher and search."""

import logging
import os
import random
import shutil
import threading
import tracemalloc
import unicodedata
from pathlib import Path
from typing import ClassVar

import pytest
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from tests.conftest import BASE_MTIME
from tv3 import tv_notebook
from tv3.ranking import SNIPPET_WIDTH, fold, fuzzy_match, fuzzy_needle, prefix_length
from tv3.tv_notebook import (
    DeleteNoteError,
    FileEventHandler,
    InvalidNoteTitleError,
    NewNoteError,
    NoteAlreadyExistsError,
)

NFC_CAFE = unicodedata.normalize("NFC", "café")
NFD_CAFE = unicodedata.normalize("NFD", "café")


def titles(nb):
    return sorted(note.title for note in nb)


def filesystem_is_case_insensitive(directory):
    probe = Path(directory, "CaseProbe")
    probe.write_text("")
    try:
        return Path(directory, "caseprobe").exists()
    finally:
        probe.unlink()


def filesystem_is_normalization_insensitive(directory):
    probe = Path(directory, NFD_CAFE)
    probe.write_text("")
    try:
        return Path(directory, NFC_CAFE).exists()
    finally:
        probe.unlink()


def is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.fixture
def read_only():
    """Make directories read-only for the test, restoring write access at teardown."""
    paths = []

    def make(path):
        os.chmod(path, 0o555)
        paths.append(path)

    yield make
    for path in paths:
        os.chmod(path, 0o755)


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


class HookedNoteBook(tv_notebook.PlainTextNoteBook):
    """A real notebook whose reads can be held back or interleaved with other work.

    The background read waits for `warmer_gate` to be set or, if it is None,
    for the notebook to be closed; `warmer_finished` is set when it ends.
    `before_next_read`, if set, is called once, just before the next note
    file is opened. `during_next_read`, if set, is called once, after the
    next note file has been read but before the read is stored.
    `after_next_check`, if set, is called once, after the notebook next
    checks whether a path is a note file to index. `before_next_write`, if set, is
    called once, after `create` or `rename` has checked the target path but
    before it writes, as another process could.
    """

    def __init__(self, *args, warmer_gate=None, **kwargs):
        self.warmer_gate = warmer_gate
        self.warmer_finished = threading.Event()
        self.before_next_read = None
        self.during_next_read = None
        self.after_next_check = None
        self.before_next_write = None
        super().__init__(*args, **kwargs)

    def _in_new_parents(self, abspath, make, failure):
        action, self.before_next_write = self.before_next_write, None
        if action is not None:
            action()
        super()._in_new_parents(abspath, make, failure)

    def _indexable(self, abspath, real_parent=None):
        indexable = super()._indexable(abspath, real_parent)
        action, self.after_next_check = self.after_next_check, None
        if action is not None:
            action()
        return indexable

    def _read_all(self):
        gate = self._closed if self.warmer_gate is None else self.warmer_gate
        gate.wait(timeout=10)
        super()._read_all()
        self.warmer_finished.set()

    def _read(self, abspath, title):
        action, self.before_next_read = self.before_next_read, None
        if action is not None:
            action()
        contents = super()._read(abspath, title)
        action, self.during_next_read = self.during_next_read, None
        if action is not None:
            action()
        return contents


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

    def test_mtime_of_missing_file_is_zero_and_logged(
        self, temp_notes_dir, unwatched_notebook, caplog
    ):
        nb = unwatched_notebook(temp_notes_dir)

        with caplog.at_level(logging.ERROR, logger="tv3"):
            assert tv_notebook.PlainTextNote("missing", nb, ".txt").mtime == 0.0

        assert any("Error getting mtime" in r.message for r in caplog.records)

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

    def test_note_at(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)
        note = next(n for n in nb if n.title == "first_note")

        assert nb.note_at(note.abspath) is note
        assert nb.note_at(os.path.join(nb.path, "first_note.md")) is None
        assert nb.note_at(os.path.join(nb.path, "nonexistent.txt")) is None
        assert nb.note_at(os.path.join(os.path.dirname(nb.path), "first_note.txt")) is None

    def test_note_at_gives_the_note_that_replaced_another(
        self, populated_notes_dir, unwatched_notebook
    ):
        nb = unwatched_notebook(populated_notes_dir)
        note = next(n for n in nb if n.title == "first_note")
        handler = FileEventHandler(nb)
        os.remove(note.abspath)
        handler.on_deleted(FileDeletedEvent(note.abspath))
        Path(note.abspath).write_text("again")
        handler.on_created(FileCreatedEvent(note.abspath))

        current = nb.note_at(note.abspath)

        assert current is not None and current is not note
        assert current.title == "first_note"

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

    @pytest.mark.parametrize("title", ["foo/", "work/project/", "foo/ "])
    def test_trailing_separator_is_reported_as_such(
        self, temp_notes_dir, unwatched_notebook, title
    ):
        nb = unwatched_notebook(temp_notes_dir)

        with pytest.raises(InvalidNoteTitleError, match="ends with a path separator"):
            nb.create(title)

        assert os.listdir(temp_notes_dir) == []

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


def rename_events(src, dest):
    """Every watchdog event a watcher might report for renaming `src` to `dest`."""
    return [
        ("on_moved", FileMovedEvent(src, dest)),
        ("on_deleted", FileDeletedEvent(src)),
        ("on_created", FileCreatedEvent(dest)),
        ("on_modified", FileModifiedEvent(dest)),
        ("on_created", DirCreatedEvent(os.path.dirname(dest))),
        ("on_modified", DirModifiedEvent(os.path.dirname(src))),
    ]


def deliver(nb, events):
    handler = FileEventHandler(nb)
    for method, event in events:
        getattr(handler, method)(event)


class TestRename:
    def test_moves_file_and_updates_index(self, temp_notes_dir, write_notes, unwatched_notebook):
        write_notes(temp_notes_dir, {"old.txt": "remember the milk", "other.txt": ""})
        nb = unwatched_notebook(temp_notes_dir)
        old = next(n for n in nb if n.title == "old")

        new = nb.rename(old, "new")

        assert new.title == "new"
        assert new.extension == ".txt"
        assert sorted(os.listdir(nb.path)) == ["new.txt", "other.txt"]
        assert Path(new.abspath).read_text() == "remember the milk"
        assert titles(nb) == ["new", "other"]
        assert new in nb
        assert old not in nb
        assert new.mtime == BASE_MTIME
        assert [n.title for n in nb.search("milk")] == ["new"]
        assert [n.title for n in nb.search("old")] == []

    @pytest.mark.parametrize(
        ("old_file", "new_title", "new_file"),
        [
            ("a.txt", "b", "b.txt"),
            ("a.md", "b", "b.md"),
            ("a.md", "b.txt", "b.txt"),
            ("a.txt", "b.md", "b.md"),
            ("a.md", "b.rst", "b.rst.md"),
            ("a.md", "v1.2 plan", "v1.2 plan.md"),
            ("a.txt", "  b \t", "b.txt"),
        ],
    )
    def test_keeps_extension_unless_title_has_a_recognised_one(
        self, temp_notes_dir, unwatched_notebook, old_file, new_title, new_file
    ):
        Path(temp_notes_dir, old_file).write_text("x")
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)

        new = nb.rename(note, new_title)

        assert os.listdir(nb.path) == [new_file]
        assert new.abspath == os.path.join(nb.path, new_file)
        assert [(n.title, n.extension) for n in nb] == [(new.title, new.extension)]

    def test_into_new_subdirectory(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "plan.txt").write_text("x")
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)

        new = nb.rename(note, "work/2026/plan")

        assert new.title == os.path.join("work", "2026", "plan")
        assert Path(nb.path, "work", "2026", "plan.txt").read_text() == "x"
        assert not Path(nb.path, "plan.txt").exists()
        assert titles(nb) == ["work/2026/plan"]

    def test_out_of_subdirectory(self, nested_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(nested_notes_dir)
        note = next(n for n in nb if n.title == "work/project_a")

        new = nb.rename(note, "project_a")

        assert new.title == "project_a"
        assert Path(nb.path, "project_a.md").read_text() == "Project A details"
        assert sorted(os.listdir(Path(nb.path, "work"))) == ["project_b.md"]
        assert "work/project_a" not in titles(nb)

    def test_same_title_changes_nothing(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)
        note = next(n for n in nb if n.title == "todo")
        before = sorted(os.listdir(nb.path))

        assert nb.rename(note, " todo ") is note
        assert sorted(os.listdir(nb.path)) == before
        assert note in nb

    @pytest.mark.parametrize(
        "title",
        [
            "",
            "   ",
            "/abs/note",
            ".hidden",
            "dir/.hidden",
            "a//b",
            "trailing/",
            "../escape",
            "a/../../escape",
            "backup/old",
            "notes~",
            "#notes#",
            "nul\0byte",
        ],
    )
    def test_invalid_titles_change_nothing(self, temp_notes_dir, unwatched_notebook, title):
        Path(temp_notes_dir, "a.txt").write_text("x")
        nb = unwatched_notebook(temp_notes_dir, exclude=["backup"])
        (note,) = list(nb)

        with pytest.raises(InvalidNoteTitleError) as exc_info:
            nb.rename(note, title)

        assert str(exc_info.value)
        assert os.listdir(temp_notes_dir) == ["a.txt"]
        assert os.listdir(os.path.dirname(temp_notes_dir)) == ["notes"]
        assert list(nb) == [note]

    @pytest.mark.parametrize("target", ["b", "sub/b"])
    def test_onto_existing_note_fails_and_changes_nothing(
        self, temp_notes_dir, write_notes, unwatched_notebook, target
    ):
        os.mkdir(Path(temp_notes_dir, "sub"))
        write_notes(temp_notes_dir, {"a.txt": "aaa", f"{target}.txt": "bbb"})
        nb = unwatched_notebook(temp_notes_dir)
        a = next(n for n in nb if n.title == "a")
        b = next(n for n in nb if n.title == target)
        recorder = ChangeRecorder(nb)
        nb.on_change = recorder

        with pytest.raises(NoteAlreadyExistsError) as exc_info:
            nb.rename(a, target)

        assert exc_info.value.note is b
        assert target in str(exc_info.value)
        assert Path(a.abspath).read_text() == "aaa"
        assert Path(b.abspath).read_text() == "bbb"
        assert sorted(nb, key=lambda n: n.title) == [a, b]
        assert recorder.calls == 0

    def test_onto_unindexed_file_fails_and_registers_it(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "a.txt").write_text("aaa")
        nb = unwatched_notebook(temp_notes_dir)
        (a,) = list(nb)
        Path(nb.path, "late.txt").write_text("keep me")

        with pytest.raises(NoteAlreadyExistsError) as exc_info:
            nb.rename(a, "late")

        assert exc_info.value.note.title == "late"
        assert titles(nb) == ["a", "late"]
        assert Path(nb.path, "late.txt").read_text() == "keep me"
        assert Path(a.abspath).read_text() == "aaa"

    def test_onto_directory_fails(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "a.txt").write_text("aaa")
        os.mkdir(Path(temp_notes_dir, "b.txt"))
        nb = unwatched_notebook(temp_notes_dir)
        (a,) = list(nb)

        with pytest.raises(InvalidNoteTitleError):
            nb.rename(a, "b")

        assert Path(a.abspath).read_text() == "aaa"
        assert Path(temp_notes_dir, "b.txt").is_dir()

    def test_to_other_extension_of_same_title(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "a.txt").write_text("txt")
        Path(temp_notes_dir, "a.md").write_text("md")
        nb = unwatched_notebook(temp_notes_dir)
        txt = next(n for n in nb if n.extension == ".txt")

        with pytest.raises(NoteAlreadyExistsError):
            nb.rename(txt, "a.md")

        new = nb.rename(txt, "b")
        assert [(n.title, n.extension) for n in sorted(nb, key=lambda n: n.title)] == [
            ("a", ".md"),
            ("b", ".txt"),
        ]
        assert Path(new.abspath).read_text() == "txt"

    def test_case_only_on_case_insensitive_filesystem(self, temp_notes_dir, unwatched_notebook):
        if not filesystem_is_case_insensitive(temp_notes_dir):
            pytest.skip("needs a case-insensitive filesystem")
        Path(temp_notes_dir, "foo.txt").write_text("x")
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)

        new = nb.rename(note, "Foo")

        assert new.title == "Foo"
        assert os.listdir(nb.path) == ["Foo.txt"]
        assert list(nb) == [new]
        assert Path(new.abspath).read_text() == "x"

    def test_case_only_on_case_sensitive_filesystem(self, temp_notes_dir, unwatched_notebook):
        if filesystem_is_case_insensitive(temp_notes_dir):
            pytest.skip("needs a case-sensitive filesystem")
        Path(temp_notes_dir, "foo.txt").write_text("x")
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)

        new = nb.rename(note, "Foo")

        assert os.listdir(nb.path) == ["Foo.txt"]
        assert list(nb) == [new]

    def test_missing_source_fails_and_leaves_no_directories(
        self, temp_notes_dir, unwatched_notebook
    ):
        Path(temp_notes_dir, "a.txt").write_text("x")
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)
        os.remove(note.abspath)

        with pytest.raises(NewNoteError, match="Could not rename"):
            nb.rename(note, "new/dir/b")

        assert os.listdir(nb.path) == []

    @pytest.mark.skipif(is_root(), reason="root ignores directory permissions")
    def test_permission_error(self, temp_notes_dir, unwatched_notebook, read_only):
        Path(temp_notes_dir, "a.txt").write_text("x")
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)
        read_only(nb.path)

        with pytest.raises(NewNoteError, match="Permission denied") as exc_info:
            nb.rename(note, "b")

        assert not isinstance(exc_info.value, InvalidNoteTitleError)
        assert os.listdir(nb.path) == ["a.txt"]
        assert list(nb) == [note]

    @pytest.mark.skipif(is_root(), reason="root ignores directory permissions")
    def test_permission_error_in_new_subdirectory_removes_it(
        self, temp_notes_dir, unwatched_notebook, read_only
    ):
        Path(temp_notes_dir, "a.txt").write_text("x")
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)
        os.mkdir(Path(nb.path, "locked"))
        read_only(Path(nb.path, "locked"))

        with pytest.raises(NewNoteError):
            nb.rename(note, "locked/sub/b")

        assert sorted(os.listdir(nb.path)) == ["a.txt", "locked"]
        assert os.listdir(Path(nb.path, "locked")) == []
        assert list(nb) == [note]

    @pytest.mark.skipif(is_root(), reason="root ignores directory permissions")
    def test_failed_move_removes_directories_it_created(
        self, temp_notes_dir, unwatched_notebook, read_only
    ):
        os.mkdir(Path(temp_notes_dir, "locked"))
        Path(temp_notes_dir, "locked", "a.txt").write_text("x")
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)
        read_only(Path(nb.path, "locked"))

        with pytest.raises(NewNoteError, match="Permission denied"):
            nb.rename(note, "new/dir/a")

        assert os.listdir(nb.path) == ["locked"]
        assert os.listdir(Path(nb.path, "locked")) == ["a.txt"]
        assert list(nb) == [note]

    @pytest.mark.parametrize(
        ("old_file", "new_title"),
        [("a.txt", "b"), ("a.txt", "sub/dir/b"), ("sub/a.txt", "a"), ("a.txt", "a.md")],
    )
    def test_watcher_events_afterwards_change_nothing(
        self, temp_notes_dir, write_notes, unwatched_notebook, old_file, new_title
    ):
        os.makedirs(Path(temp_notes_dir, "sub"))
        write_notes(temp_notes_dir, {old_file: "x", "other.txt": "y"})
        nb = unwatched_notebook(temp_notes_dir)
        note = next(n for n in nb if n.abspath.endswith(old_file))
        new = nb.rename(note, new_title)
        before = {n.abspath: n for n in nb}
        recorder = ChangeRecorder(nb)
        nb.on_change = recorder

        deliver(nb, rename_events(note.abspath, new.abspath))

        assert {n.abspath: n for n in nb} == before
        assert all(n is before[n.abspath] for n in nb)
        assert recorder.calls == 0

    def test_watcher_events_after_case_only_rename_change_nothing(
        self, temp_notes_dir, unwatched_notebook
    ):
        if not filesystem_is_case_insensitive(temp_notes_dir):
            pytest.skip("needs a case-insensitive filesystem")
        Path(temp_notes_dir, "foo.txt").write_text("x")
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)
        new = nb.rename(note, "Foo")
        recorder = ChangeRecorder(nb)
        nb.on_change = recorder

        deliver(nb, rename_events(note.abspath, new.abspath))

        assert list(nb) == [new]
        assert next(iter(nb)) is new
        assert new.title == "Foo"
        assert recorder.calls == 0

    def test_external_case_only_rename_is_still_seen(self, temp_notes_dir, unwatched_notebook):
        if not filesystem_is_case_insensitive(temp_notes_dir):
            pytest.skip("needs a case-insensitive filesystem")
        Path(temp_notes_dir, "foo.txt").write_text("x")
        nb = unwatched_notebook(temp_notes_dir)
        src = os.path.join(nb.path, "foo.txt")
        dest = os.path.join(nb.path, "Foo.txt")
        os.rename(src, dest)

        deliver(nb, [("on_moved", FileMovedEvent(src, dest))])

        assert titles(nb) == ["Foo"]

    @pytest.mark.parametrize(
        ("make_target", "error"),
        [
            (lambda path: path.write_text("EXTERNAL"), NoteAlreadyExistsError),
            (lambda path: path.mkdir(), InvalidNoteTitleError),
        ],
    )
    def test_target_created_during_rename_is_not_overwritten(
        self, temp_notes_dir, unwatched_notebook, make_target, error
    ):
        Path(temp_notes_dir, "a.txt").write_text("A")
        nb = unwatched_notebook(temp_notes_dir, cls=HookedNoteBook)
        (a,) = list(nb)
        target = Path(nb.path, "b.txt")
        nb.before_next_write = lambda: make_target(target)

        with pytest.raises(error):
            nb.rename(a, "b")

        assert Path(a.abspath).read_text() == "A"
        assert sorted(os.listdir(nb.path)) == ["a.txt", "b.txt"]
        assert a in nb
        if error is NoteAlreadyExistsError:
            assert target.read_text() == "EXTERNAL"
            assert titles(nb) == ["a", "b"]
        else:
            assert target.is_dir()
            assert titles(nb) == ["a"]

    def test_target_created_during_rename_into_new_directory_removes_nothing_of_it(
        self, temp_notes_dir, unwatched_notebook
    ):
        Path(temp_notes_dir, "a.txt").write_text("A")
        nb = unwatched_notebook(temp_notes_dir, cls=HookedNoteBook)
        (a,) = list(nb)

        def external_write():
            os.mkdir(Path(nb.path, "sub"))
            Path(nb.path, "sub", "b.txt").write_text("EXTERNAL")

        nb.before_next_write = external_write

        with pytest.raises(NoteAlreadyExistsError) as exc_info:
            nb.rename(a, "sub/b")

        assert exc_info.value.note.title == os.path.join("sub", "b")
        assert Path(nb.path, "sub", "b.txt").read_text() == "EXTERNAL"
        assert Path(a.abspath).read_text() == "A"

    def test_stale_note_is_not_renamed(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "a.txt").write_text("A")
        nb = unwatched_notebook(temp_notes_dir)
        (a,) = list(nb)
        b = nb.rename(a, "b")
        new_a = nb.create("a")
        Path(new_a.abspath).write_text("NEW A")

        with pytest.raises(NewNoteError, match=r"^'a' changed on disk; try again$"):
            nb.rename(a, "c")
        nb.rename(b, "c")
        with pytest.raises(NewNoteError, match=r"^'b' changed on disk; try again$"):
            nb.rename(b, "d")

        assert sorted(os.listdir(nb.path)) == ["a.txt", "c.txt"]
        assert Path(new_a.abspath).read_text() == "NEW A"
        assert titles(nb) == ["a", "c"]
        assert new_a in nb

    def test_note_not_from_the_index_is_not_renamed(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "a.txt").write_text("A")
        nb = unwatched_notebook(temp_notes_dir)
        other = tv_notebook.PlainTextNote("a", nb, ".txt")

        with pytest.raises(NewNoteError, match=r"^'a' changed on disk; try again$"):
            nb.rename(other, "b")

        assert os.listdir(nb.path) == ["a.txt"]

    def test_case_only_change_of_a_folder_is_rejected(self, temp_notes_dir, unwatched_notebook):
        if not filesystem_is_case_insensitive(temp_notes_dir):
            pytest.skip("needs a case-insensitive filesystem")
        os.mkdir(Path(temp_notes_dir, "sub"))
        Path(temp_notes_dir, "sub", "n.txt").write_text("x")
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)

        for title in ["Sub/n", "SUB/N"]:
            with pytest.raises(InvalidNoteTitleError, match="letter case of a folder name"):
                nb.rename(note, title)

        assert os.listdir(nb.path) == ["sub"]
        assert list(nb) == [note]
        renamed = nb.rename(note, "sub/N")
        assert renamed.title == os.path.join("sub", "N")
        assert os.listdir(Path(nb.path, "sub")) == ["N.txt"]


class TestDelete:
    def test_removes_file_and_note(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)
        note = next(n for n in nb if n.title == "todo")

        nb.delete(note)

        assert not os.path.lexists(note.abspath)
        assert note not in nb
        assert titles(nb) == ["first_note", "meeting_notes", "second_note"]
        assert nb.search("groceries") == []

    def test_only_that_extension(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "foo.md").write_text("")
        Path(temp_notes_dir, "foo.txt").write_text("")
        nb = unwatched_notebook(temp_notes_dir)
        txt = next(n for n in nb if n.extension == ".txt")

        nb.delete(txt)

        assert os.listdir(nb.path) == ["foo.md"]
        assert [(n.title, n.extension) for n in nb] == [("foo", ".md")]

    def test_nested_note_keeps_directory(self, nested_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(nested_notes_dir)
        note = next(n for n in nb if n.title == "personal/journal")

        nb.delete(note)

        assert os.listdir(Path(nb.path, "personal")) == []
        assert "personal/journal" not in titles(nb)

    def test_already_deleted_file(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)
        note = next(n for n in nb if n.title == "todo")
        os.remove(note.abspath)

        nb.delete(note)

        assert note not in nb
        assert len(nb) == 3

    def test_twice(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)
        note = next(n for n in nb if n.title == "todo")
        nb.delete(note)

        nb.delete(note)

        assert len(nb) == 3

    @pytest.mark.skipif(is_root(), reason="root ignores directory permissions")
    def test_permission_error(self, populated_notes_dir, unwatched_notebook, read_only):
        nb = unwatched_notebook(populated_notes_dir)
        note = next(n for n in nb if n.title == "todo")
        read_only(nb.path)

        with pytest.raises(DeleteNoteError, match="Permission denied") as exc_info:
            nb.delete(note)

        assert isinstance(exc_info.value, tv_notebook.Error)
        assert "todo" in str(exc_info.value)
        assert os.path.isfile(note.abspath)
        assert note in nb
        assert len(nb) == 4

    def test_not_a_file(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "a.txt").write_text("")
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)
        os.remove(note.abspath)
        os.mkdir(note.abspath)

        with pytest.raises(DeleteNoteError):
            nb.delete(note)

        assert os.path.isdir(note.abspath)

    def test_stale_note_does_not_delete_a_newer_note(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "a.txt").write_text("A")
        nb = unwatched_notebook(temp_notes_dir)
        (a,) = list(nb)
        nb.rename(a, "b")
        new_a = nb.create("a")
        Path(new_a.abspath).write_text("NEW A")

        with pytest.raises(DeleteNoteError, match=r"^'a' changed on disk; try again$"):
            nb.delete(a)

        assert sorted(os.listdir(nb.path)) == ["a.txt", "b.txt"]
        assert Path(new_a.abspath).read_text() == "NEW A"
        assert new_a in nb

    def test_stale_note_does_not_delete_an_unindexed_file(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "a.txt").write_text("A")
        nb = unwatched_notebook(temp_notes_dir)
        (a,) = list(nb)
        nb.delete(a)
        Path(a.abspath).write_text("NEW A")

        with pytest.raises(DeleteNoteError, match=r"^'a' changed on disk; try again$"):
            nb.delete(a)

        assert Path(a.abspath).read_text() == "NEW A"

    def test_watcher_events_afterwards_change_nothing(
        self, populated_notes_dir, unwatched_notebook
    ):
        nb = unwatched_notebook(populated_notes_dir)
        note = next(n for n in nb if n.title == "todo")
        nb.delete(note)
        before = list(nb)
        recorder = ChangeRecorder(nb)
        nb.on_change = recorder

        deliver(
            nb,
            [
                ("on_deleted", FileDeletedEvent(note.abspath)),
                ("on_created", FileCreatedEvent(note.abspath)),
                ("on_modified", FileModifiedEvent(note.abspath)),
                ("on_modified", DirModifiedEvent(nb.path)),
            ],
        )

        assert list(nb) == before
        assert not os.path.lexists(note.abspath)
        assert recorder.calls == 0


class TestRenameAndDeleteWithRealWatcher:
    """The live watcher's events for these operations leave the index as they made it."""

    def settle(self, nb, wait_until):
        """Wait until the watcher has handled every event before this call."""
        sentinel = Path(nb.path, "zz-sentinel.txt")
        sentinel.write_text("")
        wait_until(lambda: "zz-sentinel" in titles(nb))
        os.remove(sentinel)
        wait_until(lambda: "zz-sentinel" not in titles(nb))

    @pytest.mark.parametrize("new_title", ["b", "sub/b", "A"])
    def test_rename(self, temp_notes_dir, notebook, wait_until, new_title):
        Path(temp_notes_dir, "a.txt").write_text("x")
        nb = notebook(temp_notes_dir)
        self.settle(nb, wait_until)
        (note,) = list(nb)

        new = nb.rename(note, new_title)
        self.settle(nb, wait_until)

        assert list(nb) == [new]
        assert next(iter(nb)) is new
        assert new.title == new_title
        assert Path(new.abspath).read_text() == "x"

    @pytest.mark.parametrize("new_title", ["b", "sub/b", "A"])
    def test_rename_never_shows_a_transient_index(
        self, temp_notes_dir, notebook, wait_until, new_title
    ):
        Path(temp_notes_dir, "a.txt").write_text("x")
        Path(temp_notes_dir, "other.txt").write_text("y")
        nb = notebook(temp_notes_dir)
        self.settle(nb, wait_until)
        note = next(n for n in nb if n.title == "a")
        snapshots = []
        nb.on_change = lambda: snapshots.append([t for t in titles(nb) if t != "zz-sentinel"])

        new = nb.rename(note, new_title)
        self.settle(nb, wait_until)

        assert all(snapshot == sorted([new_title, "other"]) for snapshot in snapshots)
        assert next(n for n in nb if n.title == new_title) is new

    def test_external_renames_back_and_forth_never_drop_the_note(
        self, temp_notes_dir, notebook, wait_until
    ):
        Path(temp_notes_dir, "a.txt").write_text("x")
        nb = notebook(temp_notes_dir)
        self.settle(nb, wait_until)
        a, b = os.path.join(nb.path, "a.txt"), os.path.join(nb.path, "b.txt")
        snapshots = []
        nb.on_change = lambda: snapshots.append([t for t in titles(nb) if t != "zz-sentinel"])

        for _ in range(10):
            os.rename(a, b)
            os.rename(b, a)
        self.settle(nb, wait_until)

        # The file always exists, as a or as b, so the index is never empty;
        # which of them it holds lags behind the renames.
        assert snapshots and all(snapshots), snapshots
        assert titles(nb) == ["a"]

    def test_delete(self, populated_notes_dir, notebook, wait_until):
        nb = notebook(populated_notes_dir)
        self.settle(nb, wait_until)
        note = next(n for n in nb if n.title == "todo")
        others = {n.title: n for n in nb if n is not note}

        nb.delete(note)
        self.settle(nb, wait_until)

        assert {n.title: n for n in nb} == others
        assert not os.path.lexists(note.abspath)


class TestFileEventHandler:
    def test_created_registers_existing_file(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        path = Path(nb.path, "external.txt")
        path.write_text("x")

        FileEventHandler(nb).on_created(FileCreatedEvent(str(path)))

        assert titles(nb) == ["external"]

    def test_file_deleted_before_a_created_event_reads_it_is_not_added(
        self, temp_notes_dir, unwatched_notebook, caplog
    ):
        nb = unwatched_notebook(temp_notes_dir, cls=HookedNoteBook)
        changes = ChangeRecorder(nb)
        nb.on_change = changes
        path = Path(nb.path, "fleeting.txt")
        path.write_text("x")
        nb.before_next_read = path.unlink

        with caplog.at_level(logging.DEBUG, logger="tv3"):
            FileEventHandler(nb).on_created(FileCreatedEvent(str(path)))

        assert titles(nb) == []
        assert changes.calls == 0
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert any("fleeting.txt" in r.message for r in caplog.records)

    @pytest.mark.parametrize("event", [FileModifiedEvent, FileCreatedEvent])
    def test_note_deleted_before_an_event_rereads_it_is_removed(
        self, temp_notes_dir, write_notes, unwatched_notebook, caplog, event
    ):
        write_notes(temp_notes_dir, {"a.txt": "x", "b.txt": "y"})
        nb = unwatched_notebook(temp_notes_dir, cls=HookedNoteBook)
        changes = ChangeRecorder(nb)
        nb.on_change = changes
        path = Path(nb.path, "a.txt")
        nb.before_next_read = path.unlink

        with caplog.at_level(logging.DEBUG, logger="tv3"):
            FileEventHandler(nb).dispatch(event(str(path)))

        assert titles(nb) == ["b"]
        assert changes.calls == 1
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_refresh_of_a_deleted_note_removes_it(
        self, temp_notes_dir, write_notes, unwatched_notebook, caplog
    ):
        write_notes(temp_notes_dir, {"a.txt": "x"})
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)
        os.remove(note.abspath)

        with caplog.at_level(logging.DEBUG, logger="tv3"):
            assert nb.refresh(note) is True

        assert note not in nb
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_move_whose_file_moves_on_between_checks_keeps_the_note(
        self, temp_notes_dir, write_notes, unwatched_notebook, caplog
    ):
        write_notes(temp_notes_dir, {"a.txt": "x"})
        nb = unwatched_notebook(temp_notes_dir, cls=HookedNoteBook)
        changes = ChangeRecorder(nb)
        nb.on_change = changes
        a, b = os.path.join(nb.path, "a.txt"), os.path.join(nb.path, "b.txt")
        os.rename(a, b)
        os.rename(b, a)
        # The event for a -> b arrives after the file moved back; it moves to
        # b again once the destination has been checked.
        nb.after_next_check = lambda: os.rename(a, b)

        with caplog.at_level(logging.DEBUG, logger="tv3"):
            FileEventHandler(nb).on_moved(FileMovedEvent(a, b))

        assert titles(nb) == ["b"]
        assert changes.calls == 1
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_move_whose_destination_is_gone_before_its_read_keeps_the_source(
        self, temp_notes_dir, write_notes, unwatched_notebook, caplog
    ):
        write_notes(temp_notes_dir, {"a.txt": "x"})
        nb = unwatched_notebook(temp_notes_dir, cls=HookedNoteBook)
        a, b = os.path.join(nb.path, "a.txt"), os.path.join(nb.path, "b.txt")
        os.rename(a, b)
        nb.before_next_read = lambda: os.rename(b, a)

        with caplog.at_level(logging.DEBUG, logger="tv3"):
            FileEventHandler(nb).on_moved(FileMovedEvent(a, b))

        assert titles(nb) == ["a"]
        assert Path(a).read_text() == "x"
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

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

    def test_moved_source_that_exists_again_is_kept(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "a.txt").write_text("x")
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)
        recorder = ChangeRecorder(nb)
        nb.on_change = recorder

        deliver(nb, [("on_moved", FileMovedEvent(note.abspath, os.path.join(nb.path, "b.txt")))])

        assert list(nb) == [note]
        assert recorder.calls == 0

    def test_moved_directory_whose_source_exists_again_keeps_its_notes(
        self, nested_notes_dir, unwatched_notebook
    ):
        nb = unwatched_notebook(nested_notes_dir)
        before = titles(nb)

        deliver(
            nb,
            [
                (
                    "on_moved",
                    DirMovedEvent(os.path.join(nb.path, "work"), os.path.join(nb.path, "w")),
                )
            ],
        )

        assert titles(nb) == before

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


SEARCH_NOTES = {
    "Meeting notes.txt": "Discussed the Budget for 2025.\nCAFÉ order",
    "groceries.md": "milk, eggs, café au lait",
    "Zebra.txt": "stripes",
    "foo.txt": "bar",
}
ALL_SEARCH_TITLES = ["Meeting notes", "Zebra", "foo", "groceries"]


class TestSearchSemantics:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("", ALL_SEARCH_TITLES),
            ("  \t ", ALL_SEARCH_TITLES),
            ("meeting", ["Meeting notes"]),
            ("Meeting", ["Meeting notes"]),
            ("MEETING", []),
            ("budget", ["Meeting notes"]),
            ("Budget", ["Meeting notes"]),
            ("BUDGET", []),
            ("discussed budget", ["Meeting notes"]),
            ("  discussed   budget ", ["Meeting notes"]),
            ("meeting Budget", ["Meeting notes"]),
            ("meeting BUDGET", []),
            ("discussed milk", []),
            ("café", ["Meeting notes", "groceries"]),
            ("CAFÉ", ["Meeting notes"]),
            ("Café", []),
            ("2025", ["Meeting notes"]),
            ("2025.", ["Meeting notes"]),
            ("eggs,", ["groceries"]),
            ("zebra", ["Zebra"]),
            ("zebra stripes", ["Zebra"]),
            ("Zebra stripes", ["Zebra"]),
            ("ZEBRA", []),
            ("foo bar", ["foo"]),
            ("foobar", []),
            ("o", ["Meeting notes", "foo", "groceries"]),
            ("xyzzy", []),
        ],
    )
    def test_query(self, temp_notes_dir, write_notes, unwatched_notebook, query, expected):
        write_notes(temp_notes_dir, SEARCH_NOTES)
        nb = unwatched_notebook(temp_notes_dir)

        assert sorted(n.title for n in nb.search(query)) == expected
        assert sorted(n.title for n in tv_notebook.brute_force_search(nb, query)) == expected

    def test_search_returns_a_new_list_each_time(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)
        first = nb.search("")
        first.clear()

        assert len(nb.search("")) == 4


FIND_NOTES = {
    "meeting notes.txt": "agenda for the budget",
    "mtg plan.txt": "the meeting is tomorrow",
    "zebra.txt": "stripes and a maze",
    "maze.txt": "a zebra in a maze",
    "abc 1.txt": "x",
    "abc 2.txt": "y",
    "groceries.md": "milk eggs",
}
NEWEST_FIRST = ["groceries", "abc 2", "abc 1", "maze", "zebra", "mtg plan", "meeting notes"]


class TestFind:
    @pytest.fixture
    def nb(self, temp_notes_dir, write_notes, unwatched_notebook):
        write_notes(temp_notes_dir, FIND_NOTES)
        return unwatched_notebook(temp_notes_dir)

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            pytest.param("", NEWEST_FIRST, id="empty"),
            pytest.param(" \t ", NEWEST_FIRST, id="blank"),
            pytest.param(
                "m",
                ["maze", "mtg plan", "meeting notes", "groceries", "zebra"],
                id="one-character-prefix-then-exact",
            ),
            pytest.param("mtg", ["mtg plan", "meeting notes"], id="prefix-then-fuzzy"),
            pytest.param("budget", ["meeting notes"], id="exact-only"),
            pytest.param("maze", ["maze", "zebra"], id="equal-then-exact"),
            pytest.param("ze", ["zebra", "maze"], id="two-characters-prefix-then-exact"),
            pytest.param("abc", ["abc 2", "abc 1"], id="prefix-newer-first"),
            pytest.param("ZE", ["zebra"], id="two-characters-prefix-ignores-case"),
            pytest.param(
                "M", ["maze", "mtg plan", "meeting notes"], id="one-character-prefix-ignores-case"
            ),
            pytest.param("ZB", [], id="two-characters-no-fuzzy"),
            pytest.param("ZEB", ["zebra"], id="uppercase-fuzzy"),
            pytest.param("MAZE", ["maze"], id="uppercase-equal-not-exact"),
            pytest.param("m p", ["mtg plan", "zebra"], id="space-in-fuzzy-query"),
            pytest.param("milk eggs", ["groceries"], id="multi-word-exact"),
            pytest.param("xyzzy", [], id="nothing"),
        ],
    )
    def test_order(self, nb, query, expected):
        assert [r.note.title for r in nb.find(query)] == expected

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            pytest.param(
                "",
                dict.fromkeys(NEWEST_FIRST, ((), None, ())),
                id="empty",
            ),
            pytest.param(
                "m",
                {
                    "groceries": ((), "milk eggs", ((0, 1),)),
                    "maze": (((0, 1),), None, ()),
                    "zebra": ((), "stripes and a maze", ((14, 15),)),
                    "mtg plan": (((0, 1),), None, ()),
                    "meeting notes": (((0, 1),), None, ()),
                },
                id="one-character",
            ),
            pytest.param(
                "mtg",
                {
                    "mtg plan": (((0, 3),), None, ()),
                    "meeting notes": (((0, 1), (3, 4), (6, 7)), None, ()),
                },
                id="fuzzy",
            ),
            pytest.param(
                "budget",
                {"meeting notes": ((), "agenda for the budget", ((15, 21),))},
                id="exact-in-contents",
            ),
            pytest.param(
                "maze",
                {
                    "maze": (((0, 4),), None, ()),
                    "zebra": ((), "stripes and a maze", ((14, 18),)),
                },
                id="fuzzy-then-exact",
            ),
            pytest.param(
                "ZEB",
                {"zebra": (((0, 3),), None, ())},
                id="uppercase-query-gets-real-positions",
            ),
            pytest.param(
                "m p",
                {
                    "mtg plan": (((0, 1), (3, 5)), None, ()),
                    "zebra": ((), "stripes and a maze", ((4, 5), (14, 15))),
                },
                id="exact-words-in-title-and-snippet",
            ),
        ],
    )
    def test_spans_and_snippets(self, nb, query, expected):
        found = {r.note.title: (r.title_spans, r.snippet, r.snippet_spans) for r in nb.find(query)}
        assert found == expected

    def test_exact_title_spans_use_smart_case(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        write_notes(temp_notes_dir, {"plan Plan.txt": "x"})
        nb = unwatched_notebook(temp_notes_dir)

        (lower,) = nb.find("plan x")
        (upper,) = nb.find("Plan x")

        assert (lower.title_spans, lower.snippet, lower.snippet_spans) == (
            ((0, 4), (5, 9)),
            "x",
            ((0, 1),),
        )
        assert (upper.title_spans, upper.snippet, upper.snippet_spans) == (
            ((5, 9),),
            "x",
            ((0, 1),),
        )

    def test_result_is_immutable(self, nb):
        result = nb.find("zebra")[0]

        with pytest.raises(AttributeError):
            result.snippet = "x"

    def test_result_set_is_exact_prefix_and_fuzzy_title_matches(self, nb):
        for query in ["m", "M", "mtg", "ze", "ZE", "ZEB", "maze", "m p", "abc", "a", "e", "budget"]:
            exact = {n.title for n in nb.search(query)}
            prefix = {n.title for n in nb if prefix_length(n.title, query) is not None}
            needle = query.strip().lower()
            fuzzy = (
                {n.title for n in nb if fuzzy_match(needle, n.title) is not None}
                if len(needle) >= 3
                else set()
            )
            results = nb.find(query)
            assert {r.note.title for r in results} == exact | prefix | fuzzy, query
            assert {r.note.title for r in results if r.exact} == exact, query
            assert {r.note.title for r in results if r.prefix} == prefix, query

    @pytest.mark.parametrize("query", ["", " ", " \t "])
    def test_query_without_words_has_no_prefix_results(self, nb, query):
        assert not any(r.prefix for r in nb.find(query))

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            pytest.param("", dict.fromkeys(NEWEST_FIRST, True), id="empty-all-exact"),
            pytest.param("mtg", {"mtg plan": True, "meeting notes": False}, id="fuzzy-only"),
            pytest.param("MAZE", {"maze": False}, id="smart-case"),
            pytest.param("m p", {"mtg plan": True, "zebra": True}, id="words-anywhere"),
        ],
    )
    def test_exact_flag(self, nb, query, expected):
        assert {r.note.title: r.exact for r in nb.find(query)} == expected

    PREFIX_NOTES: ClassVar[dict[str, str]] = {
        "todo.txt": "a",
        "todo house.txt": "b",
        "Todo list.txt": "c",
        "toad dome.txt": "d",
        "photo docs.txt": "todo",
        "groceries.txt": "todo milk",
    }

    def test_equal_title_then_prefix_then_fuzzy_then_exact(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        write_notes(temp_notes_dir, self.PREFIX_NOTES)
        nb = unwatched_notebook(temp_notes_dir)

        results = nb.find("todo")

        assert [r.note.title for r in results] == [
            "todo",
            "Todo list",
            "todo house",
            "toad dome",
            "photo docs",
            "groceries",
        ]
        assert [r.title_spans for r in results[:3]] == [((0, 4),)] * 3

    def test_prefix_is_of_the_whole_title(self, temp_notes_dir, write_notes, unwatched_notebook):
        os.mkdir(Path(temp_notes_dir, "recipes"))
        write_notes(temp_notes_dir, {"recipes/pasta bake.txt": "x", "pastures.txt": "y"})
        nb = unwatched_notebook(temp_notes_dir)

        assert [r.note.title for r in nb.find("pas")] == [
            "pastures",
            os.path.join("recipes", "pasta bake"),
        ]
        assert [r.note.title for r in nb.find("rec")] == [os.path.join("recipes", "pasta bake")]

    def test_prefix_ignores_case_and_normalisation(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        write_notes(temp_notes_dir, {"Café menu.txt": "x", "café.txt": "y", "a café.txt": "z"})
        nb = unwatched_notebook(temp_notes_dir)

        results = nb.find(unicodedata.normalize("NFD", "café"))

        assert [r.note.title for r in results] == ["café", "Café menu", "a café"]
        assert [r.title_spans for r in results] == [((0, 4),), ((0, 4),), ((2, 6),)]

    def test_prefix_ignores_case_even_where_smart_case_does_not(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        write_notes(temp_notes_dir, self.PREFIX_NOTES)
        nb = unwatched_notebook(temp_notes_dir)

        results = nb.find("Todo")

        assert [(r.note.title, r.prefix, r.exact) for r in results] == [
            ("todo", True, False),
            ("Todo list", True, True),
            ("todo house", True, False),
            ("toad dome", False, False),
            ("photo docs", False, False),
        ]

    @pytest.mark.parametrize(
        ("title", "query"),
        [
            ("foo", "Foo"),
            ("ñ", "Ñ"),
            ("Straße", "strasse"),
            ("Straße", "STRASS"),
            ("\ufb01le", "fil"),
            ("\u0130stanbul", "i"),
        ],
    )
    def test_title_that_starts_with_the_query_ignoring_case_is_first(
        self, temp_notes_dir, write_notes, unwatched_notebook, title, query
    ):
        write_notes(temp_notes_dir, {title + ".txt": "", "other.txt": query})
        nb = unwatched_notebook(temp_notes_dir)

        results = nb.find(query)

        assert (results[0].note.title, results[0].prefix) == (title, True)
        assert not any(r.prefix for r in results[1:])

    def test_top_row_is_autoselectable_whenever_any_prefix_match_is(
        self, tmp_path, unwatched_notebook
    ):
        """Over random queries on a random notebook: every note whose title
        starts with the query, ignoring case (`prefix_length`), is a result
        flagged `prefix`, and those results come first, so autoselecting the
        first result when it is flagged selects the top row.
        """
        rng = random.Random(4)
        vocab = ["todo", "Todo", "to", "do", "house", "plan", "mtg", "meeting", "zebra"]
        vocab += ["maze", "ze", "a", "ab", "abc", "café", "Straße", "list", "notes"]
        os.mkdir(tmp_path / "work")
        titles: dict[str, str] = {}
        while len(titles) < 80:
            title = " ".join(rng.choice(vocab) for _ in range(rng.randint(1, 3)))
            if rng.random() < 0.15:
                title = os.path.join("work", title)
            titles.setdefault(title.casefold(), title)
        for title in titles.values():
            path = tmp_path / (title + ".txt")
            path.write_text(" ".join(rng.choice(vocab) for _ in range(rng.randint(0, 6))))
            mtime = BASE_MTIME + rng.randint(0, 40)
            os.utime(path, (mtime, mtime))
        nb = unwatched_notebook(tmp_path)

        queries = []
        for _ in range(400):
            kind = rng.random()
            if kind < 0.5:
                title = rng.choice(list(titles.values()))
                query = title[: rng.randint(1, len(title))]
            elif kind < 0.8:
                query = " ".join(rng.choice(vocab) for _ in range(rng.randint(1, 2)))
            else:
                query = "".join(rng.choice("abdeotz ") for _ in range(rng.randint(1, 4)))
            if rng.random() < 0.3:
                query = "".join(c.swapcase() if rng.random() < 0.3 else c for c in query)
            if rng.random() < 0.1:
                query = unicodedata.normalize("NFD", query)
            queries.append(query)

        prefix_matched = spans_checked = 0
        for query in queries:
            results = nb.find(query)
            folded = unicodedata.normalize("NFC", query).casefold()
            needle = fuzzy_needle(query)

            def is_prefix(r, query=query):
                return prefix_length(r.note.title, query) is not None

            def group(r, query=query, needle=needle):
                if is_prefix(r):
                    return 1 if fold(r.note.title) == fold(query) else 2
                fuzzy = needle is not None and fuzzy_match(needle, r.note.title) is not None
                return 3 if fuzzy else 4

            assert len({r.note for r in results}) == len(results), query
            assert {r.note for r in results if r.exact} == set(nb.search(query)), query
            assert [r.prefix for r in results] == [is_prefix(r) for r in results], query
            assert {r.note for r in results if r.prefix} == {
                n for n in nb if prefix_length(n.title, query) is not None
            }, query
            if any(is_prefix(r) for r in results):
                prefix_matched += 1
                assert is_prefix(results[0]), query
            groups = [group(r) for r in results]
            assert groups == sorted(groups), query
            if query.islower() and len(folded) == len(query):
                for r in results:
                    if group(r) <= 2 and r.exact:
                        covered = {i for a, b in r.title_spans for i in range(a, b)}
                        typed = {i for i, c in enumerate(r.note.title[: len(folded)]) if c != " "}
                        assert typed <= covered, (query, r.note.title, r.title_spans)
                        spans_checked += 1
            for g in (1, 2, 4):
                mtimes = [r.note.mtime for r in results if group(r) == g]
                assert mtimes == sorted(mtimes, reverse=True), query
        assert prefix_matched > 100
        assert spans_checked > 100

    def test_snippet_width(self, temp_notes_dir, write_notes, unwatched_notebook):
        write_notes(temp_notes_dir, {"plan.txt": "intro " * 50 + "budget" + " outro" * 50})
        nb = unwatched_notebook(temp_notes_dir)

        (wide,) = nb.find("budget")
        (narrow,) = nb.find("budget", snippet_width=20)

        assert len(wide.snippet) == SNIPPET_WIDTH
        assert len(narrow.snippet) == 20
        assert [narrow.snippet[a:b] for a, b in narrow.snippet_spans] == ["budget"]

    def test_fuzzy_matches_the_last_path_component_unless_query_has_a_separator(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        os.mkdir(Path(temp_notes_dir, "work"))
        write_notes(temp_notes_dir, {"work/zebra.txt": "x", "wonky.txt": "y"})
        nb = unwatched_notebook(temp_notes_dir)

        found = [(r.note.title, r.title_spans, r.exact) for r in nb.find("wky")]
        nested = [(r.note.title, r.title_spans) for r in nb.find("zea")]
        full = [(r.note.title, r.title_spans) for r in nb.find("k/z")]

        assert found == [("wonky", ((0, 1), (3, 5)), False)]
        assert nested == [(os.path.join("work", "zebra"), ((5, 7), (9, 10)))]
        assert full == [(os.path.join("work", "zebra"), ((3, 6),))]

    def test_nfd_title_and_contents_match_nfc_query(self, temp_notes_dir, unwatched_notebook):
        nfd = unicodedata.normalize("NFD", "café menu")
        Path(temp_notes_dir, nfd + ".txt").write_text(
            unicodedata.normalize("NFD", "crème brûlée"), encoding="utf-8"
        )
        nb = unwatched_notebook(temp_notes_dir)

        (by_title,) = nb.find("café")
        (by_contents,) = nb.find("brûlée")

        assert by_title.note.title == "café menu"
        assert by_title.title_spans == ((0, 4),)
        assert by_contents.snippet == "crème brûlée"
        assert by_contents.snippet_spans == ((6, 12),)
        assert by_title.note.contents == "crème brûlée"
        assert [n.title for n in nb.search(NFD_CAFE)] == ["café menu"]
        assert [n.title for n in nb.search("café")] == ["café menu"]

    def test_sees_watcher_changes(self, nb):
        path = Path(nb.path, "zebra.txt")
        path.write_text("now about budget")
        os.utime(path, (BASE_MTIME + 100, BASE_MTIME + 100))
        FileEventHandler(nb).on_modified(FileModifiedEvent(str(path)))

        assert [r.snippet for r in nb.find("budget")] == [
            "now about budget",
            "agenda for the budget",
        ]


class TestContentCache:
    """Searches use in-memory copies of the notes; watcher events update them."""

    def make(self, notes_dir, write_notes, unwatched_notebook, notes):
        write_notes(notes_dir, notes)
        nb = unwatched_notebook(notes_dir)
        return nb, FileEventHandler(nb)

    def found(self, nb, query):
        return sorted(n.title for n in nb.search(query))

    def test_search_does_not_read_files_once_cached(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        nb, _ = self.make(
            temp_notes_dir, write_notes, unwatched_notebook, {"a.txt": "apple", "b.txt": "banana"}
        )
        assert self.found(nb, "apple") == ["a"]
        os.remove(Path(nb.path, "a.txt"))
        Path(nb.path, "b.txt").write_text("cherry")

        assert self.found(nb, "apple") == ["a"]
        assert self.found(nb, "banana") == ["b"]
        assert self.found(nb, "cherry") == []

    def test_modified_event_rereads_note(self, temp_notes_dir, write_notes, unwatched_notebook):
        nb, handler = self.make(temp_notes_dir, write_notes, unwatched_notebook, {"a.txt": "old"})
        recorder = ChangeRecorder(nb)
        nb.on_change = recorder
        assert self.found(nb, "old") == ["a"]
        path = Path(nb.path, "a.txt")
        path.write_text("new")
        os.utime(path, (2_000_000_000, 2_000_000_000))

        handler.on_modified(FileModifiedEvent(str(path)))

        assert self.found(nb, "old") == []
        assert self.found(nb, "new") == ["a"]
        assert [n.mtime for n in nb] == [2_000_000_000]
        assert recorder.calls == 1

    def test_modified_event_without_change_does_not_notify(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        nb, handler = self.make(temp_notes_dir, write_notes, unwatched_notebook, {"a.txt": "same"})
        recorder = ChangeRecorder(nb)
        nb.on_change = recorder
        nb.search("same")

        handler.on_modified(FileModifiedEvent(os.path.join(nb.path, "a.txt")))

        assert recorder.calls == 0

    def test_atomic_save_by_rename_rereads_note(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        nb, handler = self.make(temp_notes_dir, write_notes, unwatched_notebook, {"a.txt": "old"})
        assert self.found(nb, "old") == ["a"]
        tmp = os.path.join(nb.path, ".a.txt.tmp")
        dest = os.path.join(nb.path, "a.txt")
        Path(tmp).write_text("new")
        os.replace(tmp, dest)

        handler.on_moved(FileMovedEvent(tmp, dest))

        assert self.found(nb, "old") == []
        assert self.found(nb, "new") == ["a"]

    def test_backup_rename_then_rewrite_rereads_note(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        nb, handler = self.make(temp_notes_dir, write_notes, unwatched_notebook, {"a.txt": "old"})
        assert self.found(nb, "old") == ["a"]
        path = os.path.join(nb.path, "a.txt")
        backup = path + "~"
        os.rename(path, backup)
        Path(path).write_text("new")
        os.remove(backup)

        handler.on_moved(FileMovedEvent(path, backup))
        handler.on_created(FileCreatedEvent(path))
        handler.on_deleted(FileDeletedEvent(backup))

        assert self.found(nb, "old") == []
        assert self.found(nb, "new") == ["a"]

    def test_created_event_for_known_note_rereads_it(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        nb, handler = self.make(temp_notes_dir, write_notes, unwatched_notebook, {"a.txt": "old"})
        assert self.found(nb, "old") == ["a"]
        path = Path(nb.path, "a.txt")
        path.write_text("new")

        handler.on_created(FileCreatedEvent(str(path)))

        assert self.found(nb, "new") == ["a"]

    def test_external_delete_removes_note_from_results(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        nb, handler = self.make(
            temp_notes_dir, write_notes, unwatched_notebook, {"a.txt": "apple", "b.txt": "apple"}
        )
        assert self.found(nb, "apple") == ["a", "b"]
        path = os.path.join(nb.path, "a.txt")
        os.remove(path)

        handler.on_deleted(FileDeletedEvent(path))

        assert self.found(nb, "apple") == ["b"]

    def test_create_caches_the_new_note(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        note = nb.create("fresh idea")
        mtime = os.stat(note.abspath).st_mtime
        os.remove(note.abspath)

        assert self.found(nb, "fresh idea") == ["fresh idea"]
        assert note.contents == ""
        assert note.mtime == mtime

    def test_create_then_edit_is_seen_after_event(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        note = nb.create("plan")
        Path(note.abspath).write_text("remember the milk")

        FileEventHandler(nb).on_modified(FileModifiedEvent(note.abspath))

        assert self.found(nb, "milk") == ["plan"]

    def test_mtime_is_cached_until_an_event(self, temp_notes_dir, write_notes, unwatched_notebook):
        nb, handler = self.make(temp_notes_dir, write_notes, unwatched_notebook, {"a.txt": "x"})
        (note,) = list(nb)
        assert note.mtime == BASE_MTIME
        os.utime(note.abspath, (BASE_MTIME + 100, BASE_MTIME + 100))

        assert note.mtime == BASE_MTIME

        handler.on_modified(FileModifiedEvent(note.abspath))

        assert note.mtime == BASE_MTIME + 100

    def test_refresh_rereads_at_once(self, temp_notes_dir, write_notes, unwatched_notebook):
        nb, _ = self.make(temp_notes_dir, write_notes, unwatched_notebook, {"a.txt": "old"})
        (note,) = list(nb)
        assert self.found(nb, "old") == ["a"]
        Path(note.abspath).write_text("new")

        assert nb.refresh(note) is True
        assert self.found(nb, "new") == ["a"]
        assert nb.refresh(note) is False

    def test_refresh_ignores_notes_not_in_notebook(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        Path(nb.path, "loose.txt").write_text("x")

        assert nb.refresh(tv_notebook.PlainTextNote("loose", nb, ".txt")) is False
        assert len(nb) == 0

    def test_refresh_with_a_replaced_note_object_updates_the_notebook(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        nb, handler = self.make(temp_notes_dir, write_notes, unwatched_notebook, {"a.txt": "old"})
        (held,) = list(nb)
        path = held.abspath
        os.rename(path, path + "~")
        handler.on_moved(FileMovedEvent(path, path + "~"))
        Path(path).write_text("")
        handler.on_created(FileCreatedEvent(path))
        Path(path).write_text("new")

        assert nb.refresh(held) is True
        assert self.found(nb, "new") == ["a"]

    def test_search_read_that_raced_with_event_does_not_overwrite_it(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        write_notes(temp_notes_dir, {"a.txt": "old"})
        nb = unwatched_notebook(temp_notes_dir, cls=HookedNoteBook)
        handler = FileEventHandler(nb)
        (note,) = list(nb)

        def edit_and_report():
            Path(note.abspath).write_text("new")
            handler.on_modified(FileModifiedEvent(note.abspath))

        nb.during_next_read = edit_and_report

        assert self.found(nb, "old") == ["a"]
        assert self.found(nb, "new") == ["a"]
        assert note.contents == "new"

    def test_refresh_that_raced_with_event_does_not_overwrite_it(
        self, temp_notes_dir, write_notes, unwatched_notebook
    ):
        write_notes(temp_notes_dir, {"a.txt": "old"})
        nb = unwatched_notebook(temp_notes_dir, cls=HookedNoteBook)
        handler = FileEventHandler(nb)
        (note,) = list(nb)
        assert self.found(nb, "old") == ["a"]

        def edit_and_report():
            Path(note.abspath).write_text("new")
            handler.on_modified(FileModifiedEvent(note.abspath))

        nb.during_next_read = edit_and_report

        assert nb.refresh(note) is True
        assert self.found(nb, "old") == []
        assert self.found(nb, "new") == ["a"]

    def test_unreadable_note_is_logged_once_and_searched_as_empty(
        self, temp_notes_dir, write_notes, unwatched_notebook, caplog
    ):
        write_notes(temp_notes_dir, {"a.txt": "x"})
        nb = unwatched_notebook(temp_notes_dir, cls=HookedNoteBook)
        os.remove(Path(nb.path, "a.txt"))

        with caplog.at_level(logging.ERROR, logger="tv3"):
            assert self.found(nb, "x") == []
            assert self.found(nb, "a") == ["a"]

        assert len([r for r in caplog.records if "Error reading note" in r.message]) == 1

    @pytest.mark.skipif(is_root(), reason="root can read any file")
    def test_note_that_cannot_be_read_is_logged_once_and_searched_as_empty(
        self, temp_notes_dir, write_notes, unwatched_notebook, caplog
    ):
        write_notes(temp_notes_dir, {"a.txt": "x"})
        nb = unwatched_notebook(temp_notes_dir, cls=HookedNoteBook)
        path = Path(nb.path, "a.txt")
        path.chmod(0)
        try:
            with caplog.at_level(logging.ERROR, logger="tv3"):
                assert self.found(nb, "x") == []
                assert self.found(nb, "a") == ["a"]
        finally:
            path.chmod(0o644)

        assert len([r for r in caplog.records if "Error reading note" in r.message]) == 1

    def test_directory_added_later_is_read_when_it_is_added(
        self, temp_notes_dir, unwatched_notebook
    ):
        nb = unwatched_notebook(temp_notes_dir)
        sub = Path(nb.path, "sub")
        sub.mkdir()
        (sub / "a.txt").write_text("apple")
        (sub / "b.txt").write_text("banana")

        FileEventHandler(nb).on_created(DirCreatedEvent(str(sub)))
        for path in sub.iterdir():
            path.unlink()

        assert self.found(nb, "apple") == [os.path.join("sub", "a")]
        assert self.found(nb, "banana") == [os.path.join("sub", "b")]

    def test_directory_moved_in_is_read_when_it_is_moved(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        hidden = Path(nb.path, ".incoming")
        hidden.mkdir()
        (hidden / "a.txt").write_text("apple")
        dest = Path(nb.path, "sub")
        os.rename(hidden, dest)

        FileEventHandler(nb).on_moved(DirMovedEvent(str(hidden), str(dest)))
        (dest / "a.txt").unlink()

        assert self.found(nb, "apple") == [os.path.join("sub", "a")]


MIB = 1024 * 1024


class TestSizeCap:
    """Only the first MiB of a note is kept in memory and searched."""

    def found(self, nb, query):
        return sorted(n.title for n in nb.search(query))

    def test_only_the_first_mebibyte_is_searched(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "big.txt").write_bytes(b"head " + b"x" * MIB + b" tail")
        nb = unwatched_notebook(temp_notes_dir)

        assert self.found(nb, "head") == ["big"]
        assert self.found(nb, "tail") == []
        (note,) = list(nb)
        assert len(note.contents) == MIB

    def test_a_note_of_exactly_one_mebibyte_is_not_truncated(
        self, temp_notes_dir, unwatched_notebook, caplog
    ):
        Path(temp_notes_dir, "edge.txt").write_bytes(b"x" * (MIB - 4) + b"tail")
        nb = unwatched_notebook(temp_notes_dir)

        with caplog.at_level(logging.INFO, logger="tv3"):
            assert self.found(nb, "tail") == ["edge"]

        assert not [r for r in caplog.records if "truncated" in r.message]

    def test_character_cut_at_the_limit_is_replaced(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "cut.txt").write_bytes(b"a" * (MIB - 1) + "é tail".encode())
        nb = unwatched_notebook(temp_notes_dir)
        (note,) = list(nb)

        assert note.contents == "a" * (MIB - 1) + "�"
        assert self.found(nb, "é") == []
        assert self.found(nb, "a�") == ["cut"]
        assert self.found(nb, "A�") == []
        assert nb.refresh(note) is False

    def test_large_note_is_not_read_whole(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "huge.txt").write_bytes(b"y" * (32 * MIB))
        nb = unwatched_notebook(temp_notes_dir, cls=HookedNoteBook)
        tracemalloc.start()
        try:
            self.found(nb, "y")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert peak < 8 * MIB

    def test_lowercase_note_is_stored_once(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "lower.txt").write_bytes(b"z" * MIB)
        nb = unwatched_notebook(temp_notes_dir, cls=HookedNoteBook)
        tracemalloc.start()
        try:
            self.found(nb, "z")
            retained, _ = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert retained < 1.5 * MIB

    def test_truncation_is_logged_once_per_file(self, temp_notes_dir, unwatched_notebook, caplog):
        path = Path(temp_notes_dir, "big.txt")
        path.write_bytes(b"x" * (2 * MIB))
        nb = unwatched_notebook(temp_notes_dir, cls=HookedNoteBook)
        handler = FileEventHandler(nb)

        with caplog.at_level(logging.INFO, logger="tv3"):
            self.found(nb, "x")
            path.write_bytes(b"y" * (2 * MIB))
            handler.on_modified(FileModifiedEvent(str(path)))
            assert self.found(nb, "y") == ["big"]

        logged = [r for r in caplog.records if "truncated" in r.message]
        assert len(logged) == 1
        assert logged[0].levelno == logging.INFO
        assert path.name in logged[0].getMessage()


class TestBackgroundRead:
    def test_notes_are_read_after_startup(self, temp_notes_dir, write_notes, notebook):
        write_notes(temp_notes_dir, {f"n{i}.txt": f"word{i}" for i in range(50)})
        nb = notebook(temp_notes_dir)
        assert len(nb.search("word")) == 50
        nb.close()
        for path in Path(temp_notes_dir).iterdir():
            path.unlink()

        assert sorted(n.title for n in nb.search("word7")) == ["n7"]
        assert len(nb.search("word")) == 50

    def test_close_stops_background_read(self, temp_notes_dir, write_notes, notebook):
        write_notes(temp_notes_dir, {f"n{i}.txt": "x" for i in range(50)})
        nb = notebook(temp_notes_dir, cls=HookedNoteBook)

        nb.close()
        assert nb.warmer_finished.is_set()
        for path in Path(temp_notes_dir).iterdir():
            path.unlink()

        assert nb.search("x") == []

    def test_empty_search_does_not_wait_for_background_read(
        self, temp_notes_dir, write_notes, notebook
    ):
        write_notes(temp_notes_dir, {f"n{i}.txt": "x" for i in range(50)})
        gate = threading.Event()
        nb = notebook(temp_notes_dir, cls=HookedNoteBook, warmer_gate=gate)
        try:
            assert len(nb.search("")) == 50
            assert len(nb.search("  ")) == 50
            assert not nb.warmer_finished.is_set()
            assert sorted(n.mtime for n in nb) == [BASE_MTIME + i for i in range(50)]
        finally:
            gate.set()

    def test_search_waits_for_background_read(self, temp_notes_dir, write_notes, notebook):
        write_notes(temp_notes_dir, {f"n{i}.txt": "x" for i in range(50)})
        gate = threading.Event()
        nb = notebook(temp_notes_dir, cls=HookedNoteBook, warmer_gate=gate)
        threading.Timer(0.05, gate.set).start()

        assert len(nb.search("x")) == 50
        assert nb.warmer_finished.is_set()

    def test_loaded_and_notifies_once_the_background_read_finishes(
        self, temp_notes_dir, write_notes, notebook
    ):
        write_notes(temp_notes_dir, {f"n{i}.txt": "x" for i in range(50)})
        gate = threading.Event()
        nb = notebook(temp_notes_dir, cls=HookedNoteBook, warmer_gate=gate)
        notified = threading.Event()
        seen = []

        def on_change():
            seen.append((nb.loaded, len(nb.search("x"))))
            notified.set()

        nb.on_change = on_change
        assert not nb.loaded

        gate.set()

        assert notified.wait(5)
        assert nb.warmer_finished.wait(5)
        assert seen == [(True, 50)]
        assert nb.loaded

    def test_not_loaded_and_no_notification_when_closed_before_the_read(
        self, temp_notes_dir, write_notes, notebook
    ):
        write_notes(temp_notes_dir, {f"n{i}.txt": "x" for i in range(50)})
        nb = notebook(temp_notes_dir, cls=HookedNoteBook)
        changes = ChangeRecorder(nb)
        nb.on_change = changes

        nb.close()

        assert nb.warmer_finished.is_set()
        assert changes.calls == 0
        assert not nb.loaded


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

    def test_root_deleted_clears_notes_and_reports_it(
        self, populated_notes_dir, notebook, wait_until, caplog
    ):
        nb = notebook(populated_notes_dir)
        changed = threading.Event()
        nb.on_change = changed.set
        caplog.set_level(logging.ERROR, logger="tv3")

        shutil.rmtree(nb.path)

        wait_until(lambda: len(nb) == 0)
        assert changed.wait(5)
        wait_until(lambda: any("no longer watching" in r.getMessage() for r in caplog.records))

    def test_close_from_on_change(self, temp_notes_dir, notebook):
        nb = notebook(temp_notes_dir)
        done = threading.Event()
        errors = []

        def close_from_callback():
            try:
                nb.close()
            except Exception as e:
                errors.append(e)
            done.set()

        nb.on_change = close_from_callback
        Path(nb.path, "trigger.txt").write_text("")

        assert done.wait(5)
        assert errors == []

    def test_concurrent_close(self, temp_notes_dir, notebook):
        nb = notebook(temp_notes_dir)
        start = threading.Barrier(4)
        errors = []

        def closer():
            start.wait()
            try:
                nb.close()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=closer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


class TestRootRemoved:
    def test_deleted_root_clears_notes_logs_error_and_notifies(
        self, populated_notes_dir, unwatched_notebook, caplog
    ):
        nb = unwatched_notebook(populated_notes_dir)
        recorder = ChangeRecorder(nb)
        nb.on_change = recorder
        caplog.set_level(logging.ERROR, logger="tv3")
        shutil.rmtree(nb.path)

        FileEventHandler(nb).on_deleted(DirDeletedEvent(nb.path))

        assert len(nb) == 0
        assert recorder.calls == 1
        assert [r.levelno for r in caplog.records if "no longer watching" in r.getMessage()] == [
            logging.ERROR
        ]

    def test_moved_root_clears_notes(self, tmp_path, unwatched_notebook, caplog):
        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "a.txt").write_text("")
        nb = unwatched_notebook(notes)
        caplog.set_level(logging.ERROR, logger="tv3")
        dest = str(tmp_path / "elsewhere")
        os.rename(nb.path, dest)

        FileEventHandler(nb).on_moved(DirMovedEvent(nb.path, dest))

        assert len(nb) == 0
        assert any("no longer watching" in r.getMessage() for r in caplog.records)


class TestUnicodeNormalization:
    def test_create_matches_indexed_note_in_other_normalization(
        self, temp_notes_dir, unwatched_notebook
    ):
        Path(temp_notes_dir, NFD_CAFE + ".txt").write_text("original")
        nb = unwatched_notebook(temp_notes_dir)

        with pytest.raises(NoteAlreadyExistsError) as exc_info:
            nb.create(NFC_CAFE)

        assert exc_info.value.note.title == NFC_CAFE
        assert exc_info.value.note.abspath == os.path.join(nb.path, NFD_CAFE + ".txt")
        assert len(nb) == 1
        assert os.listdir(nb.path) == [NFD_CAFE + ".txt"]

    def test_unindexed_file_is_registered_under_its_on_disk_name(
        self, temp_notes_dir, unwatched_notebook
    ):
        if not filesystem_is_normalization_insensitive(temp_notes_dir):
            pytest.skip("needs a normalization-insensitive filesystem")
        nb = unwatched_notebook(temp_notes_dir)
        Path(nb.path, NFD_CAFE + ".txt").write_text("original")

        with pytest.raises(NoteAlreadyExistsError) as exc_info:
            nb.create(NFC_CAFE)

        assert exc_info.value.note.abspath == os.path.join(nb.path, NFD_CAFE + ".txt")
        assert titles(nb) == [NFC_CAFE]

    def test_watcher_event_in_other_normalization_is_not_a_duplicate(
        self, temp_notes_dir, unwatched_notebook
    ):
        if not filesystem_is_normalization_insensitive(temp_notes_dir):
            pytest.skip("needs a normalization-insensitive filesystem")
        Path(temp_notes_dir, NFD_CAFE + ".txt").write_text("")
        nb = unwatched_notebook(temp_notes_dir)

        FileEventHandler(nb).on_created(FileCreatedEvent(os.path.join(nb.path, NFC_CAFE + ".txt")))

        assert [n.abspath for n in nb] == [os.path.join(nb.path, NFD_CAFE + ".txt")]

    def test_nfd_note_has_nfc_title_and_can_be_renamed_and_deleted(
        self, temp_notes_dir, unwatched_notebook
    ):
        Path(temp_notes_dir, NFD_CAFE + ".txt").write_text("x")
        Path(temp_notes_dir, NFD_CAFE + " 2.txt").write_text("y")
        nb = unwatched_notebook(temp_notes_dir)
        first = next(n for n in nb if n.title == NFC_CAFE)
        second = next(n for n in nb if n.title == NFC_CAFE + " 2")

        assert nb.rename(first, NFC_CAFE) is first
        renamed = nb.rename(first, "menu")
        nb.delete(second)

        assert os.listdir(nb.path) == ["menu.txt"]
        assert list(nb) == [renamed]
        assert Path(renamed.abspath).read_text() == "x"

    def test_created_note_uses_on_disk_directory_name(self, temp_notes_dir, unwatched_notebook):
        if not filesystem_is_case_insensitive(temp_notes_dir):
            pytest.skip("needs a case-insensitive filesystem")
        os.mkdir(os.path.join(temp_notes_dir, "work"))
        nb = unwatched_notebook(temp_notes_dir)

        note = nb.create("WORK/plan")

        assert note.title == os.path.join("work", "plan")
        assert os.listdir(nb.path) == ["work"]


class TestCreateEdgeCases:
    def test_create_through_symlinked_directory_uses_real_path(
        self, temp_notes_dir, unwatched_notebook
    ):
        os.symlink(temp_notes_dir, os.path.join(temp_notes_dir, "self"))
        nb = unwatched_notebook(temp_notes_dir)

        note = nb.create("self/x")

        assert note.title == "x"
        assert note.abspath == os.path.join(nb.path, "x.txt")
        assert titles(nb) == ["x"]
        with pytest.raises(NoteAlreadyExistsError) as exc_info:
            nb.create("x")
        assert exc_info.value.note is note

    def test_create_through_symlinked_directory_into_hidden_directory_is_rejected(
        self, temp_notes_dir, unwatched_notebook
    ):
        os.mkdir(os.path.join(temp_notes_dir, ".git"))
        os.symlink(".git", os.path.join(temp_notes_dir, "git"))
        nb = unwatched_notebook(temp_notes_dir)

        with pytest.raises(InvalidNoteTitleError):
            nb.create("git/x")

        assert os.listdir(os.path.join(temp_notes_dir, ".git")) == []

    def test_create_over_symlinked_file_is_rejected(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "n.txt").write_text("target")
        os.symlink("n.txt", os.path.join(temp_notes_dir, "alias.txt"))
        nb = unwatched_notebook(temp_notes_dir)

        with pytest.raises(InvalidNoteTitleError):
            nb.create("alias")

        assert titles(nb) == ["n"]

    def test_directory_at_note_path_is_rejected(self, temp_notes_dir, unwatched_notebook):
        os.mkdir(os.path.join(temp_notes_dir, "folder.txt"))
        nb = unwatched_notebook(temp_notes_dir)

        with pytest.raises(InvalidNoteTitleError):
            nb.create("folder")

        assert len(nb) == 0

    def test_external_delete_then_create_succeeds(self, populated_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(populated_notes_dir)
        path = os.path.join(nb.path, "first_note.txt")
        os.remove(path)

        note = nb.create("first_note")

        assert note.abspath == path
        assert os.path.isfile(path)
        assert note in nb
        assert len(nb) == 4

    def test_late_delete_event_does_not_drop_recreated_note(
        self, populated_notes_dir, unwatched_notebook
    ):
        nb = unwatched_notebook(populated_notes_dir)
        path = os.path.join(nb.path, "first_note.txt")
        os.remove(path)
        nb.create("first_note")

        FileEventHandler(nb).on_deleted(FileDeletedEvent(path))

        assert "first_note" in titles(nb)

    def test_component_longer_than_name_max_writes_nothing(
        self, temp_notes_dir, unwatched_notebook
    ):
        nb = unwatched_notebook(temp_notes_dir)
        name_max = os.pathconf(nb.path, "PC_NAME_MAX")

        with pytest.raises(InvalidNoteTitleError):
            nb.create("dir/" + "x" * (name_max + 1))

        assert os.listdir(nb.path) == []

    def test_path_longer_than_path_max_writes_nothing(self, temp_notes_dir, unwatched_notebook):
        nb = unwatched_notebook(temp_notes_dir)
        path_max = os.pathconf(nb.path, "PC_PATH_MAX")
        title = "/".join(["d" * 100] * (path_max // 100 + 1))

        with pytest.raises(InvalidNoteTitleError):
            nb.create(title)

        assert os.listdir(nb.path) == []

    def test_failed_write_removes_directories_it_created(
        self, temp_notes_dir, unwatched_notebook, monkeypatch
    ):
        os.mkdir(os.path.join(temp_notes_dir, "existing"))
        nb = unwatched_notebook(temp_notes_dir)

        def failing_open(path, *args, **kwargs):
            raise OSError(28, "No space left on device", path)

        monkeypatch.setattr(tv_notebook, "open", failing_open, raising=False)
        with pytest.raises(NewNoteError):
            nb.create("existing/new/deeper/note")

        assert os.listdir(nb.path) == ["existing"]
        assert os.listdir(os.path.join(nb.path, "existing")) == []
        assert len(nb) == 0

    def test_write_failure_in_read_only_directory(
        self, temp_notes_dir, unwatched_notebook, read_only
    ):
        if is_root():
            pytest.skip("root ignores directory permissions")
        nb = unwatched_notebook(temp_notes_dir)
        read_only(nb.path)

        with pytest.raises(NewNoteError) as exc_info:
            nb.create("note")

        assert type(exc_info.value) is NewNoteError
        assert len(nb) == 0

    def test_extension_matching_follows_filesystem_case_rules(
        self, temp_notes_dir, unwatched_notebook
    ):
        if not filesystem_is_case_insensitive(temp_notes_dir):
            pytest.skip("needs a case-insensitive filesystem")
        Path(temp_notes_dir, "Old.TXT").write_text("")
        nb = unwatched_notebook(temp_notes_dir, extensions=[".txt", ".md"])

        note = nb.create("Ideas.MD")

        assert note.title == "Ideas"
        assert note.extension == ".MD"
        assert sorted(os.listdir(nb.path)) == ["Ideas.MD", "Old.TXT"]
        assert titles(nb) == ["Ideas", "Old"]

    def test_extension_matching_is_exact_on_case_sensitive_filesystem(
        self, temp_notes_dir, unwatched_notebook
    ):
        if filesystem_is_case_insensitive(temp_notes_dir):
            pytest.skip("needs a case-sensitive filesystem")
        nb = unwatched_notebook(temp_notes_dir, extensions=[".txt", ".md"])

        nb.create("Ideas.MD")

        assert os.listdir(nb.path) == ["Ideas.MD.txt"]


class TestCaseSensitivityProbe:
    def test_read_only_directory_uses_existing_entry(self, temp_notes_dir, read_only):
        Path(temp_notes_dir, "r.txt").write_text("")
        expected = filesystem_is_case_insensitive(temp_notes_dir)
        read_only(temp_notes_dir)

        assert tv_notebook._probe_case_insensitive(temp_notes_dir) is expected

    def test_writes_nothing_when_an_entry_exists(self, temp_notes_dir):
        Path(temp_notes_dir, "r.txt").write_text("")
        expected = filesystem_is_case_insensitive(temp_notes_dir)
        before = os.stat(temp_notes_dir).st_mtime_ns

        assert tv_notebook._probe_case_insensitive(temp_notes_dir) is expected
        assert os.listdir(temp_notes_dir) == ["r.txt"]
        assert os.stat(temp_notes_dir).st_mtime_ns == before

    def test_empty_directory_uses_its_own_name(self, tmp_path):
        notes = tmp_path / "notes"
        notes.mkdir()
        expected = filesystem_is_case_insensitive(tmp_path)
        before = os.stat(notes).st_mtime_ns

        assert tv_notebook._probe_case_insensitive(str(notes)) is expected
        assert os.stat(notes).st_mtime_ns == before

    def test_entry_with_case_variant_sibling_is_case_sensitive(self, temp_notes_dir):
        Path(temp_notes_dir, "r.txt").write_text("a")
        if Path(temp_notes_dir, "R.txt").exists():
            pytest.skip("needs a case-sensitive filesystem")
        Path(temp_notes_dir, "R.txt").write_text("b")

        assert tv_notebook._probe_case_insensitive(temp_notes_dir) is False

    def test_unprobeable_directory_defaults_to_case_insensitive(self, tmp_path, read_only):
        if is_root():
            pytest.skip("root ignores directory permissions")
        unnamed = tmp_path / "1234"
        unnamed.mkdir()
        read_only(str(unnamed))

        assert tv_notebook._probe_case_insensitive(str(unnamed)) is True


class TestSymlinkPolicy:
    def test_symlinked_note_file_is_not_indexed(self, temp_notes_dir, unwatched_notebook):
        Path(temp_notes_dir, "n.txt").write_text("x")
        os.symlink("n.txt", os.path.join(temp_notes_dir, "alias.txt"))
        nb = unwatched_notebook(temp_notes_dir)

        FileEventHandler(nb).on_created(FileCreatedEvent(os.path.join(nb.path, "alias.txt")))

        assert titles(nb) == ["n"]

    def test_symlinked_directories_are_not_indexed(self, temp_notes_dir, unwatched_notebook):
        os.mkdir(os.path.join(temp_notes_dir, "real"))
        Path(temp_notes_dir, "real", "n.txt").write_text("x")
        os.symlink("real", os.path.join(temp_notes_dir, "alias"))
        os.symlink(temp_notes_dir, os.path.join(temp_notes_dir, "loop"))
        nb = unwatched_notebook(temp_notes_dir)
        handler = FileEventHandler(nb)

        handler.on_created(DirCreatedEvent(os.path.join(nb.path, "alias")))
        handler.on_created(DirCreatedEvent(os.path.join(nb.path, "loop")))

        assert titles(nb) == [os.path.join("real", "n")]

    def test_event_through_symlinked_directory_maps_to_real_note(
        self, temp_notes_dir, unwatched_notebook
    ):
        Path(temp_notes_dir, "n.txt").write_text("x")
        os.symlink(temp_notes_dir, os.path.join(temp_notes_dir, "self"))
        nb = unwatched_notebook(temp_notes_dir)

        FileEventHandler(nb).on_created(
            FileCreatedEvent(os.path.join(nb.path, "self", "self", "n.txt"))
        )

        assert titles(nb) == ["n"]


class TestScanAndWatcherAgree:
    TREE = (
        "a.txt",
        "b.md",
        "c.rst",
        ".hidden.txt",
        "backup/old.txt",
        "deep/backup/old.txt",
        ".git/x.txt",
        "sub/n.txt",
        "sub/n.txt~",
        "sub/#n.txt#",
    )

    @staticmethod
    def write_tree(root, relpaths):
        paths = []
        for relpath in relpaths:
            path = Path(root, relpath)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
            paths.append(path)
        return paths

    def test_same_paths_indexed_by_scan_and_by_watcher(self, tmp_path, unwatched_notebook):
        self.write_tree(tmp_path / "scanned", self.TREE)
        scanned = unwatched_notebook(tmp_path / "scanned", exclude=["backup"])
        watched = unwatched_notebook(tmp_path / "watched", exclude=["backup"])
        handler = FileEventHandler(watched)

        for path in self.write_tree(watched.path, self.TREE):
            handler.on_created(FileCreatedEvent(str(path)))

        assert titles(scanned) == titles(watched) == ["a", "b", os.path.join("sub", "n")]
