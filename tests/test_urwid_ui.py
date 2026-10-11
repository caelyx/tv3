"""Tests for urwid_ui.py.

MainFrame is built against a real notebook in a temporary directory, driven
through `keypress` and checked by rendering it. The editor is a real
executable (the `stub_editor` fixture). The only stand-in is the terminal:
`RecordingScreen` replaces urwid's display, recording stop/start and the
canvases urwid draws, so a real `urwid.MainLoop` can run headless.
"""

import os
import threading
from pathlib import Path

import pytest
import urwid
from watchdog.events import FileCreatedEvent, FileDeletedEvent

from tests.ui_helpers import (
    FOCUSED,
    SIZE,
    highlighted,
    list_lines,
    press,
    run_headless,
    screen_lines,
    search_line,
    status_line,
    type_text,
)
from tv3 import tv_notebook, urwid_ui


class TestNoteWidget:
    def test_renders_title_with_focus_attribute(self, notes_dir, notebook):
        nb = notebook(notes_dir)
        widget = urwid_ui.NoteWidget(tv_notebook.PlainTextNote("my note", nb, ".txt"))

        focused = widget.render((20,), focus=True)
        unfocused = widget.render((20,), focus=False)

        assert focused.text == [b"my note".ljust(20)]
        assert {attr for attr, _, _ in next(iter(focused.content()))} == {FOCUSED}
        assert {attr for attr, _, _ in next(iter(unfocused.content()))} == {"notewidget unfocused"}


class TestAutocompleteWidget:
    def test_placeholder_when_empty(self):
        widget = urwid_ui.AutocompleteWidget()
        canvas = widget.render((20,))
        assert canvas.text == [b"Find or Create".ljust(20)]

    def test_completion_shown_after_typed_text(self):
        widget = urwid_ui.AutocompleteWidget()
        widget.set_edit_text("hel")
        widget.autocomplete_text = "hello"

        assert widget.get_text() == ("hello", [("search", 3), ("autocomplete", 2)])

    def test_completion_keeps_title_case(self):
        widget = urwid_ui.AutocompleteWidget()
        widget.set_edit_text("HEL")
        widget.autocomplete_text = "hello"

        assert widget.get_text()[0] == "hello"

    def test_non_prefix_selection_shows_whole_title(self):
        widget = urwid_ui.AutocompleteWidget()
        widget.set_edit_text("xyz")
        widget.autocomplete_text = "hello"

        assert widget.get_text() == ("hello", [("autocomplete", 5)])

    def test_consume_turns_completion_into_text_with_cursor_at_end(self):
        widget = urwid_ui.AutocompleteWidget()
        widget.set_edit_text("hel")
        widget.autocomplete_text = "hello"

        assert widget.consume()
        assert widget.edit_text == "hello"
        assert widget.edit_pos == 5
        assert widget.autocomplete_text is None

    def test_consume_without_completion(self):
        widget = urwid_ui.AutocompleteWidget()
        widget.set_edit_text("hello")
        assert not widget.consume()
        assert widget.edit_text == "hello"


class TestFilteringAndRendering:
    def test_lists_notes_newest_first(self, make_frame):
        frame = make_frame({"old.txt": "", "middle.md": "", "new.txt": ""})
        assert list_lines(frame) == ["new", "middle", "old"]
        assert search_line(frame) == "Find or Create"
        assert highlighted(frame) == []

    def test_typing_filters_by_title_and_contents(self, make_frame):
        frame = make_frame({"apple.txt": "fruit", "banana.txt": "yellow fruit", "car.txt": ""})
        type_text(frame, "fruit")
        assert list_lines(frame) == ["banana", "apple"]

    def test_typing_autocompletes_newest_prefix_match(self, make_frame):
        frame = make_frame({"foo.txt": "", "foo bar.txt": "", "other.txt": "foo"})
        type_text(frame, "fo")
        assert search_line(frame) == "foo bar"
        assert highlighted(frame) == ["foo bar"]
        assert list_lines(frame) == ["other", "foo bar", "foo"]

    def test_unicode_search(self, make_frame):
        frame = make_frame({"greeting.txt": "こんにちは", "other.txt": "hello"})
        type_text(frame, "こんにちは")
        assert list_lines(frame) == ["greeting"]

    def test_no_match_placeholder(self, make_frame):
        frame = make_frame({"a.txt": ""})
        type_text(frame, "zzz")
        assert "No matching notes, press Enter to create a new note" in "\n".join(
            screen_lines(frame)
        )

    def test_empty_notebook_placeholder(self, make_frame):
        frame = make_frame()
        assert "You have no notes yet" in "\n".join(screen_lines(frame))


class TestEscapeAndQuit:
    def test_escape_clears_selection_then_search_then_quits(self, make_frame):
        frame = make_frame({"foo.txt": ""})
        type_text(frame, "fo")
        assert search_line(frame) == "foo"

        press(frame, "esc")
        assert search_line(frame) == "fo"
        assert highlighted(frame) == []

        press(frame, "esc")
        assert search_line(frame) == "Find or Create"
        assert list_lines(frame) == ["foo"]

        with pytest.raises(urwid.ExitMainLoop):
            press(frame, "esc")

    @pytest.mark.parametrize("key", ["ctrl q", "ctrl x"])
    def test_quit_keys(self, make_frame, key):
        frame = make_frame({"foo.txt": ""})
        type_text(frame, "fo")
        with pytest.raises(urwid.ExitMainLoop):
            press(frame, key)


class TestSelection:
    """The highlighted list row is the selection; Tab, Enter and the box agree."""

    def test_tab_completes_the_highlighted_note_and_enter_opens_it(
        self, make_frame, stub_editor, notes_dir
    ):
        frame = make_frame({"foo.txt": "", "foo bar.txt": ""})
        type_text(frame, "fo")
        press(frame, "down", "down")
        assert highlighted(frame) == ["foo"]

        press(frame, "tab")
        assert search_line(frame) == "foo"
        assert highlighted(frame) == ["foo"]

        press(frame, "enter")
        assert stub_editor.calls() == [[str(notes_dir / "foo.txt")]]

    def test_down_from_search_selects_first_note(self, make_frame):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        assert highlighted(frame) == []
        press(frame, "down")
        assert highlighted(frame) == ["b"]
        assert search_line(frame) == "b"
        press(frame, "down")
        assert highlighted(frame) == ["a"]
        assert search_line(frame) == "a"
        press(frame, "up")
        assert highlighted(frame) == ["b"]

    def test_selection_stays_on_opened_note_after_editor_returns(
        self, make_frame, stub_editor, notes_dir
    ):
        frame = make_frame({"a.txt": "", "b.txt": "", "c.txt": ""})
        press(frame, "down", "down")
        assert highlighted(frame) == ["b"]

        press(frame, "enter")
        assert highlighted(frame) == ["b"]
        assert search_line(frame) == "b"

        press(frame, "enter")
        assert stub_editor.calls() == [[str(notes_dir / "b.txt")]] * 2

    def test_edited_note_moves_to_top_and_stays_selected(
        self, make_frame, stub_editor, notes_dir, monkeypatch
    ):
        monkeypatch.setenv("STUB_EDITOR_APPEND", "edited")
        frame = make_frame({"a.txt": "", "b.txt": "", "c.txt": ""})
        press(frame, "down", "down", "down")
        assert highlighted(frame) == ["a"]

        press(frame, "enter")
        assert (notes_dir / "a.txt").read_text() == "edited"
        assert list_lines(frame) == ["a", "c", "b"]
        assert highlighted(frame) == ["a"]

    def test_page_down_moves_by_the_visible_list_height(self, make_frame):
        notes = {f"note{i:02d}.txt": "" for i in range(60)}
        frame = make_frame(notes)
        press(frame, "down")
        visible = list_lines(frame)
        assert visible[0] == "note59"
        assert len(visible) == SIZE[1] - 3

        press(frame, "page down")
        # One page below the first visible row is the first row that was hidden.
        assert highlighted(frame) == [f"note{59 - len(visible):02d}"]


class TestBackspace:
    def test_first_backspace_removes_only_the_completion(self, make_frame):
        frame = make_frame({"hello world.txt": ""})
        type_text(frame, "hel")
        assert search_line(frame) == "hello world"

        press(frame, "backspace")
        assert search_line(frame) == "hel"
        assert highlighted(frame) == []

        press(frame, "backspace")
        assert search_line(frame) == "he"
        assert highlighted(frame) == []

    def test_backspace_on_navigated_note_edits_its_title(self, make_frame):
        frame = make_frame({"apple.txt": "", "zebra.txt": "apple"})
        type_text(frame, "apple")
        press(frame, "down", "up")
        assert highlighted(frame) == ["zebra"]
        assert search_line(frame) == "zebra"

        press(frame, "backspace")
        assert search_line(frame) == "zebr"


class TestEnter:
    def test_enter_creates_note_and_opens_it(self, make_frame, stub_editor, notes_dir):
        frame = make_frame({"other.txt": ""})
        type_text(frame, "new note")
        press(frame, "enter")

        path = notes_dir / "new note.txt"
        assert path.is_file()
        assert stub_editor.calls() == [[str(path)]]
        assert highlighted(frame) == ["new note"]

    def test_typed_recognised_extension_is_kept(self, make_frame, stub_editor, notes_dir):
        frame = make_frame()
        type_text(frame, "ideas.md")
        press(frame, "enter")

        assert sorted(os.listdir(notes_dir)) == ["ideas.md"]
        assert stub_editor.calls() == [[str(notes_dir / "ideas.md")]]

    def test_default_extension_need_not_be_listed(self, make_frame, stub_editor, notes_dir):
        frame = make_frame(extension=".org", extensions=[".txt"])
        type_text(frame, "plan")
        press(frame, "enter")

        assert stub_editor.calls() == [[str(notes_dir / "plan.org")]]

    def test_existing_unselected_title_opens_the_note_in_the_notebook(
        self, make_frame, stub_editor, notes_dir, tmp_path, monkeypatch
    ):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        frame = make_frame({"foo.txt": "content"})
        type_text(frame, "foo")
        press(frame, "esc")
        assert highlighted(frame) == []

        press(frame, "enter")

        assert stub_editor.calls() == [[str(notes_dir / "foo.txt")]]
        assert (notes_dir / "foo.txt").read_text() == "content"
        assert list(elsewhere.iterdir()) == []

    def test_enter_with_nothing_typed_or_selected_does_nothing(self, make_frame, stub_editor):
        frame = make_frame({"a.txt": ""})
        press(frame, "enter")
        assert stub_editor.calls() == []
        assert frame.footer is None


class TestStatusLine:
    def test_invalid_title_is_reported_and_nothing_is_written(
        self, make_frame, stub_editor, notes_dir
    ):
        frame = make_frame()
        type_text(frame, "../escape")
        press(frame, "enter")

        assert status_line(frame) == "Invalid note title: '..' is not allowed: ../escape.txt"
        assert stub_editor.calls() == []
        assert list(notes_dir.iterdir()) == []
        assert not (notes_dir.parent / "escape.txt").exists()

    def test_message_clears_on_next_keypress(self, make_frame):
        frame = make_frame()
        type_text(frame, ".hidden")
        press(frame, "enter")
        assert status_line(frame).startswith("Invalid note title")
        assert len(screen_lines(frame)) == SIZE[1]

        press(frame, "x")
        assert frame.footer is None
        assert search_line(frame) == ".hiddenx"

    def test_filesystem_error_is_reported(self, make_frame, stub_editor, notes_dir):
        frame = make_frame()
        notes_dir.chmod(0o500)
        try:
            type_text(frame, "blocked")
            press(frame, "enter")
        finally:
            notes_dir.chmod(0o700)

        assert status_line(frame) == "Could not create blocked.txt: Permission denied"
        assert stub_editor.calls() == []


class TestEditor:
    def test_screen_is_stopped_while_the_editor_runs(self, make_frame, stub_editor, notes_dir):
        frame = make_frame({"a.txt": ""})
        screen = frame.loop.screen
        screen.probe = lambda: len(stub_editor.calls())
        press(frame, "down", "enter")

        assert screen.events == [("stop", 0), ("start", 1)]

    def test_editor_command_is_split_like_a_shell(
        self, make_frame, make_stub_editor, tmp_path, notes_dir
    ):
        editor = make_stub_editor(tmp_path / "my editors")
        frame = make_frame({"a b.txt": ""}, editor=f"{editor.command} --wait")
        press(frame, "down", "enter")

        assert editor.calls() == [["--wait", str(notes_dir / "a b.txt")]]

    def test_nonzero_exit_is_reported_and_screen_restored(
        self, make_frame, stub_editor, monkeypatch
    ):
        monkeypatch.setenv("STUB_EDITOR_EXIT", "3")
        frame = make_frame({"a.txt": ""})
        press(frame, "down", "enter")

        assert len(stub_editor.calls()) == 1
        assert status_line(frame) == "Editor exited with status 3"
        assert [name for name, _ in frame.loop.screen.events] == ["stop", "start"]
        assert highlighted(frame) == ["a"]

    def test_missing_editor_is_reported_and_screen_restored(self, make_frame):
        frame = make_frame({"a.txt": ""}, editor="tv3-no-such-editor")
        press(frame, "down", "enter")

        assert status_line(frame) == (
            "Could not run editor tv3-no-such-editor: No such file or directory"
        )
        assert [name for name, _ in frame.loop.screen.events] == ["stop", "start"]

    def test_unparseable_editor_command_is_reported(self, make_frame):
        frame = make_frame({"a.txt": ""}, editor="vim 'unbalanced")
        press(frame, "down", "enter")

        assert status_line(frame) == "Could not run editor vim 'unbalanced: No closing quotation"
        assert frame.loop.screen.events == []


class TestNotebookChanges:
    """Watcher-thread changes reach the screen through the main loop's pipe."""

    def _dispatch_from_thread(self, frame, event):
        handler = tv_notebook.FileEventHandler(frame.tv_notebook)
        thread = threading.Thread(target=handler.dispatch, args=(event,))
        thread.start()
        thread.join()

    def test_created_note_appears(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": ""})
        frame.tv_notebook.close()
        screen = frame.loop.screen
        path = notes_dir / "b.txt"
        path.write_text("")
        seen_before_refresh = []

        def change(loop, _data):
            self._dispatch_from_thread(frame, FileCreatedEvent(str(path)))
            seen_before_refresh.append(list_lines(frame))

        frame.loop.set_alarm_in(0, change)
        assert run_headless(frame.loop, lambda: "b" in [line.strip() for line in screen.lines])
        assert seen_before_refresh == [["a"]]
        assert list_lines(frame) == ["b", "a"]

    def test_deleted_note_disappears_and_is_not_reopened(self, make_frame, notes_dir, stub_editor):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        frame.tv_notebook.close()
        press(frame, "down")
        assert highlighted(frame) == ["b"]
        (notes_dir / "b.txt").unlink()

        def change(loop, _data):
            self._dispatch_from_thread(frame, FileDeletedEvent(str(notes_dir / "b.txt")))

        frame.loop.set_alarm_in(0, change)
        screen = frame.loop.screen
        assert run_headless(frame.loop, lambda: [s.strip() for s in screen.lines[3:4]] == ["a"])
        assert list_lines(frame) == ["a"]
        assert set(frame.list_box.widgets) == set(frame.tv_notebook)

        press(frame, "enter")
        assert not (notes_dir / "b.txt").exists()

    def test_selection_survives_unrelated_change(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": "", "b.txt": "", "c.txt": ""})
        frame.tv_notebook.close()
        press(frame, "down", "down")
        assert highlighted(frame) == ["b"]
        path = notes_dir / "d.txt"
        path.write_text("")
        os.utime(path, (1, 1))

        def change(loop, _data):
            self._dispatch_from_thread(frame, FileCreatedEvent(str(path)))

        frame.loop.set_alarm_in(0, change)
        screen = frame.loop.screen
        assert run_headless(frame.loop, lambda: "d" in [s.strip() for s in screen.lines])
        assert list_lines(frame) == ["c", "b", "a", "d"]
        assert highlighted(frame) == ["b"]

    @pytest.mark.slow
    def test_real_watcher_refreshes_running_loop(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": ""})
        screen = frame.loop.screen

        def create(loop, _data):
            Path(notes_dir, "from outside.txt").write_text("")

        frame.loop.set_alarm_in(0.05, create)
        assert run_headless(frame.loop, lambda: any("from outside" in s for s in screen.lines))

    def test_close_releases_pipe_and_stops_notifications(self, make_frame):
        open_fds = set(os.listdir("/dev/fd"))
        frame = make_frame()
        assert set(os.listdir("/dev/fd")) > open_fds

        frame.close()

        assert frame.tv_notebook.on_change is None
        assert set(os.listdir("/dev/fd")) == open_fds
