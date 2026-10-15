"""Tests for urwid_ui.py.

MainFrame is built against a real notebook in a temporary directory, driven
through `keypress` and checked by rendering it. The editor is a real
executable (the `stub_editor` fixture). The only stand-in is the terminal:
`RecordingScreen` replaces urwid's display, recording stop/start and the
canvases urwid draws, so a real `urwid_ui.MainLoop` can run headless.
"""

import contextlib
import os
import random
import select
import signal
import sys
import termios
import threading
import time
import unicodedata
from pathlib import Path

import pytest
import urwid
from watchdog.events import FileCreatedEvent, FileDeletedEvent, FileModifiedEvent
from watchdog.observers.api import BaseObserver

from tests.test_tv_notebook import HookedNoteBook
from tests.ui_helpers import (
    FOCUSED,
    SIZE,
    ScriptedScreen,
    attr_runs,
    feed,
    footer_rows,
    highlighted,
    list_lines,
    list_titles,
    marked,
    press,
    run_headless,
    screen_lines,
    search_line,
    status_line,
    type_text,
)
from tv3 import tv_notebook, urwid_ui
from tv3.ranking import prefix_length


class TestNoteWidget:
    def test_renders_title_with_focus_attribute(self, notes_dir, notebook, write_notes):
        write_notes(notes_dir, {"my note.txt": ""})
        nb = notebook(notes_dir)
        (result,) = nb.find("")
        widget = urwid_ui.NoteWidget(result)

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

    @pytest.mark.parametrize(
        ("typed", "drawn", "cursor"),
        [("\u0303", "", 0), ("\u0303\u0301", "", 0), ("a\u0303", "a\u0303", 1)],
    )
    def test_combining_characters_without_a_base_draw_as_nothing(self, typed, drawn, cursor):
        widget = urwid_ui.AutocompleteWidget(edit_text=typed)

        canvas = widget.render((20,))

        assert canvas.text[0].decode().rstrip() == drawn
        assert canvas.cursor == (cursor, 0)

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

    @pytest.mark.parametrize(
        ("typed", "completion", "accepted"),
        [
            ("rec", "recipes/Pasta sauce", "recipes/"),
            ("recipes", "recipes/Pasta sauce", "recipes/"),
            ("recipes/", "recipes/Pasta sauce", "recipes/Pasta sauce"),
            ("recipes/pa", "recipes/Pasta sauce", "recipes/Pasta sauce"),
            ("REC", "recipes/Pasta sauce", "recipes/"),
            ("a", "a/b/c", "a/"),
            ("a/", "a/b/c", "a/b/"),
            ("a/b/", "a/b/c", "a/b/c"),
            ("strasse", "Straße/x", "Straße/"),
            ("hel", "hello", "hello"),
            ("sauce", "recipes/Pasta sauce", "recipes/Pasta sauce"),
        ],
    )
    def test_consume_segment_stops_after_the_next_slash(self, typed, completion, accepted):
        widget = urwid_ui.AutocompleteWidget(edit_text=typed)
        widget.autocomplete_text = completion

        assert widget.consume(segment=True)
        assert widget.edit_text == accepted
        assert widget.edit_pos == len(accepted)


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
        assert list_lines(frame) == ["banana", "yellow fruit", "apple", "fruit"]

    def test_typing_autocompletes_the_top_row_newest_prefix_match_first(self, make_frame):
        frame = make_frame({"foo.txt": "", "foo bar.txt": "", "other.txt": "foo"})
        type_text(frame, "fo")
        assert search_line(frame) == "foo bar"
        assert highlighted(frame) == ["foo bar"]
        assert list_lines(frame) == ["foo bar", "foo", "other", "foo"]

    def test_exact_title_is_on_top_selected_and_opened_by_enter(
        self, make_frame, stub_editor, notes_dir
    ):
        frame = make_frame({"todo.txt": "", "todo work.txt": "", "todo house.txt": ""})
        type_text(frame, "todo")

        assert list_titles(frame) == ["todo", "todo house", "todo work"]
        assert highlighted(frame) == ["todo"]
        assert search_line(frame) == "todo"
        press(frame, "enter")
        assert stub_editor.calls() == [[str(notes_dir / "todo.txt")]]

    def test_prefix_match_on_top_is_selected(self, make_frame):
        frame = make_frame({"mtg retro.txt": "", "mtg with landlord.txt": "", "meeting.txt": "mtg"})
        type_text(frame, "mtg")

        assert list_titles(frame)[0] == "mtg with landlord"
        assert highlighted(frame) == ["mtg with landlord"]

    def test_decomposed_text_selects_the_composed_title(self, make_frame):
        frame = make_frame({"café notes.txt": "", "zed.txt": ""})
        type_text(frame, unicodedata.normalize("NFD", "café"))

        assert highlighted(frame) == ["café notes"]
        assert search_line(frame) == "café notes"

    def test_dotted_capital_i_title_is_not_appended_to(self, make_frame):
        frame = make_frame({"İstanbul trip.txt": "", "zed.txt": ""})
        type_text(frame, "i")
        assert highlighted(frame) == ["İstanbul trip"]
        assert search_line(frame) == "İstanbul trip"

        type_text(frame, "st")

        assert frame.search_box.edit_text == "ist"
        assert highlighted(frame) == []

    def test_title_of_only_a_combining_character(self, make_frame):
        frame = make_frame({"\u0303.txt": "", "zed.txt": ""})

        assert list_lines(frame) == ["zed"]

        press(frame, "down", "down")
        assert frame.selected_note.title == "\u0303"
        assert search_line(frame) == ""

    def test_unicode_search(self, make_frame):
        frame = make_frame({"greeting.txt": "こんにちは", "other.txt": "hello"})
        type_text(frame, "こんにちは")
        assert list_lines(frame) == ["greeting", "こんにちは"]

    def test_no_match_placeholder(self, make_frame):
        frame = make_frame({"a.txt": ""})
        type_text(frame, "zzz")
        assert "No matching notes, press Enter to create a new note" in "\n".join(
            screen_lines(frame)
        )

    def test_empty_notebook_placeholder(self, make_frame):
        frame = make_frame()
        assert "You have no notes yet" in "\n".join(screen_lines(frame))


class TestCaselessPrefix:
    """A title that starts with the typed text, ignoring case, is selected and
    shown as a completion of it, however case folding changes its length."""

    def test_typing_through_a_title_that_folds_longer(self, make_frame):
        frame = make_frame({"Straße.txt": "strasse plan", "zed.txt": ""})
        type_text(frame, "strasse")

        assert frame.search_box.edit_text == "strasse"
        assert highlighted(frame) == ["Straße"]
        assert search_line(frame) == "Straße"

    def test_typing_past_a_ligature_title(self, make_frame):
        frame = make_frame({"\ufb01le.txt": "file list", "zed.txt": ""})
        type_text(frame, "file")
        assert highlighted(frame) == ["\ufb01le"]

        type_text(frame, "s")

        assert frame.search_box.edit_text == "files"
        assert highlighted(frame) == []

    def test_backspace_removes_only_the_completion_of_a_folded_title(self, make_frame):
        frame = make_frame({"Straße.txt": "", "zed.txt": ""})
        type_text(frame, "strass")
        assert highlighted(frame) == ["Straße"]
        assert search_line(frame) == "Straße"

        press(frame, "backspace")

        assert frame.search_box.edit_text == "strass"
        assert highlighted(frame) == []
        assert search_line(frame) == "strass"

    def test_typed_text_in_another_case_opens_the_existing_note(
        self, make_frame, stub_editor, notes_dir
    ):
        frame = make_frame({"foo.txt": "", "zed.txt": ""})
        type_text(frame, "Foo")
        assert highlighted(frame) == ["foo"]

        press(frame, "enter")

        assert stub_editor.calls() == [[str(notes_dir / "foo.txt")]]
        assert sorted(p.name for p in notes_dir.iterdir()) == ["foo.txt", "zed.txt"]

    def test_single_character_in_another_case(self, make_frame):
        frame = make_frame({"ñ.txt": "", "zed.txt": ""})
        type_text(frame, "Ñ")

        assert list_titles(frame) == ["ñ"]
        assert highlighted(frame) == ["ñ"]

    ALPHABET = ("a", "A", "s", "S", "ß", "ẞ", "i", "I", "\u0130", "\u0131", "\ufb01", "f", "ǅ")
    ALPHABET += ("n", "\u0303", "ñ", "Ñ", "e", "\u0301", "é", "o", "J", "\u030c", "x")

    def test_selection_and_completion_agree_with_the_prefix_rule(self, make_frame, notes_dir):
        """Over random typing in a notebook of titles that case folding
        changes the length of: a note is selected exactly when a result is
        flagged `prefix`, which is exactly when `prefix_length` says its
        title starts with the typed text; the selected note is the top row
        and the search box shows it as a completion of the typed text; and
        the next key typed is added to the typed text unchanged.
        """
        rng = random.Random(11)

        def word():
            return "".join(rng.choice(self.ALPHABET) for _ in range(rng.randint(1, 5)))

        for i in range(40):
            title = unicodedata.normalize("NFC", " ".join(word() for _ in range(rng.randint(1, 2))))
            form = rng.choice(["NFC", "NFD"])
            with contextlib.suppress(FileExistsError, OSError):
                path = notes_dir / (unicodedata.normalize(form, title) + ".txt")
                with open(path, "x") as f:
                    f.write(word() + " " + title)
                os.utime(path, (1_700_000_000 + i, 1_700_000_000 + i))
        frame = make_frame()
        notes = list(frame.tv_notebook)
        assert len(notes) > 30

        selected = 0
        for _ in range(150):
            while frame.selected_note is not None or frame.search_box.edit_text:
                press(frame, "esc")
            if rng.random() < 0.6:
                title = rng.choice(notes).title
                typed = title[: rng.randint(1, min(5, len(title)))]
                typed = rng.choice([typed, typed.swapcase(), unicodedata.normalize("NFD", typed)])
            else:
                typed = word()
            type_text(frame, typed)
            context = (typed, frame.search_box.edit_text, frame.selected_note)

            assert frame.search_box.edit_text == typed, context
            results = frame.tv_notebook.find(typed)
            prefixed = {r.note for r in results if r.prefix}
            assert prefixed == {
                n for n in frame.tv_notebook if prefix_length(n.title, typed) is not None
            }, context
            note = frame.selected_note
            if prefixed:
                selected += 1
                assert note is results[0].note and note in prefixed, context
                assert list_titles(frame)[0] == note.title.strip(), context
                assert frame.search_box.completion_extends_text(), context
                assert search_line(frame) == note.title.strip(), context
            else:
                assert note is None, context
                assert search_line(frame) == typed.strip(), context

            extra = rng.choice(self.ALPHABET)
            type_text(frame, extra)
            assert frame.search_box.edit_text == typed + extra, context
        assert selected > 50


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
        assert highlighted(frame) == ["foo bar"]
        press(frame, "down")
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

    def test_tab_completes_one_folder_at_a_time(self, make_frame, stub_editor, notes_dir):
        frame = make_frame({"recipes/Pasta sauce.txt": "", "zed.txt": ""})
        type_text(frame, "rec")
        assert search_line(frame) == "recipes/Pasta sauce"

        press(frame, "tab")
        assert frame.search_box.edit_text == "recipes/"
        assert search_line(frame) == "recipes/Pasta sauce"
        assert highlighted(frame) == ["recipes/Pasta sauce"]

        press(frame, "tab")
        assert frame.search_box.edit_text == "recipes/Pasta sauce"

        press(frame, "enter")
        assert stub_editor.calls() == [[str(notes_dir / "recipes" / "Pasta sauce.txt")]]

    def test_typing_after_a_folder_creates_a_note_in_it(self, make_frame, stub_editor, notes_dir):
        frame = make_frame({"recipes/Pasta sauce.txt": ""})
        type_text(frame, "rec")
        press(frame, "tab")
        type_text(frame, "pesto")
        assert highlighted(frame) == []

        press(frame, "enter")

        assert stub_editor.calls() == [[str(notes_dir / "recipes" / "pesto.txt")]]

    @pytest.mark.parametrize("key", ["left", "right"])
    def test_left_and_right_complete_the_whole_title(self, make_frame, key):
        frame = make_frame({"recipes/Pasta sauce.txt": ""})
        type_text(frame, "rec")

        press(frame, key)

        assert frame.search_box.edit_text == "recipes/Pasta sauce"

    def test_tab_on_a_navigated_to_note_completes_the_whole_title(self, make_frame):
        frame = make_frame({"recipes/Pasta sauce.txt": "", "zed.txt": "sauce"})
        type_text(frame, "sauce")
        while highlighted(frame) != ["recipes/Pasta sauce"]:
            press(frame, "down")

        press(frame, "tab")

        assert frame.search_box.edit_text == "recipes/Pasta sauce"

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
        press(frame, "down")
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
        press(frame, "down")
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
        type_text(frame, "Existing ")
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

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
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

    def test_screen_resized_while_the_editor_runs_is_drawn_at_its_new_size(self, make_frame):
        frame = make_frame({"a.txt": ""})
        screen = frame.loop.screen

        def resize_while_stopped():
            screen.size = (100, 30)

        screen.probe = resize_while_stopped
        press(frame, "down", "enter")

        assert len(screen.lines) == 30
        assert {len(line) for line in screen.lines} == {100}

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


def _count_searches(monkeypatch, frame):
    """Record each query `frame`'s notebook searches for, still searching."""
    queries = []
    search = frame.tv_notebook.search

    def counting_search(query):
        queries.append(query)
        return search(query)

    monkeypatch.setattr(frame.tv_notebook, "search", counting_search)
    return queries


class TestInputBatches:
    """Keys that reach the main loop together refilter the list once."""

    def test_burst_of_typing_refilters_once_before_the_redraw(self, make_frame, monkeypatch):
        frame = make_frame({"foo.txt": "", "foo bar.txt": "", "other.txt": "foo"})
        press(frame, "f")
        assert highlighted(frame) == ["foo bar"]
        queries = _count_searches(monkeypatch, frame)

        drawn = feed(frame, "oo b")

        assert queries == ["foo b"]
        assert drawn[1].strip(" │┃") == "foo bar"
        assert [line.strip() for line in drawn[3:] if line.strip()] == ["foo bar"]
        assert highlighted(frame) == ["foo bar"]

    def test_single_key_is_filtered_in_its_first_redraw(self, make_frame, monkeypatch):
        frame = make_frame({"foo.txt": "", "bar.txt": ""})
        queries = _count_searches(monkeypatch, frame)

        drawn = feed(frame, "f")

        assert queries == ["f"]
        assert drawn[1].strip(" │┃") == "foo"
        assert [line.strip() for line in drawn[3:] if line.strip()] == ["foo"]

    def test_enter_straight_after_typing_opens_the_completed_note(
        self, make_frame, stub_editor, notes_dir
    ):
        frame = make_frame({"alpha.txt": "", "zeta.txt": ""})

        feed(frame, ["a", "l", "enter"])

        assert stub_editor.calls() == [[str(notes_dir / "alpha.txt")]]
        assert sorted(p.name for p in notes_dir.iterdir()) == ["alpha.txt", "zeta.txt"]
        assert highlighted(frame) == ["alpha"]

    def test_tab_straight_after_typing_completes_the_title(self, make_frame):
        frame = make_frame({"foo.txt": "", "foo bar.txt": ""})

        drawn = feed(frame, ["f", "o", "tab", "x"])

        assert frame.search_box.edit_text == "foo barx"
        assert drawn[1].strip(" │┃") == "foo barx"

    def test_tab_straight_after_typing_completes_the_folder(self, make_frame):
        frame = make_frame({"recipes/Pasta sauce.txt": "", "zed.txt": ""})

        feed(frame, ["r", "e", "c", "tab", "x"])

        assert frame.search_box.edit_text == "recipes/x"

    def test_backspace_straight_after_typing_removes_only_the_completion(self, make_frame):
        frame = make_frame({"foo bar.txt": "", "zed.txt": ""})

        feed(frame, ["f", "o", "backspace"])

        assert frame.search_box.edit_text == "fo"
        assert search_line(frame) == "fo"
        assert highlighted(frame) == []

    def test_click_straight_after_typing_picks_from_the_filtered_list(self, make_frame):
        frame = make_frame({"apple.txt": "fruit", "banana.txt": "fruit", "cherry.txt": ""})

        feed(frame, ["f", "r", "u", "i", "t", ("mouse press", 1, 5, 5)])

        assert list_lines(frame) == ["banana", "fruit", "apple", "fruit"]
        assert highlighted(frame) == ["apple"]

    @pytest.mark.parametrize("key", ["down", "page down"])
    def test_moving_straight_after_typing_moves_in_the_filtered_list(self, make_frame, key):
        frame = make_frame({"fob.txt": "", "foo.txt": "", "bar.txt": ""})

        drawn = feed(frame, ["f", "o", key])

        assert [line.strip() for line in drawn[3:] if line.strip()] == ["foo", "fob"]
        assert drawn[1].strip(" │┃") == "fob"
        assert highlighted(frame) == ["fob"]

    @pytest.mark.parametrize("key", ["esc", "ctrl d", "ctrl u"])
    def test_escape_straight_after_typing_clears_only_the_completion(self, make_frame, key):
        frame = make_frame({"fob.txt": "", "foo.txt": "", "bar.txt": ""})

        drawn = feed(frame, ["f", "o", key])

        assert frame.search_box.edit_text == "fo"
        assert drawn[1].strip(" │┃") == "fo"
        assert [line.strip() for line in drawn[3:] if line.strip()] == ["foo", "fob"]
        assert highlighted(frame) == []

    def test_first_screen_shows_a_refilter_left_pending_before_the_loop_ran(self, make_frame):
        frame = make_frame({"foo.txt": "", "bar.txt": ""})
        frame.keypress(SIZE, "f")
        screen = frame.loop.screen

        assert run_headless(frame.loop, lambda: bool(screen.drawn))

        first = screen.drawn[0]
        assert first[1].strip(" │┃") == "foo"
        assert [line.strip() for line in first[3:] if line.strip()] == ["foo"]

    def test_typing_burst_on_a_navigated_note_appends_to_its_title(self, make_frame):
        frame = make_frame({"zeta.txt": "", "alpha.txt": ""})
        press(frame, "down", "down")
        assert highlighted(frame) == ["zeta"]

        drawn = feed(frame, "xy")

        assert frame.search_box.edit_text == "zetaxy"
        assert drawn[1].strip(" │┃") == "zetaxy"

    def test_notebook_change_while_typing_joins_the_same_refilter(
        self, make_frame, notes_dir, monkeypatch
    ):
        frame = make_frame({"foo.txt": "", "bar.txt": ""})
        frame.tv_notebook.close()
        queries = _count_searches(monkeypatch, frame)
        path = notes_dir / "fourteen.txt"
        path.write_text("")
        handler = tv_notebook.FileEventHandler(frame.tv_notebook)

        def change():
            thread = threading.Thread(target=handler.dispatch, args=(FileCreatedEvent(str(path)),))
            thread.start()
            thread.join()

        drawn = feed(frame, ["f", "o"], before=change)

        assert queries == ["fo"]
        assert drawn[1].strip(" │┃") == "fourteen"
        assert [line.strip() for line in drawn[3:] if line.strip()] == ["fourteen", "foo"]
        assert highlighted(frame) == ["fourteen"]


@pytest.fixture
def held_read(monkeypatch):
    """Make frames' notebooks hold their background read until the returned gate is set."""
    gate = threading.Event()

    class HeldNoteBook(HookedNoteBook):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, warmer_gate=gate, **kwargs)

    monkeypatch.setattr(tv_notebook, "PlainTextNoteBook", HeldNoteBook)
    yield gate
    gate.set()


def _list_rows(drawn):
    return [line.strip() for line in drawn[3:] if line.strip()]


class TestBackgroundRead:
    """Until the notebook has read every note, typing is drawn without waiting for it."""

    def test_typing_is_drawn_at_once_and_refiltered_when_the_read_finishes(
        self, make_frame, held_read
    ):
        frame = make_frame({"foo.txt": "", "bar.txt": ""}, wait_for_read=False)
        screen = frame.loop.screen

        drawn = feed(frame, ["f", "o"])

        assert not frame.tv_notebook.loaded
        assert drawn[1].strip(" │┃") == "fo"
        assert _list_rows(drawn) == ["bar", "foo"]

        held_read.set()

        assert run_headless(frame.loop, lambda: _list_rows(screen.lines) == ["foo"])
        assert screen.lines[1].strip(" │┃") == "foo"
        assert highlighted(frame) == ["foo"]

    def test_search_without_words_is_refiltered_without_waiting(
        self, make_frame, held_read, monkeypatch
    ):
        frame = make_frame({"foo.txt": "", "bar.txt": ""}, wait_for_read=False)
        queries = _count_searches(monkeypatch, frame)

        drawn = feed(frame, [" "])

        assert not frame.tv_notebook.loaded
        assert queries == [" "]
        assert _list_rows(drawn) == ["bar", "foo"]

    def test_enter_waits_for_the_read_and_opens_the_matching_note(
        self, make_frame, held_read, stub_editor, notes_dir
    ):
        frame = make_frame({"alpha.txt": "", "zeta.txt": ""}, wait_for_read=False)

        feed(frame, ["a", "l", "enter"], before=threading.Timer(0.05, held_read.set).start)

        assert stub_editor.calls() == [[str(notes_dir / "alpha.txt")]]
        assert sorted(p.name for p in notes_dir.iterdir()) == ["alpha.txt", "zeta.txt"]
        assert highlighted(frame) == ["alpha"]

    def test_enter_waits_for_the_read_and_creates_a_new_note(
        self, make_frame, held_read, stub_editor, notes_dir
    ):
        frame = make_frame({"alpha.txt": "", "zeta.txt": ""}, wait_for_read=False)

        feed(frame, ["a", "x", "enter"], before=threading.Timer(0.05, held_read.set).start)

        assert stub_editor.calls() == [[str(notes_dir / "ax.txt")]]
        assert sorted(p.name for p in notes_dir.iterdir()) == ["alpha.txt", "ax.txt", "zeta.txt"]
        assert highlighted(frame) == ["ax"]


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


def _dispatch_from_thread(frame, event):
    """Deliver a watchdog `event` to `frame`'s notebook on another thread, as the watcher does."""
    handler = tv_notebook.FileEventHandler(frame.tv_notebook)
    thread = threading.Thread(target=handler.dispatch, args=(event,))
    thread.start()
    thread.join()


def _watcher_event_in_loop(frame, event, until):
    """Deliver `event` from inside `frame`'s running main loop; run it until `until()`."""
    frame.loop.set_alarm_in(0, lambda *_: _dispatch_from_thread(frame, event))
    assert run_headless(frame.loop, until)


class TestHighlighting:
    """Matched characters of titles and snippets are drawn with the match attribute."""

    def test_query_words_in_a_title_are_marked(self, make_frame):
        frame = make_frame({"foo bar.txt": "", "zed.txt": ""})
        type_text(frame, "bar")

        assert list_lines(frame) == ["foo bar"]
        assert [run for run in attr_runs(frame)[3] if run[0] is not None] == [
            ("notewidget unfocused", "foo "),
            ("match", "bar"),
            ("notewidget unfocused", " " * 71),
        ]

    def test_selected_note_uses_the_focused_match_attribute(self, make_frame):
        frame = make_frame({"foo bar.txt": "", "zed.txt": ""})
        type_text(frame, "bar")
        press(frame, "down")

        assert highlighted(frame) == ["foo bar"]
        assert marked(frame, 3) == ["bar"]
        assert {attr for attr, _ in attr_runs(frame)[3]} == {None, FOCUSED, "match focused"}

    def test_fuzzy_title_match_marks_the_matched_characters(self, make_frame):
        frame = make_frame({"apple banana.txt": "", "zed.txt": ""})
        type_text(frame, "apbn")

        assert list_lines(frame) == ["apple banana"]
        assert "".join(marked(frame, 3)) == "apbn"

    def test_matches_in_contents_get_a_snippet_line(self, make_frame):
        frame = make_frame({"recipes.txt": "intro\n  pasta with tomato sauce  \nend", "z.txt": ""})
        type_text(frame, "sauce")

        assert list_lines(frame) == ["recipes", "pasta with tomato sauce"]
        snippet = attr_runs(frame)[4]
        assert ("match", "sauce") in snippet
        assert {attr for attr, _ in snippet} == {None, "snippet", "match"}

    def test_selected_snippet_uses_focused_attributes(self, make_frame):
        frame = make_frame({"recipes.txt": "pasta sauce", "z.txt": ""})
        type_text(frame, "sauce")
        press(frame, "down")

        assert highlighted(frame) == ["recipes"]
        assert ("match focused", "sauce") in attr_runs(frame)[4]
        assert "snippet focused" in {attr for attr, _ in attr_runs(frame)[4]}

    def test_title_only_match_has_no_snippet(self, make_frame):
        frame = make_frame({"sauce ideas.txt": "sauce sauce", "z.txt": ""})
        type_text(frame, "sauce")
        assert list_lines(frame) == ["sauce ideas"]

    def test_long_titles_and_snippets_are_clipped_to_one_row(self, make_frame):
        title = "a long title " * 10
        frame = make_frame({f"{title.strip()}.txt": "word " * 40 + "needle " + "word " * 40})
        type_text(frame, "needle")

        lines = screen_lines(frame)
        assert lines[3].strip() == title[:78].strip()
        assert "needle" in lines[4]
        assert lines[5].strip() == ""
        assert marked(frame, 4) == ["needle"]

    def test_selection_is_the_top_row(self, make_frame):
        frame = make_frame({"fox.txt": "", "fox and hound.txt": ""})
        type_text(frame, "fo")
        assert list_lines(frame) == ["fox and hound", "fox"]
        assert highlighted(frame) == ["fox and hound"]

        type_text(frame, "x")

        assert list_lines(frame) == ["fox", "fox and hound"]
        assert highlighted(frame) == ["fox"]
        assert search_line(frame) == "fox"

    @pytest.mark.parametrize(("title", "typed"), [("Existing", "eXi"), ("existing", "Exi")])
    def test_prefix_in_another_case_is_selected(self, make_frame, title, typed):
        frame = make_frame({title + ".txt": "", "zed.txt": ""})
        type_text(frame, typed)

        assert list_lines(frame) == [title]
        assert highlighted(frame) == [title]
        assert search_line(frame) == title

    def test_fuzzy_match_with_another_case_is_not_autoselected(self, make_frame):
        frame = make_frame({"an existing note.txt": "", "zed.txt": ""})
        type_text(frame, "Exi")

        assert list_lines(frame) == ["an existing note"]
        assert highlighted(frame) == []
        assert search_line(frame) == "Exi"

    def test_fuzzy_match_does_not_change_what_enter_creates(
        self, make_frame, stub_editor, notes_dir
    ):
        frame = make_frame({"apple banana.txt": ""})
        type_text(frame, "apbn")
        assert highlighted(frame) == []

        press(frame, "enter")

        assert stub_editor.calls() == [[str(notes_dir / "apbn.txt")]]

    def test_a_note_in_a_folder_is_not_selected_by_its_name_alone(
        self, make_frame, stub_editor, notes_dir
    ):
        (notes_dir / "recipes").mkdir()
        frame = make_frame({os.path.join("recipes", "Pasta sauce.txt"): ""})
        type_text(frame, "pasta")
        assert list_lines(frame) == [os.path.join("recipes", "Pasta sauce")]
        assert highlighted(frame) == []

        press(frame, "enter")

        assert stub_editor.calls() == [[str(notes_dir / "pasta.txt")]]

    def test_only_visible_rows_are_built(self, make_frame, monkeypatch):
        frame = make_frame({f"note{i:03d}.txt": "body" for i in range(300)})
        built = []
        init = urwid_ui.NoteWidget.__init__

        def counting_init(self, result):
            built.append(result.note.title)
            init(self, result)

        type_text(frame, "bod")
        monkeypatch.setattr(urwid_ui.NoteWidget, "__init__", counting_init)
        type_text(frame, "y")

        assert 0 < len(built) <= SIZE[1]


def _snippet_row(frame, row, divider="│"):
    """Screen row `row` of the list, left of any preview divider, without padding."""
    return screen_lines(frame)[row].split(divider)[0].strip()


class TestSnippetWidth:
    """A snippet fits the list's width, so the match in it is on screen."""

    BODY = "a" * 150 + " needle " + "b" * 150

    def test_snippet_fits_a_narrow_screen(self, make_frame):
        frame = make_frame({"recipes.txt": self.BODY}, size=(28, 24))
        type_text(frame, "needle")

        snippet = _snippet_row(frame, 4)
        assert "needle" in snippet
        assert snippet.startswith("…")
        assert snippet.endswith("…")
        assert marked(frame, 4) == ["needle"]

    def test_snippet_fits_the_list_beside_the_preview(self, make_frame):
        frame = make_frame({"recipes.txt": self.BODY}, size=(100, 24))
        type_text(frame, "needle")
        press(frame, "ctrl p")

        snippet = _snippet_row(frame, 4)
        assert "needle" in snippet
        assert snippet.endswith("…")

        press(frame, "ctrl p")
        assert len(_snippet_row(frame, 4)) > 80

    def test_wide_characters_before_the_match_leave_it_on_screen(self, make_frame):
        frame = make_frame(
            {"日記.txt": "日本語の文章" * 30 + " needle " + "b" * 50}, size=(100, 24)
        )
        press(frame, "ctrl p")
        type_text(frame, "needle")

        assert "needle" in _snippet_row(frame, 4)

    def test_wide_characters_after_the_match_leave_the_end_ellipsis_on_screen(self, make_frame):
        frame = make_frame({"日記.txt": "日本語" * 30 + " needle " + "日本語" * 30}, size=(40, 24))
        type_text(frame, "needle")

        snippet = _snippet_row(frame, 4)
        assert snippet.startswith("…")
        assert snippet.endswith("…")
        assert marked(frame, 4) == ["needle"]

    def test_snippet_fills_the_list_above_the_preview(self, make_frame):
        cols = urwid_ui.PREVIEW_BESIDE_MIN_COLS - 1
        frame = make_frame({"recipes.txt": self.BODY}, size=(cols, 24))
        type_text(frame, "needle")
        press(frame, "ctrl p")

        snippet = _snippet_row(frame, 4)
        assert len(snippet) == cols - 2 * urwid_ui.LIST_PADDING - len(urwid_ui.SNIPPET_INDENT)
        assert "needle" in snippet
        assert any(set(line.strip()) == {"─"} for line in screen_lines(frame)[5:])

    def test_snippet_follows_a_resized_screen(self, make_frame):
        frame = make_frame({"recipes.txt": self.BODY})
        type_text(frame, "needle")
        assert len(_snippet_row(frame, 4)) > 60

        frame.loop.screen.size = (30, 24)
        frame.loop.draw_screen()

        snippet = _snippet_row(frame, 4)
        assert "needle" in snippet
        assert snippet.endswith("…")


class TestPalette:
    """Snippets are legible on any background, and the focused row's snippet stands out."""

    @staticmethod
    def _spec(name, colors):
        """The AttrSpec a terminal with `colors` colours draws palette entry `name` with."""
        _, fg, bg, *rest = next(e for e in urwid_ui.palette if e[0] == name)
        if colors > 16 and rest:
            fg, bg = rest[1:]
        return urwid.AttrSpec(fg, bg, colors)

    def test_snippet_is_the_default_colour_on_16_colour_terminals(self):
        spec = self._spec("snippet", 16)
        assert spec.foreground.split(",")[0] == "default"
        assert spec.background == "default"

    def test_snippet_is_a_mid_grey_on_256_colour_terminals(self):
        spec = self._spec("snippet", 256)
        grey = spec.foreground.split(",")[0]
        assert grey.startswith("g")
        assert 40 <= int(grey[1:]) <= 65
        assert spec.background == "default"

    @pytest.mark.parametrize("colors", [16, 256])
    def test_focused_snippet_differs_from_the_focused_title(self, colors):
        snippet = self._spec("snippet focused", colors)
        title = self._spec("notewidget focused", colors)
        assert snippet != title
        assert snippet.background == title.background

    def test_prompt_differs_from_the_status_message(self):
        assert self._spec("prompt", 16) != self._spec("status", 16)


def _select(frame, title):
    """Select the note titled `title` by moving down from an empty search."""
    press(frame, "down")
    while highlighted(frame) != [title]:
        press(frame, "down")


class TestRename:
    def test_ctrl_r_prompts_with_the_title_and_enter_renames(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": "", "old name.md": "body"})
        _select(frame, "old name")
        press(frame, "ctrl r")
        assert status_line(frame) == "Rename: old name"

        press(frame, *["backspace"] * 4)
        type_text(frame, "title")
        assert status_line(frame) == "Rename: old title"
        press(frame, "enter")

        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt", "old title.md"]
        assert (notes_dir / "old title.md").read_text() == "body"
        assert frame.footer is None
        assert highlighted(frame) == ["old title"]
        assert search_line(frame) == "old title"

    def test_renamed_note_is_selected_even_if_it_no_longer_matches(self, make_frame, notes_dir):
        frame = make_frame({"apple.txt": "", "banana.txt": ""})
        type_text(frame, "ban")
        assert highlighted(frame) == ["banana"]

        press(frame, "ctrl r", *["backspace"] * 6)
        type_text(frame, "cherry")
        press(frame, "enter")

        assert highlighted(frame) == ["cherry"]
        assert search_line(frame) == "cherry"
        assert sorted(p.name for p in notes_dir.iterdir()) == ["apple.txt", "cherry.txt"]

    def test_escape_cancels(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        _select(frame, "a")
        press(frame, "ctrl r")
        type_text(frame, "xyz")

        press(frame, "esc")

        assert frame.footer is None
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt", "b.txt"]
        assert highlighted(frame) == ["a"]
        assert search_line(frame) == "a"

    def test_keys_go_to_the_prompt_not_the_search_box(self, make_frame, stub_editor):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        _select(frame, "b")
        press(frame, "ctrl r")

        press(frame, "down", "x", "ctrl k", "ctrl p", "left", "y")

        assert status_line(frame) == "Rename: byx"
        assert highlighted(frame) == ["b"]
        assert search_line(frame) == "b"
        assert stub_editor.calls() == []

    def test_cursor_is_in_the_prompt(self, make_frame):
        frame = make_frame({"abc.txt": ""})
        _select(frame, "abc")
        press(frame, "ctrl r", "left")

        canvas = frame.render(SIZE, focus=True)

        assert canvas.cursor == (len("Rename: ab"), SIZE[1] - 1)

    def test_error_is_shown_above_the_prompt_which_stays_open(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        _select(frame, "b")
        press(frame, "ctrl r", "backspace")
        type_text(frame, "a")

        press(frame, "enter")

        (error, error_attrs), (prompt, prompt_attrs) = footer_rows(frame)
        assert error.startswith("Note already exists")
        assert error_attrs - {None} == {"status"}
        assert prompt == "Rename: a"
        assert prompt_attrs == {"prompt"}
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt", "b.txt"]
        assert highlighted(frame) == ["b"]

        type_text(frame, "2")
        assert [text for text, _ in footer_rows(frame)] == ["Rename: a2"]
        press(frame, "enter")
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt", "a2.txt"]
        assert frame.footer is None
        assert highlighted(frame) == ["a2"]

    def test_invalid_title_is_reported(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": ""})
        _select(frame, "a")
        press(frame, "ctrl r", "backspace")
        type_text(frame, "../escape")

        press(frame, "enter")

        assert footer_rows(frame)[0][0].startswith("Invalid note title")
        assert status_line(frame) == "Rename: ../escape"
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt"]

    def test_ctrl_u_clears_the_prompt(self, make_frame):
        frame = make_frame({"old name.txt": ""})
        _select(frame, "old name")
        press(frame, "ctrl r", "ctrl u")
        assert status_line(frame) == "Rename:"

        type_text(frame, "new")
        assert status_line(frame) == "Rename: new"
        assert search_line(frame) == "old name"

    def test_prompt_has_its_own_attribute(self, make_frame):
        frame = make_frame({"a.txt": ""})
        _select(frame, "a")
        press(frame, "ctrl r")

        assert footer_rows(frame) == [("Rename: a", {"prompt"})]

    @pytest.mark.parametrize("key", ["ctrl q", "ctrl x"])
    def test_quit_keys_quit_from_the_prompt(self, make_frame, notes_dir, key):
        frame = make_frame({"a.txt": ""})
        _select(frame, "a")
        press(frame, "ctrl r", "x")

        with pytest.raises(urwid.ExitMainLoop):
            frame.keypress(SIZE, key)
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt"]

    def test_mouse_is_ignored_while_prompting(self, make_frame):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        _select(frame, "b")
        press(frame, "ctrl r")

        assert frame.mouse_event(SIZE, "mouse press", 1, 5, 4, True) is False

        assert highlighted(frame) == ["b"]
        assert status_line(frame) == "Rename: b"

    def test_without_a_selection_shows_a_hint(self, make_frame):
        frame = make_frame({"a.txt": ""})

        press(frame, "ctrl r")

        assert status_line(frame) == "Select a note to rename it"
        press(frame, "x")
        assert frame.footer is None
        assert frame.search_box.edit_text == "x"

    def test_prompt_survives_a_notebook_change(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        frame.tv_notebook.close()
        _select(frame, "a")
        press(frame, "ctrl r", "1")
        path = notes_dir / "c.txt"
        path.write_text("")

        screen = frame.loop.screen
        _watcher_event_in_loop(
            frame,
            FileCreatedEvent(str(path)),
            lambda: "c" in [s.strip() for s in screen.lines[3:6]],
        )

        assert status_line(frame) == "Rename: a1"
        press(frame, "2", "enter")
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a12.txt", "b.txt", "c.txt"]
        assert highlighted(frame) == ["a12"]

    def test_note_deleted_while_prompting_is_reported(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        frame.tv_notebook.close()
        _select(frame, "b")
        press(frame, "ctrl r")
        (notes_dir / "b.txt").unlink()

        screen = frame.loop.screen
        _watcher_event_in_loop(
            frame,
            FileDeletedEvent(str(notes_dir / "b.txt")),
            lambda: "no longer exists" in screen.lines[-1],
        )

        assert status_line(frame) == "'b' no longer exists"
        assert list_lines(frame) == ["a"]
        press(frame, "x")
        assert frame.footer is None
        assert frame.search_box.edit_text == "x"
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt"]

    def test_note_deleted_before_the_watcher_noticed_is_reported(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        frame.tv_notebook.close()
        _select(frame, "b")
        press(frame, "ctrl r", "x")
        (notes_dir / "b.txt").unlink()

        press(frame, "enter")

        assert footer_rows(frame)[0][0].startswith("Could not rename b.txt to bx.txt")
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt"]

    @pytest.mark.parametrize("key, answer", [("ctrl r", "enter"), ("ctrl k", "y")])
    def test_note_gone_before_the_change_reached_the_loop_is_reported(
        self, make_frame, notes_dir, key, answer
    ):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        frame.tv_notebook.close()
        _select(frame, "b")
        press(frame, key)
        (notes_dir / "b.txt").unlink()
        _dispatch_from_thread(frame, FileDeletedEvent(str(notes_dir / "b.txt")))

        frame.keypress(SIZE, answer)

        assert status_line(frame) == "'b' no longer exists"
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt"]

    @pytest.mark.parametrize("key", ["ctrl r", "ctrl k"])
    def test_note_replaced_while_prompting_closes_the_prompt(self, make_frame, notes_dir, key):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        frame.tv_notebook.close()
        _select(frame, "b")
        press(frame, key)
        path = notes_dir / "b.txt"
        screen = frame.loop.screen
        drawn = len(screen.drawn)

        def replace(*_):
            path.unlink()
            _dispatch_from_thread(frame, FileDeletedEvent(str(path)))
            path.write_text("new")
            _dispatch_from_thread(frame, FileCreatedEvent(str(path)))

        frame.loop.set_alarm_in(0, replace)
        assert run_headless(frame.loop, lambda: len(screen.drawn) > drawn + 1)

        assert status_line(frame) == "'b' changed on disk"
        press(frame, "y")
        assert frame.search_box.edit_text == "by"
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt", "b.txt"]
        assert (notes_dir / "b.txt").read_text() == "new"


class TestDelete:
    def test_ctrl_k_asks_and_y_deletes_selecting_the_next_note(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": "", "b.txt": "", "c.txt": ""})
        press(frame, "down", "down")
        assert highlighted(frame) == ["b"]

        press(frame, "ctrl k")
        assert status_line(frame) == "Delete 'b'? (y/N)"
        press(frame, "y")

        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt", "c.txt"]
        assert status_line(frame) == "Deleted 'b'"
        assert list_lines(frame) == ["c", "a"]
        assert highlighted(frame) == ["a"]
        assert search_line(frame) == "a"

    def test_deleting_the_last_note_in_the_list_selects_the_one_above(self, make_frame):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        press(frame, "down", "down")
        assert highlighted(frame) == ["a"]

        press(frame, "ctrl k", "y")

        assert list_lines(frame) == ["b"]
        assert highlighted(frame) == ["b"]

    def test_deleting_the_only_match_leaves_nothing_selected(self, make_frame, notes_dir):
        frame = make_frame({"apple.txt": "", "banana.txt": ""})
        type_text(frame, "ban")
        assert highlighted(frame) == ["banana"]

        press(frame, "ctrl k", "y")

        assert sorted(p.name for p in notes_dir.iterdir()) == ["apple.txt"]
        assert highlighted(frame) == []
        assert search_line(frame) == "ban"
        assert "No matching notes" in "\n".join(screen_lines(frame))

    def test_note_that_left_the_list_while_asked_about_is_deleted(self, make_frame, notes_dir):
        frame = make_frame({"apple.txt": "fruit", "zed.txt": "fruit"})
        frame.tv_notebook.close()
        type_text(frame, "fruit")
        press(frame, "down", "ctrl k")
        assert status_line(frame) == "Delete 'zed'? (y/N)"
        path = notes_dir / "zed.txt"
        path.write_text("vegetable")

        _watcher_event_in_loop(
            frame, FileModifiedEvent(str(path)), lambda: list_titles(frame) == ["apple"]
        )
        press(frame, "y")

        assert sorted(p.name for p in notes_dir.iterdir()) == ["apple.txt"]
        assert status_line(frame) == "Deleted 'zed'"
        assert highlighted(frame) == []

    def test_capital_y_deletes(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        press(frame, "down", "ctrl k", "Y")

        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt"]
        assert status_line(frame) == "Deleted 'b'"
        press(frame, "x")
        assert frame.footer is None

    @pytest.mark.parametrize("key", ["ctrl q", "ctrl x"])
    def test_quit_keys_quit_from_the_question(self, make_frame, notes_dir, key):
        frame = make_frame({"a.txt": ""})
        press(frame, "down", "ctrl k")

        with pytest.raises(urwid.ExitMainLoop):
            frame.keypress(SIZE, key)
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt"]

    @pytest.mark.parametrize("key", ["n", "enter", "esc", "ctrl k"])
    def test_any_other_key_cancels_and_is_not_handled_otherwise(
        self, make_frame, notes_dir, stub_editor, key
    ):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        press(frame, "down")

        press(frame, "ctrl k", key)

        assert frame.footer is None
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt", "b.txt"]
        assert highlighted(frame) == ["b"]
        assert search_line(frame) == "b"
        assert stub_editor.calls() == []

    def test_without_a_selection_shows_a_hint(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": ""})

        press(frame, "ctrl k")

        assert status_line(frame) == "Select a note to delete it"
        press(frame, "y")
        assert frame.search_box.edit_text == "y"
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt"]

    def test_note_deleted_while_asking_is_reported(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": "", "b.txt": ""})
        frame.tv_notebook.close()
        press(frame, "down", "ctrl k")
        (notes_dir / "b.txt").unlink()

        screen = frame.loop.screen
        _watcher_event_in_loop(
            frame,
            FileDeletedEvent(str(notes_dir / "b.txt")),
            lambda: "no longer exists" in screen.lines[-1],
        )

        assert status_line(frame) == "'b' no longer exists"
        press(frame, "y")
        assert frame.search_box.edit_text == "y"
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt"]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_filesystem_error_is_reported(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": ""})
        press(frame, "down", "ctrl k")
        notes_dir.chmod(0o500)
        try:
            press(frame, "y")
        finally:
            notes_dir.chmod(0o700)

        assert status_line(frame) == "Could not delete a.txt: Permission denied"
        assert list_lines(frame) == ["a"]
        assert highlighted(frame) == ["a"]


WIDE = (120, 30)


class TestPreview:
    def test_ctrl_p_shows_the_selected_note_beside_the_list_on_a_wide_screen(self, make_frame):
        frame = make_frame({"a.txt": "alpha body", "b.txt": "bravo body\nsecond line"}, size=WIDE)
        press(frame, "down", "ctrl p")

        lines = screen_lines(frame)
        assert len(lines[3]) == WIDE[0]
        assert lines[3].split() == ["b", "│", "bravo", "body"]
        assert lines[4].split() == ["a", "│", "second", "line"]
        assert lines[3].index("bravo") > WIDE[0] // 2 - 2

    def test_preview_is_below_the_list_on_a_narrow_screen(self, make_frame):
        frame = make_frame({"a.txt": "alpha body", "b.txt": "bravo body"})
        press(frame, "down", "ctrl p")

        lines = [line.strip() for line in screen_lines(frame)]
        divider = next(i for i, line in enumerate(lines) if line and set(line) == {"─"})
        assert lines[3:5] == ["b", "a"]
        assert 5 < divider < SIZE[1] - 2
        assert lines[divider + 1] == "bravo body"

    def test_preview_follows_the_selection(self, make_frame):
        frame = make_frame({"a.txt": "alpha body", "b.txt": "bravo body"}, size=WIDE)
        press(frame, "down", "ctrl p")
        assert "bravo body" in screen_lines(frame)[3]

        press(frame, "down")
        assert "alpha body" in screen_lines(frame)[3]
        press(frame, "esc")
        assert "bravo body" in screen_lines(frame)[3]
        type_text(frame, "a")
        assert highlighted(frame)[0].startswith("a ")
        assert "alpha body" in screen_lines(frame)[3]

    def test_without_a_selection_the_top_note_is_shown(self, make_frame):
        frame = make_frame({"a.txt": "alpha body", "b.txt": "bravo body"}, size=WIDE)
        press(frame, "ctrl p")

        assert highlighted(frame) == []
        assert screen_lines(frame)[3].split() == ["b", "│", "bravo", "body"]

        type_text(frame, "alpha")
        assert highlighted(frame) == []
        assert screen_lines(frame)[3].split() == ["a", "│", "alpha", "body"]

    def test_empty_list_shows_a_placeholder(self, make_frame):
        frame = make_frame({"a.txt": "alpha body"}, size=WIDE)
        press(frame, "ctrl p")
        type_text(frame, "zzz")

        text = "\n".join(screen_lines(frame))
        assert "Nothing to preview" in text
        assert "alpha body" not in text

    def test_a_huge_single_line_note_does_not_slow_the_ui(self, make_frame):
        frame = make_frame({"a.txt": "", "huge.txt": "x" * (2 << 20)}, size=WIDE)
        press(frame, "ctrl p")
        assert screen_lines(frame)[3].split()[2].startswith("xxxx")

        start = time.monotonic()
        press(frame, "down", "down", "up", "down", "up")
        elapsed = time.monotonic() - start

        assert highlighted(frame)[0].startswith("huge ")
        assert elapsed < 0.5

    def test_ctrl_p_again_hides_the_preview(self, make_frame):
        frame = make_frame({"a.txt": "alpha body"}, size=WIDE)
        press(frame, "down", "ctrl p", "ctrl p")

        assert list_lines(frame) == ["a"]
        assert highlighted(frame) == ["a"]

    def test_preview_shows_the_first_screenful_without_control_characters(self, make_frame):
        body = "\n".join(f"line\t{i} {'x' * 100}\r" for i in range(200))
        frame = make_frame({"a.txt": body}, size=WIDE)
        press(frame, "down", "ctrl p")

        lines = screen_lines(frame)
        assert all(len(line) == WIDE[0] for line in lines)
        assert "line    0 " in lines[3]
        assert "\r" not in "".join(lines)

    def test_list_keys_and_clicks_still_work_with_the_preview(self, make_frame):
        notes = {f"note{i:02d}.txt": f"body {i}" for i in range(60)}
        frame = make_frame(notes)
        press(frame, "ctrl p", "down", "page down")
        assert highlighted(frame)[0] != "note59"
        press(frame, "esc", "down")
        assert highlighted(frame) == ["note59"]

        frame.mouse_event(SIZE, "mouse press", 1, 5, 4, True)

        assert highlighted(frame) == ["note58"]
        assert "body 58" in "\n".join(screen_lines(frame))

    def test_preview_shows_changes_from_the_watcher(self, make_frame, notes_dir):
        frame = make_frame({"a.txt": "old body"}, size=WIDE)
        frame.tv_notebook.close()
        press(frame, "down", "ctrl p")
        path = notes_dir / "a.txt"
        path.write_text("new body")
        os.utime(path, (2_000_000_000, 2_000_000_000))

        screen = frame.loop.screen
        _watcher_event_in_loop(
            frame, FileModifiedEvent(str(path)), lambda: "new body" in screen.lines[3]
        )
        assert [line.split() for line in highlighted(frame)] == [["a", "│", "new", "body"]]

    def test_preview_with_an_empty_notebook_shows_the_empty_placeholder(self, make_frame):
        frame = make_frame(size=WIDE)
        press(frame, "ctrl p")
        assert "You have no notes yet" in "\n".join(screen_lines(frame))
