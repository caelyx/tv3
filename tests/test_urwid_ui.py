"""Tests for urwid_ui.py.

MainFrame is built against a real notebook in a temporary directory, driven
through `keypress` and checked by rendering it. The editor is a real
executable (the `stub_editor` fixture). The only stand-in is the terminal:
`RecordingScreen` replaces urwid's display, recording stop/start and the
canvases urwid draws, so a real `urwid.MainLoop` can run headless.
"""

import os
import select
import signal
import sys
import termios
import threading
from pathlib import Path

import pytest
import urwid
from watchdog.events import FileCreatedEvent, FileDeletedEvent
from watchdog.observers.api import BaseObserver

from tests.ui_helpers import (
    FOCUSED,
    SIZE,
    ScriptedScreen,
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


def _runs(canvas):
    """The (attribute, text) runs of a one-row canvas, without trailing padding."""
    row = next(iter(canvas.content()))
    runs = [(attr, text.decode()) for attr, _, text in row]
    return [(attr, text) for attr, text in runs if text.strip()]


class TestAutocompleteWidget:
    def test_placeholder_when_empty(self):
        widget = urwid_ui.AutocompleteWidget()
        canvas = widget.render((20,))
        assert canvas.text == [b"Find or Create".ljust(20)]
        assert canvas.cursor == (0, 0)

    def test_completion_shown_after_typed_text(self):
        widget = urwid_ui.AutocompleteWidget(edit_text="hel")
        widget.autocomplete_text = "hello"

        canvas = widget.render((20,))
        assert _runs(canvas) == [("search", "hel"), ("autocomplete", "lo")]
        assert canvas.cursor == (3, 0)

    def test_completion_keeps_title_case(self):
        widget = urwid_ui.AutocompleteWidget(edit_text="HEL")
        widget.autocomplete_text = "hello"

        assert _runs(widget.render((20,))) == [("search", "hel"), ("autocomplete", "lo")]

    def test_non_prefix_selection_shows_whole_title_with_cursor_at_its_end(self):
        widget = urwid_ui.AutocompleteWidget(edit_text="xyz")
        widget.autocomplete_text = "hello"

        canvas = widget.render((20,))
        assert _runs(canvas) == [("autocomplete", "hello")]
        assert canvas.cursor == (5, 0)

    def test_completion_set_after_a_render_is_drawn_in_full(self):
        widget = urwid_ui.AutocompleteWidget(edit_text="sauce")
        widget.render((30,))

        widget.autocomplete_text = "recipes"

        assert widget.render((30,)).text == [b"recipes".ljust(30)]

    def test_text_longer_than_the_box_scrolls_to_the_cursor(self):
        widget = urwid_ui.AutocompleteWidget(edit_text="abcdefghij")
        widget.autocomplete_text = "abcdefghijklm"

        canvas = widget.render((8,))
        assert canvas.text == [b"defghijk"]
        assert canvas.cursor == (7, 0)

    def test_consume_turns_completion_into_text_with_cursor_at_end(self):
        widget = urwid_ui.AutocompleteWidget(edit_text="hel")
        widget.autocomplete_text = "hello"

        assert widget.consume()
        assert widget.edit_text == "hello"
        assert widget.edit_pos == 5
        assert widget.autocomplete_text is None

    def test_consume_without_completion(self):
        widget = urwid_ui.AutocompleteWidget(edit_text="hello")
        assert not widget.consume()
        assert widget.edit_text == "hello"


class TestSearchBoxMatchesSelection:
    """The search box always shows the current text and completion in full."""

    def test_body_match_selected_with_down(self, make_frame):
        frame = make_frame({"shopping list.txt": "", "recipes.txt": "pasta with foo sauce"})
        type_text(frame, "sauce")
        assert search_line(frame) == "sauce"

        press(frame, "down")

        assert highlighted(frame) == ["recipes"]
        assert search_line(frame) == "recipes"

    def test_title_longer_than_placeholder(self, make_frame):
        frame = make_frame({"a.txt": "", "a title much longer than the placeholder.txt": ""})
        assert search_line(frame) == "Find or Create"

        press(frame, "down")

        assert search_line(frame) == "a title much longer than the placeholder"

    def test_single_character_completes_to_full_title(self, make_frame):
        frame = make_frame({"zed.txt": "", "foo bar.txt": ""})
        type_text(frame, "f")
        assert search_line(frame) == "foo bar"

    def test_editing_inside_an_accepted_completion(self, make_frame):
        frame = make_frame({"zed.txt": "", "foo bar.txt": ""})
        type_text(frame, "fo")
        press(frame, "left", "left")
        type_text(frame, "x")

        assert frame.search_box.edit_text == "foo baxr"
        assert search_line(frame) == "foo baxr"

    def test_selection_changed_by_notebook_change(self, make_frame, notes_dir, stub_editor):
        frame = make_frame({"foo.txt": ""})
        frame.tv_notebook.close()
        type_text(frame, "fo")
        assert search_line(frame) == "foo"
        (notes_dir / "foo.txt").unlink()
        (notes_dir / "fourteen nights.txt").write_text("")
        handler = tv_notebook.FileEventHandler(frame.tv_notebook)

        def change(loop, _data):
            for event in (
                FileDeletedEvent(str(notes_dir / "foo.txt")),
                FileCreatedEvent(str(notes_dir / "fourteen nights.txt")),
            ):
                thread = threading.Thread(target=handler.dispatch, args=(event,))
                thread.start()
                thread.join()

        frame.loop.set_alarm_in(0, change)
        screen = frame.loop.screen
        assert run_headless(frame.loop, lambda: "fourteen nights" in "".join(screen.lines[1:2]))
        assert highlighted(frame) == ["fourteen nights"]
        assert search_line(frame) == "fourteen nights"

        press(frame, "enter")
        assert stub_editor.calls() == [[str(notes_dir / "fourteen nights.txt")]]


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

    def test_down_after_escape_selects_the_first_note(self, make_frame):
        frame = make_frame({"a.txt": "", "b.txt": "", "c.txt": ""})
        press(frame, "down", "down")
        assert highlighted(frame) == ["b"]

        press(frame, "esc")
        assert highlighted(frame) == []

        press(frame, "down")
        assert highlighted(frame) == ["c"]
        assert search_line(frame) == "c"

    def test_clicking_a_note_selects_it(self, make_frame):
        frame = make_frame({"a.txt": "", "b.txt": "", "c.txt": ""})
        assert list_lines(frame) == ["c", "b", "a"]

        frame.mouse_event(SIZE, "mouse press", 1, 5, 4, True)

        assert highlighted(frame) == ["b"]
        assert search_line(frame) == "b"

    def test_left_and_right_move_the_cursor_once_the_title_is_typed(self, make_frame):
        frame = make_frame({"foo.txt": ""})
        type_text(frame, "foo")
        assert highlighted(frame) == ["foo"]

        press(frame, "left")
        type_text(frame, "x")
        assert frame.search_box.edit_text == "foxo"

        press(frame, "esc")
        type_text(frame, "foo")
        press(frame, "left", "right")
        type_text(frame, "x")
        assert frame.search_box.edit_text == "foox"

    def test_tab_is_ignored_once_the_title_is_typed(self, make_frame):
        frame = make_frame({"foo.txt": ""})
        type_text(frame, "foo")

        assert frame.keypress(SIZE, "tab") == "tab"
        assert frame.search_box.edit_text == "foo"
        assert highlighted(frame) == ["foo"]


class TestTypingOnANavigatedNote:
    """With a note's title replacing the typed text, edit keys edit the title."""

    def test_typing_appends_to_the_title(self, make_frame):
        frame = make_frame({"foo.txt": "", "zed.txt": "foo"})
        type_text(frame, "foo")
        press(frame, "up")
        assert highlighted(frame) == ["zed"]
        assert search_line(frame) == "zed"

        type_text(frame, "d")

        assert frame.search_box.edit_text == "zedd"
        assert search_line(frame) == "zedd"

    def test_typing_after_down_from_an_empty_box_appends_to_the_title(self, make_frame):
        frame = make_frame({"a.txt": "", "zed.txt": ""})
        press(frame, "down")
        assert search_line(frame) == "zed"

        type_text(frame, "d")

        assert frame.search_box.edit_text == "zedd"

    def test_backspace_after_down_from_an_empty_box_edits_the_title(self, make_frame):
        frame = make_frame({"a.txt": "", "zed.txt": ""})
        press(frame, "down", "backspace")

        assert frame.search_box.edit_text == "ze"


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
        assert highlighted(frame) == ["ideas"]
        assert search_line(frame) == "ideas"

    def test_title_in_another_case_opens_and_shows_the_existing_note(
        self, make_frame, stub_editor, notes_dir
    ):
        (notes_dir / "probe").write_text("")
        if not (notes_dir / "PROBE").exists():
            pytest.skip("needs a case-insensitive filesystem")
        (notes_dir / "probe").unlink()
        frame = make_frame({"existing.txt": "", "other.txt": ""})
        type_text(frame, "Existing")
        assert highlighted(frame) == []

        press(frame, "enter")

        assert stub_editor.calls() == [[str(notes_dir / "existing.txt")]]
        assert highlighted(frame) == ["existing"]
        assert search_line(frame) == "existing"

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

    def test_message_is_one_distinct_row_on_a_small_terminal(self, make_frame):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        type_text(frame, ".hidden")
        press(frame, "enter")

        canvas = frame.render((20, 5), focus=True)
        content = list(canvas.content())
        assert {attr for attr, _, _ in content[-1]} == {"status"}
        assert all("status" not in {attr for attr, _, _ in row} for row in content[:-1])
        assert canvas.text[-1].decode().startswith("Invalid note title")


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

    def test_editor_killed_by_a_signal_is_reported(self, make_frame, stub_editor, monkeypatch):
        monkeypatch.setenv("STUB_EDITOR_SIGNAL", str(int(signal.SIGTERM)))
        frame = make_frame({"a.txt": ""})
        press(frame, "down", "enter")

        assert len(stub_editor.calls()) == 1
        assert status_line(frame) == f"Editor killed by signal {int(signal.SIGTERM)}"

    def test_failure_to_restore_the_screen_does_not_hide_the_editor_error(self, make_frame, caplog):
        frame = make_frame({"a.txt": ""}, editor="tv3-editor\0")
        frame.loop.screen.start_error = RuntimeError("terminal gone")
        press(frame, "down")

        with pytest.raises(ValueError, match="null"):
            frame.keypress(SIZE, "enter")
        assert "terminal gone" in caplog.text

    def test_interrupt_does_not_stop_the_editor_or_tv3(self, make_frame, tmp_path, notes_dir):
        log = tmp_path / "interrupt.log"
        editor = tmp_path / "interrupting-editor"
        editor.write_text(
            f"""#!{sys.executable}
import os, signal, time
with open({str(log)!r}, "a") as log:
    log.write(f"ignored={{signal.getsignal(signal.SIGINT) is signal.SIG_IGN}}\\n")
    log.flush()
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(0.5)
    log.write("done\\n")
"""
        )
        editor.chmod(0o755)
        handler = signal.getsignal(signal.SIGINT)
        frame = make_frame({"a.txt": ""}, editor=str(editor))
        press(frame, "down")

        try:
            press(frame, "enter")
        except KeyboardInterrupt:
            pytest.fail("Ctrl-C while the editor ran reached tv3")

        assert log.read_text().splitlines() == ["ignored=False", "done"]
        assert signal.getsignal(signal.SIGINT) is handler
        assert frame.footer is None

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

    def test_notification_racing_close_is_dropped(self, make_frame):
        frame = make_frame()
        notify = frame.tv_notebook.on_change

        frame.close()

        notify()

    def test_close_releases_pipe_and_stops_notifications(self, make_frame):
        open_fds = set(os.listdir("/dev/fd"))
        frame = make_frame()
        assert set(os.listdir("/dev/fd")) > open_fds

        frame.close()

        assert frame.tv_notebook.on_change is None
        assert set(os.listdir("/dev/fd")) == open_fds


@pytest.fixture
def pty_screen():
    """A urwid_ui.Screen on a pseudo-terminal, and the terminal's fd.

    The terminal starts with flow control on; its output is drained so
    writes never block.
    """
    controller, terminal = os.openpty()
    attrs = termios.tcgetattr(terminal)
    attrs[0] |= termios.IXON
    termios.tcsetattr(terminal, termios.TCSANOW, attrs)
    stop = threading.Event()

    def drain():
        while not stop.is_set():
            readable, _, _ = select.select([controller], [], [], 0.05)
            if readable:
                os.read(controller, 65536)

    drainer = threading.Thread(target=drain)
    drainer.start()
    with (
        open(terminal, "rb", closefd=False) as tty_in,
        open(terminal, "w", closefd=False) as tty_out,
    ):
        yield urwid_ui.Screen(tty_in, tty_out), terminal
    stop.set()
    drainer.join()
    os.close(terminal)
    os.close(controller)


def _flow_control(fd):
    return bool(termios.tcgetattr(fd)[0] & termios.IXON)


class TestScreen:
    def test_flow_control_is_off_while_started_and_restored_by_stop(self, pty_screen):
        screen, terminal = pty_screen
        before = termios.tcgetattr(terminal)

        screen.start()
        assert not _flow_control(terminal)

        screen.stop()
        assert termios.tcgetattr(terminal) == before

    def test_flow_control_is_off_again_after_restarting(self, pty_screen):
        screen, terminal = pty_screen
        screen.start()
        screen.stop()
        assert _flow_control(terminal)

        screen.start()
        assert not _flow_control(terminal)
        screen.stop()


def _observers():
    return {t for t in threading.enumerate() if isinstance(t, BaseObserver) and t.is_alive()}


class TestLaunch:
    """launch() with ScriptedScreen standing in for the terminal."""

    def _launch(self, monkeypatch, notes_dir, script):
        """Run launch() playing `script` on `self.screen`; return the screen."""

        def make_screen():
            self.screen = ScriptedScreen(script)
            return self.screen

        monkeypatch.setattr(urwid_ui, "Screen", make_screen)
        urwid_ui.launch(str(notes_dir), "true", ".txt", [".txt"])
        return self.screen

    @pytest.mark.slow
    def test_shows_notebook_changes_until_quit_then_stops_watching(
        self, monkeypatch, notes_dir, write_notes
    ):
        write_notes(notes_dir, {"a.txt": ""})
        observers = _observers()
        watching = []

        def create_note():
            watching.append(len(_observers() - observers))
            (notes_dir / "from outside.txt").write_text("")
            return True

        def note_shown():
            return any("from outside" in line for line in self.screen.lines)

        screen = self._launch(monkeypatch, notes_dir, [create_note, note_shown, "ctrl q"])

        assert screen.script == []
        assert watching == [1]
        assert [name for name, _ in screen.events] == ["start", "stop"]
        assert _observers() == observers

    def test_stops_watching_when_the_loop_fails(self, monkeypatch, notes_dir):
        observers = _observers()

        def fail():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            self._launch(monkeypatch, notes_dir, [fail])

        assert [name for name, _ in self.screen.events] == ["start", "stop"]
        assert _observers() == observers
