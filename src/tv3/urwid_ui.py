"""A console user interface for Terminal Velocity."""

import contextlib
import logging
import os
import shlex
import signal
import subprocess
import sys
import termios
import threading
import typing
import unicodedata
from typing import NamedTuple

import urwid
import urwid.display.raw
from urwid import text_layout
from urwid.canvas import apply_text_layout

from . import ranking, tv_notebook

logger = logging.getLogger("tv3")

# Color palette for the UI. Entries with six items give the colours for
# 256-colour terminals last; 16-colour terminals use the first pair.
palette = [
    ("autocomplete", "black", "brown"),
    ("match", "default,bold,underline", "default"),
    ("match focused", "black,bold,underline", "brown"),
    ("notewidget focused", "black", "brown"),
    ("notewidget unfocused", "default", "default"),
    ("placeholder", "dark blue", "default"),
    ("prompt", "black", "light gray"),
    ("search", "default", "default"),
    ("snippet", "default,italics", "default", None, "g50,italics", "default"),
    ("snippet focused", "black,italics", "brown", None, "g19,italics", "brown"),
    ("status", "white", "dark red"),
]

EDIT_KEYS = ("backspace", "delete", "home", "end")

QUIT_KEYS = ("ctrl q", "ctrl x")

SNIPPET_INDENT = "  "

PREVIEW_BESIDE_MIN_COLS = 100

LIST_PADDING = 1


def completed_length(completion, typed):
    """How many characters of `completion` `typed` spells out, ignoring case, or None if
    `typed` does not start `completion`.

    `typed` is compared in Unicode normalisation form NFC, as titles are.
    Lowercasing can lengthen a character (U+0130 becomes "i" and a combining
    dot), so the count is of `completion`'s characters, not `typed`'s; a
    character whose lowercase form `typed` only starts counts.
    """
    typed = unicodedata.normalize("NFC", typed).lower()
    if not completion.lower().startswith(typed):
        return None
    length = covered = 0
    while covered < len(typed):
        covered += len(completion[length].lower())
        length += 1
    return length


def placeholder_text(text):
    """Return a widget showing `text` centred in the placeholder style."""
    text_widget = urwid.Text(("placeholder", text), align="center")
    return urwid.Filler(text_widget)


def marked_up(text, spans, attr=None):
    """Markup for `text`, with the characters in `spans` in the match style and the rest in `attr`."""
    markup = []
    end = 0
    for start, stop in spans:
        if start > end:
            markup.append((attr, text[end:start]))
        markup.append(("match", text[start:stop]))
        end = stop
    if end < len(text) or not markup:
        markup.append((attr, text[end:]))
    return markup


class NoteWidget(urwid.WidgetWrap):
    """A note in the list: its title with matches marked, and a snippet line if the result has one."""

    FOCUSED: typing.ClassVar[dict[str | None, str]] = {
        None: "notewidget focused",
        "match": "match focused",
        "snippet": "snippet focused",
    }
    UNFOCUSED: typing.ClassVar[dict[str | None, str]] = {None: "notewidget unfocused"}

    def __init__(self, result):
        self.result = result
        lines = [urwid.Text(marked_up(result.note.title, result.title_spans), wrap=urwid.CLIP)]
        if result.snippet is not None:
            snippet = [SNIPPET_INDENT, *marked_up(result.snippet, result.snippet_spans)]
            lines.append(urwid.AttrMap(urwid.Text(snippet, wrap=urwid.CLIP), "snippet"))
        super().__init__(urwid.Pile(lines))

    @property
    def note(self):
        return self.result.note

    def selectable(self):
        return True

    def keypress(self, size, key):
        """Pass through all keypresses to parent."""
        return key

    def render(self, size, focus=False):
        """Render in the focused or unfocused note style."""
        canv = urwid.CompositeCanvas(super().render(size, focus=focus))
        canv.fill_attr_apply(self.FOCUSED if focus else self.UNFOCUSED)
        return canv


class AutocompleteWidget(urwid.Edit):
    """A one-line text editing widget that can show a completion of the typed text.

    The widget draws its own display: the typed text, a completion over it,
    or a placeholder. Edit's text, cursor and layout only ever describe the
    typed text. The cursor is always shown.
    """

    placeholder = "Find or Create"

    def __init__(self, *args, **kwargs):
        self._autocomplete_text = None
        super().__init__(*args, wrap=urwid.CLIP, **kwargs)

    def get_autocomplete_text(self):
        return self._autocomplete_text

    def set_autocomplete_text(self, text):
        self._autocomplete_text = text
        self._invalidate()

    autocomplete_text = property(get_autocomplete_text, set_autocomplete_text)

    def completion_extends_text(self):
        """Whether the completion continues the non-empty typed text, ignoring case."""
        typed = self.edit_text
        completion = self.autocomplete_text
        return (
            bool(typed)
            and completion is not None
            and completed_length(completion, typed) is not None
        )

    def _display(self):
        """The markup to draw, and the cursor's index into its text.

        A completion that extends the typed text is shown after it, with the
        cursor at the typed position; any other completion replaces it, with
        the cursor at its end.
        """
        typed = self.edit_text
        completion = self.autocomplete_text
        if completion is None:
            if typed:
                return typed, self.edit_pos
            return ("placeholder", self.placeholder), 0
        end = completed_length(completion, typed) if typed else None
        if end is not None:
            cursor = completed_length(completion, typed[: self.edit_pos])
            return [
                ("search", completion[:end]),
                ("autocomplete", completion[end:]),
            ], self.edit_pos if cursor is None else cursor
        return ("autocomplete", completion), len(completion)

    def render(self, size, focus=False):
        """Render the display on one row, scrolled to keep the cursor visible."""
        (maxcol,) = size
        markup, cursor = self._display()
        text, attrs = urwid.decompose_tagmarkup(markup)
        layout = self.layout.layout(text, maxcol, urwid.LEFT, urwid.CLIP)
        x, y = text_layout.calc_coords(text, layout, cursor)
        if x >= maxcol:
            layout = [text_layout.shift_line(layout[0], maxcol - 1 - x)]
            x = maxcol - 1
        canvas = urwid.CompositeCanvas(apply_text_layout(text, attrs, layout, maxcol))
        canvas.cursor = (x, y)
        return canvas

    def consume(self):
        """Turn the completion into typed text, with the cursor at the end.

        Returns:
            True if there was a completion that differed from the typed text.
        """
        text = self.autocomplete_text
        if text is None or text == self.edit_text:
            return False
        self.set_edit_text(text)
        self.set_edit_pos(len(text))
        self.autocomplete_text = None
        return True


class ResultWalker(urwid.ListWalker):
    """The rows of the note list, built from search results as the list box shows them."""

    def __init__(self):
        self._results = []
        self._widgets = {}
        self._positions = None
        self.focus = 0

    def set_results(self, results):
        self._results = results
        self._widgets = {}
        self._positions = None
        self.focus = 0
        self._modified()

    def position_of(self, note):
        """The position of `note`, or None if it is not in the list."""
        if self._positions is None:
            self._positions = {result.note: i for i, result in enumerate(self._results)}
        return self._positions.get(note)

    def __len__(self):
        return len(self._results)

    def __getitem__(self, position):
        widget = self._widgets.get(position)
        if widget is None:
            if not 0 <= position < len(self._results):
                raise IndexError(position)
            widget = NoteWidget(self._results[position])
            self._widgets[position] = widget
        return widget

    def next_position(self, position):
        if position + 1 >= len(self._results):
            raise IndexError(position)
        return position + 1

    def prev_position(self, position):
        if position <= 0:
            raise IndexError(position)
        return position - 1

    def set_focus(self, position):
        self.focus = position
        self._modified()


class NoteFilterListBox(urwid.ListBox):
    """A filterable list of notes from a notebook."""

    def __init__(self, on_changed=None):
        """Initialise a new NoteFilterListBox.

        Args:
            on_changed: Optional callback, called with the focused note after
                a keypress or mouse event.
        """
        self._fake_focus = False
        self.list_walker = ResultWalker()
        super().__init__(self.list_walker)
        self.on_changed = on_changed

    def get_selected_note(self):
        """The note in focus, or None if the list is empty."""
        if self.focus is not None:
            return self.focus.note
        return None

    selected_note = property(get_selected_note)

    def get_fake_focus(self):
        return self._fake_focus

    def set_fake_focus(self, value):
        self._fake_focus = value
        self._invalidate()

    fake_focus = property(get_fake_focus, set_fake_focus)

    def render(self, size, focus=False):
        """Render the list, highlighting the focus only when `fake_focus` is set."""
        if len(self.list_walker) == 0:
            placeholder = placeholder_text("No matching notes, press Enter to create a new note")
            return placeholder.render(size)
        return super().render(size, self.fake_focus)

    def filter(self, results):
        """Show rows for `results`, in order, focusing the first."""
        self.list_walker.set_results(results)
        if results:
            self.focus_position = 0

    def neighbour(self, note):
        """The note after `note` in the list, else the one before, else None."""
        position = self.list_walker.position_of(note)
        if position is None:
            return None
        for other in (position + 1, position - 1):
            if 0 <= other < len(self.list_walker):
                return self.list_walker[other].note
        return None

    def focus_note(self, note):
        """Focus the row for `note`, if it is in the list.

        Returns:
            Whether `note` is in the list.
        """
        position = self.list_walker.position_of(note)
        if position is None:
            return False
        self.focus_position = position
        return True

    def keypress(self, size, key):
        result = super().keypress(size, key)
        if self.on_changed and self.selected_note:
            self.on_changed(self.selected_note)
        return result

    def mouse_event(self, size, event, button, col, row, focus):
        result = super().mouse_event(size, event, button, col, row, focus)
        if self.on_changed and self.selected_note:
            self.on_changed(self.selected_note)
        return result


class Preview(urwid.Widget):
    """The start of a note's contents as plain text, as much as fits.

    Only as much of each line as could fit is laid out, so a note with a
    huge line draws as fast as any other.
    """

    _sizing = frozenset([urwid.BOX])
    _selectable = False

    def __init__(self):
        super().__init__()
        self._note = None

    def set_note(self, note):
        """Show `note`, or a placeholder if None; redraw even if it is the note shown."""
        self._note = note
        self._invalidate()

    def render(self, size, focus=False):
        maxcol, maxrow = size
        if self._note is None:
            return placeholder_text("Nothing to preview").render(size)
        shown = []
        rows_left = maxrow
        for line in self._note.contents.split("\n", maxrow)[:maxrow]:
            if rows_left <= 0:
                break
            limit = maxcol * rows_left
            line = ranking.printable(line[:limit].expandtabs(4)[:limit])
            shown.append(line)
            rows_left -= max(1, -(-len(line) // maxcol))
        return urwid.Filler(urwid.Text("\n".join(shown)), valign=urwid.TOP).render(size)


class PreviewSplit(urwid.Widget):
    """A list with a preview pane: beside it on wide screens, below it on narrow ones."""

    _sizing = frozenset([urwid.BOX])
    _selectable = True
    no_cache: typing.ClassVar[list[str]] = ["render"]

    def __init__(self, main, preview):
        super().__init__()
        self._beside = urwid.Columns([main, (1, urwid.SolidFill("│")), preview])
        self._below = urwid.Pile([main, (1, urwid.SolidFill("─")), preview])

    def _layout(self, size):
        return self._beside if size[0] >= PREVIEW_BESIDE_MIN_COLS else self._below

    def main_cols(self, maxcol):
        """The width of the list at screen width `maxcol`."""
        if maxcol >= PREVIEW_BESIDE_MIN_COLS:
            return self._beside.column_widths((maxcol,))[0]
        return maxcol

    def render(self, size, focus=False):
        return self._layout(size).render(size, focus)

    def keypress(self, size, key):
        return self._layout(size).keypress(size, key)

    def mouse_event(self, size, event, button, col, row, focus):
        return self._layout(size).mouse_event(size, event, button, col, row, focus)


class PendingFilter(NamedTuple):
    """A refilter left to run later: the note to keep selected, and whether to autoselect."""

    keep: tv_notebook.PlainTextNote | None
    autoselect: bool


class Prompt(NamedTuple):
    """A question about `note` in the status line, which gets every key until it closes."""

    note: tv_notebook.PlainTextNote
    widget: urwid.Widget
    on_key: typing.Callable[[tuple[int, int], str], None]


class MainFrame(urwid.Frame):
    """The topmost urwid widget.

    The selected note is the list's focused note while the list has fake
    focus; the search box's completion always shows its title. A footer
    shows a message until the next keypress, or a prompt until it closes.

    Typing does not refilter at once: it clears the selection and leaves a
    refilter pending, which `flush_filter()` runs. `MainLoop` runs it before
    each redraw, once the loop has handled all waiting input, so a burst of
    keys refilters once. Any other key or mouse event runs a pending
    refilter before it is handled.

    Until the notebook has read every note, a search with any words would
    wait for the read, so the redraw leaves such a refilter pending and the
    typed text is drawn at once. The notebook's change notification when
    the read finishes wakes the loop, and the next redraw runs it. A key or
    mouse event that needs the refilter waits for the read.

    While a prompt is open, keys other than the quit keys go to it and
    mouse events are ignored. Notebook changes still refilter the list; one
    that removes or replaces the prompt's note closes the prompt.

    Snippets are as wide as the list. When its width changes, the next
    redraw refilters.

    The preview shows the selected note, or the top row's note when there is
    no selection.
    """

    def __init__(
        self,
        notes_dir,
        editor,
        extension,
        extensions,
        exclude=None,
    ):
        """Initialize the main frame.

        Args:
            notes_dir: Path to the notes directory.
            editor: Editor command line, split like a shell would split it;
                the note's path is appended as the last argument.
            extension: Default extension for new notes.
            extensions: List of extensions to recognize.
            exclude: Optional list of directories to exclude.
        """
        self.editor = editor
        self.tv_notebook = tv_notebook.PlainTextNoteBook(
            notes_dir,
            extension,
            extensions,
            exclude=exclude,
        )
        self.loop = None
        self._pending_filter = None
        self._snippet_cols = None
        self._prompt = None
        self._wake_fd = None
        self._wake_lock = threading.Lock()
        self.search_box = AutocompleteWidget()
        self.list_box = NoteFilterListBox(on_changed=self.select)
        self.preview = Preview()
        self.preview_shown = False
        self._list_body = urwid.Padding(self.list_box, left=LIST_PADDING, right=LIST_PADDING)
        self._split_body = PreviewSplit(
            self._list_body, urwid.Padding(self.preview, left=LIST_PADDING, right=LIST_PADDING)
        )
        self._empty_body = placeholder_text(
            "You have no notes yet, to create a note type a note title then press Enter"
        )
        super().__init__(
            header=urwid.LineBox(self.search_box),
            body=self._empty_body,
            focus_part="body",
        )
        self.filter(self.search_box.edit_text)

    def attach(self, loop):
        """Run inside `loop`: refresh from notebook changes and use its screen."""
        self.loop = loop
        self._wake_fd = loop.watch_pipe(self._on_notebook_changed)
        os.set_blocking(self._wake_fd, False)
        self.tv_notebook.on_change = self._wake

    def close(self):
        """Stop the file watcher and release the main loop pipe."""
        self.tv_notebook.on_change = None
        self.tv_notebook.close()
        with self._wake_lock:
            if self._wake_fd is not None:
                self.loop.remove_watch_pipe(self._wake_fd)
                os.close(self._wake_fd)
                self._wake_fd = None

    def _wake(self):
        """Wake the main loop, unless closed. Called on the watcher thread, so touches no widgets."""
        with self._wake_lock:
            if self._wake_fd is None:
                return
            # A full pipe already holds a pending wake-up.
            with contextlib.suppress(BlockingIOError):
                os.write(self._wake_fd, b"\0")

    def _on_notebook_changed(self, _data):
        prompt = self._prompt
        if prompt is not None:
            problem = self._stale(prompt.note)
            if problem is not None:
                self._close_prompt()
                self.show_message(problem)
        self._refilter_later()
        return True

    def _refilter_later(self):
        """Leave a refilter pending, if none is, that keeps the selection."""
        if self._pending_filter is None:
            selected = self.selected_note
            self._pending_filter = PendingFilter(selected, selected is not None)

    def _stale(self, note):
        """Why `note` is no longer the notebook's note for its file, or None if it is."""
        if note not in self.tv_notebook:
            return f"'{note.title}' no longer exists"
        if not any(current is note for current in self.tv_notebook):
            return f"'{note.title}' changed on disk"
        return None

    def _snippet_width(self):
        """The width of snippets: the list's, less the snippet indent."""
        if self.loop is None:
            return ranking.SNIPPET_WIDTH
        cols, _ = self.loop.screen.get_cols_rows()
        if self.body is self._split_body:
            cols = self._split_body.main_cols(cols)
        return max(1, cols - 2 * LIST_PADDING - len(SNIPPET_INDENT))

    def refilter_if_resized(self):
        """Leave a refilter pending if the search has words, so the list may show
        snippets, and the list's width changed since the last refilter."""
        if self.search_box.edit_text.split() and self._snippet_cols != self._snippet_width():
            self._refilter_later()

    def flush_filter(self, wait=True):
        """Run the pending refilter, if any.

        Unless `wait`, a refilter whose search would wait for the notebook's
        background read stays pending.
        """
        pending = self._pending_filter
        if pending is None:
            return
        query = self.search_box.edit_text
        if not wait and query.split() and not self.tv_notebook.loaded:
            return
        self.filter(query, keep=pending.keep, autoselect=pending.autoselect)

    @property
    def selected_note(self):
        """The selected note, or None."""
        if self.list_box.fake_focus:
            return self.list_box.selected_note
        return None

    def select(self, note):
        """Select `note` (which must be in the list), or clear the selection.

        Clearing the selection moves the list's focus back to its first row.
        """
        if note is not None:
            self.list_box.focus_note(note)
        elif len(self.list_box.list_walker):
            self.list_box.focus_position = 0
        self.list_box.fake_focus = note is not None
        selected = self.selected_note
        self.search_box.autocomplete_text = selected.title if selected else None
        self.preview.set_note(self.list_box.selected_note)

    def show_message(self, text):
        """Show `text` in the one-row status line until the next keypress."""
        self.footer = urwid.Text(("status", text), wrap=urwid.ELLIPSIS)

    def _open_prompt(self, note, widget, on_key):
        """Show `widget` in the status line and send it every key until it closes."""
        self._prompt = Prompt(note, widget, on_key)
        self._show_prompt()

    def _show_prompt(self, error=None):
        """Show the open prompt, with `error` on the row above it if given."""
        prompt = urwid.AttrMap(self._prompt.widget, "prompt")
        if error is None:
            self.footer = prompt
        else:
            message = urwid.Text(("status", error), wrap=urwid.ELLIPSIS)
            self.footer = urwid.Pile([message, prompt], focus_item=1)
        self.focus_position = "footer"

    def _close_prompt(self):
        self._prompt = None
        self.footer = None

    def quit(self):
        raise urwid.ExitMainLoop()

    def keypress(self, size, key):
        """Handle keypress events.

        Returns:
            Key if not handled, None if handled.
        """
        if key in QUIT_KEYS:
            self.quit()
        if self._prompt is not None:
            self._prompt.on_key(size, key)
            return None
        self.footer = None
        # Typing onto unselected text only extends the pending refilter.
        if not (self.search_box.valid_char(key) and self.selected_note is None):
            self.flush_filter()
        selected = self.selected_note
        if key in ["esc", "ctrl d", "ctrl u"]:
            if selected is not None:
                self.select(None)
            elif self.search_box.edit_text:
                self.set_search_text("")
            else:
                self.quit()
        elif key == "enter":
            self.open_or_create()
        elif key == "ctrl r":
            self.ask_rename(selected)
        elif key == "ctrl k":
            self.ask_delete(selected)
        elif key == "ctrl p":
            self.preview_shown = not self.preview_shown
            self._update_body()
        elif selected is not None and key in ["tab", "left", "right"]:
            if self.search_box.consume():
                self.filter(self.search_box.edit_text, keep=selected)
            else:
                return self._edit_search(size, key)
        elif key == "down" and not self.list_box.fake_focus:
            self.select(self.list_box.selected_note)
        elif key in ["down", "up", "page up", "page down"]:
            return super().keypress(size, key)
        elif key == "backspace":
            return self._backspace(size)
        else:
            return self._edit_search(size, key)
        return None

    def mouse_event(self, size, event, button, col, row, focus):
        if self._prompt is not None:
            return False
        self.flush_filter()
        return super().mouse_event(size, event, button, col, row, focus)

    def _edit_search(self, size, key, autoselect=True):
        """Pass `key` to the search box and leave a refilter pending if its text changed.

        When the selected note's title is shown in place of the typed text,
        an editing key edits the title.
        """
        before = self.search_box.edit_text
        if (
            (self.search_box.valid_char(key) or key in EDIT_KEYS)
            and self.selected_note is not None
            and not self.search_box.completion_extends_text()
        ):
            self.search_box.consume()
        result = self.header.keypress((size[0],), key)
        if self.search_box.edit_text != before:
            self.select(None)
            self._pending_filter = PendingFilter(None, autoselect)
        return result

    def _backspace(self, size):
        """Remove a completion shown after the typed text, else delete a character.

        Deleting never autoselects a note.
        """
        box = self.search_box
        if (
            self.selected_note is not None
            and box.completion_extends_text()
            and len(box.autocomplete_text) > len(box.edit_text)
        ):
            self.select(None)
            return None
        return self._edit_search(size, "backspace", autoselect=False)

    def set_search_text(self, text, keep=None):
        """Replace the search text, with the cursor at the end, and refilter."""
        self.search_box.set_edit_text(text)
        self.search_box.set_edit_pos(len(text))
        self.filter(text, keep=keep)

    def open_or_create(self):
        """Open the selected note, or create and open a note titled as typed.

        Afterwards the note is selected; if it does not match the typed text,
        the search text becomes its title.
        """
        note = self.selected_note
        if note is None:
            title = self.search_box.edit_text
            if not title:
                return
            try:
                note = self.tv_notebook.create(title)
            except tv_notebook.NoteAlreadyExistsError as e:
                note = e.note
            except tv_notebook.NewNoteError as e:
                self.show_message(str(e))
                return
        self.edit(note)
        self.tv_notebook.refresh(note)
        self._show_selected(note)

    def _show_selected(self, note):
        """Refilter and select `note`; if it does not match the typed text, search for its title."""
        self.filter(self.search_box.edit_text, keep=note)
        if self.selected_note != note:
            self.set_search_text(note.title, keep=note)

    def ask_rename(self, note):
        """Prompt for a new title for `note`, prefilled with its title."""
        if note is None:
            self.show_message("Select a note to rename it")
            return
        edit = urwid.Edit("Rename: ", note.title, wrap=urwid.CLIP)
        self._open_prompt(note, edit, self._rename_key)

    def _rename_key(self, size, key):
        prompt = self._prompt
        if key == "esc":
            self._close_prompt()
        elif key == "enter":
            self._rename(prompt.note, prompt.widget.edit_text)
        else:
            self._show_prompt()
            if key == "ctrl u":
                prompt.widget.set_edit_text("")
            else:
                prompt.widget.keypress((size[0],), key)

    def _rename(self, note, title):
        """Rename `note` to `title`, close the prompt and select the note.

        If the notebook refuses, the prompt stays open, showing why, to try
        another title. If `note` is gone or replaced, the prompt closes.
        """
        problem = self._stale(note)
        if problem is not None:
            self._close_prompt()
            self.show_message(problem)
            return
        try:
            renamed = self.tv_notebook.rename(note, title)
        except tv_notebook.Error as e:
            self._show_prompt(error=str(e))
            return
        self._close_prompt()
        self._show_selected(renamed)

    def ask_delete(self, note):
        """Ask to confirm deleting `note`."""
        if note is None:
            self.show_message("Select a note to delete it")
            return
        question = urwid.Text(f"Delete '{note.title}'? (y/N)", wrap=urwid.ELLIPSIS)
        self._open_prompt(note, question, self._delete_key)

    def _delete_key(self, _size, key):
        note = self._prompt.note
        self._close_prompt()
        if key in ("y", "Y"):
            self.delete(note)

    def delete(self, note):
        """Delete `note`, select the note next to it and say so, or show why it could not be deleted."""
        problem = self._stale(note)
        if problem is not None:
            self.show_message(problem)
            return
        neighbour = self.list_box.neighbour(note)
        try:
            self.tv_notebook.delete(note)
        except tv_notebook.Error as e:
            self.show_message(str(e))
            return
        self.filter(self.search_box.edit_text, keep=neighbour, autoselect=False)
        self.show_message(f"Deleted '{note.title}'")

    def edit(self, note):
        """Run the editor on `note`, handing it the terminal while it runs."""
        try:
            command = [*shlex.split(self.editor), note.abspath]
        except ValueError as e:
            self.show_message(f"Could not run editor {self.editor}: {e}")
            return
        logger.debug(f"Running editor: {command}")
        screen = self.loop.screen
        screen.stop()
        try:
            returncode = call_with_interrupts_ignored(command)
        except OSError as e:
            screen.start()
            logger.error(f"Could not run editor {command}: {e}")
            self.show_message(f"Could not run editor {self.editor}: {e.strerror or e}")
            return
        except BaseException:
            try:
                screen.start()
            except Exception:
                logger.exception("Could not restore the screen after the editor failed")
            raise
        screen.start()
        if returncode < 0:
            logger.error(f"Editor killed by signal {-returncode}: {command}")
            self.show_message(f"Editor killed by signal {-returncode}")
        elif returncode > 0:
            logger.error(f"Editor exited with status {returncode}: {command}")
            self.show_message(f"Editor exited with status {returncode}")

    def _update_body(self):
        if not len(self.tv_notebook):
            body = self._empty_body
        elif self.preview_shown:
            body = self._split_body
        else:
            body = self._list_body
        if self.body is not body:
            self.body = body

    def filter(self, query, keep=None, autoselect=True):
        """Show the notes matching `query`, best match first, and update the selection.

        `keep` stays selected if it still matches. Otherwise, if `autoselect`,
        the top row is selected if `autoselection` picks it.
        """
        self._pending_filter = None
        self._update_body()
        self._snippet_cols = self._snippet_width()
        results = self.tv_notebook.find(query, snippet_width=self._snippet_cols)
        self.list_box.filter(results)
        if keep is not None and self.list_box.list_walker.position_of(keep) is not None:
            self.select(keep)
        elif autoselect and query:
            self.select(autoselection(results, query))
        else:
            self.select(None)


def autoselection(results, query):
    """The note of the first of `results` if it is an exact match whose title starts
    with `query`, ignoring case and Unicode normalisation; else None.

    Notes found only by fuzzy matching are never autoselected, so fuzzy
    matching changes neither which note Enter opens nor whether it creates one.
    """
    if not results:
        return None
    top = results[0]
    prefix = unicodedata.normalize("NFC", query).lower()
    return top.note if top.exact and top.note.title.lower().startswith(prefix) else None


def call_with_interrupts_ignored(command):
    """Run `command` and return its exit status, letting Ctrl-C reach only it.

    Ctrl-C sends SIGINT to the terminal's whole foreground process group.
    While waiting, tv3 handles SIGINT by doing nothing. Unlike ignoring the
    signal, a handler is not inherited, so the command still receives it.
    Must be called on the main thread.
    """
    previous = signal.signal(signal.SIGINT, lambda _signum, _frame: None)
    try:
        return subprocess.call(command)
    finally:
        signal.signal(signal.SIGINT, previous)


class MainLoop(urwid.MainLoop):
    """A main loop for a MainFrame that runs the frame's pending refilter before each redraw,
    after leaving one pending if the list's width changed.

    The refilter stays pending if it would wait for the notebook's background read.
    """

    def __init__(self, frame, *args, **kwargs):
        super().__init__(frame, *args, **kwargs)
        self.frame = frame
        frame.attach(self)

    def draw_screen(self):
        self.frame.refilter_if_resized()
        self.frame.flush_filter(wait=False)
        super().draw_screen()


class Screen(urwid.display.raw.Screen):
    """A terminal screen that receives Ctrl-Q and Ctrl-S as keys.

    Terminal flow control (IXON) would otherwise consume them. `start()`
    turns it off; `stop()` restores the terminal settings saved by
    `start()`, flow control included.
    """

    def __init__(self, input=sys.stdin, output=sys.stdout):
        super().__init__(input, output)
        self._input = input

    def start(self, *args, **kwargs):
        context = super().start(*args, **kwargs)
        fd = self._input.fileno()
        if os.isatty(fd):
            attrs = termios.tcgetattr(fd)
            attrs[0] &= ~termios.IXON
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return context


def launch(notes_dir, editor, extension, extensions, exclude=None):
    """Run the user interface until the user quits.

    Args:
        notes_dir: Path to the notes directory.
        editor: Editor command line.
        extension: Default extension for new notes.
        extensions: List of extensions to recognize.
        exclude: Optional list of directories to exclude.
    """
    frame = MainFrame(
        notes_dir,
        editor,
        extension,
        extensions,
        exclude=exclude,
    )
    try:
        MainLoop(frame, palette, screen=Screen()).run()
    finally:
        frame.close()
