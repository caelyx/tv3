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

import urwid
import urwid.display.raw
from urwid import text_layout
from urwid.canvas import apply_text_layout

from . import tv_notebook

logger = logging.getLogger("tv3")

# Color palette for the UI
palette = [
    ("autocomplete", "black", "brown"),
    ("notewidget focused", "black", "brown"),
    ("notewidget unfocused", "default", "default"),
    ("placeholder", "dark blue", "default"),
    ("search", "default", "default"),
    ("status", "white", "dark red"),
]

EDIT_KEYS = ("backspace", "delete", "home", "end")


def placeholder_text(text):
    """Return a widget showing `text` centred in the placeholder style."""
    text_widget = urwid.Text(("placeholder", text), align="center")
    return urwid.Filler(text_widget)


class NoteWidget(urwid.Text):
    """Widget for displaying a note in the list."""

    def __init__(self, note):
        self.note = note
        super().__init__(note.title)

    def selectable(self):
        return True

    def keypress(self, size, key):
        """Pass through all keypresses to parent."""
        return key

    def render(self, size, focus=False):
        """Render the title in the focused or unfocused note style."""
        attr_map = {None: "notewidget focused" if focus else "notewidget unfocused"}
        canv = urwid.CompositeCanvas(super().render(size, focus=focus))
        canv.fill_attr_apply(attr_map)
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
            and completion[: len(typed)].lower() == typed.lower()
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
        if self.completion_extends_text():
            return [
                ("search", completion[: len(typed)]),
                ("autocomplete", completion[len(typed) :]),
            ], self.edit_pos
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


class NoteFilterListBox(urwid.ListBox):
    """A filterable list of notes from a notebook."""

    def __init__(self, on_changed=None):
        """Initialise a new NoteFilterListBox.

        Args:
            on_changed: Optional callback, called with the focused note after
                a keypress or mouse event.
        """
        self._fake_focus = False
        self.list_walker = urwid.SimpleFocusListWalker([])
        self.widgets = {}
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

    def filter(self, matching_notes):
        """Show widgets for `matching_notes`, in order, focusing the first."""
        matching_widgets = []
        for note in matching_notes:
            widget = self.widgets.get(note)
            if widget is None:
                widget = NoteWidget(note)
                self.widgets[note] = widget
            matching_widgets.append(widget)
        self.list_walker[:] = matching_widgets
        if matching_widgets:
            self.focus_position = 0

    def prune(self, notes):
        """Drop cached widgets for notes not in `notes`."""
        self.widgets = {note: w for note, w in self.widgets.items() if note in notes}

    def focus_note(self, note):
        """Focus the widget for `note`, if it is in the list."""
        for position, widget in enumerate(self.list_walker):
            if widget.note == note:
                self.focus_position = position
                break

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


class MainFrame(urwid.Frame):
    """The topmost urwid widget.

    The selected note is the list's focused note while the list has fake
    focus; the search box's completion always shows its title. A footer
    shows a message until the next keypress.

    Typing does not refilter at once: it clears the selection and leaves a
    refilter pending, which `flush_filter()` runs. Inside a main loop that
    happens when the loop has handled all waiting input, before it redraws,
    so a burst of keys refilters once. Any other key or mouse event runs a
    pending refilter before it is handled.
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
        self._idle_handle = None
        self._pending_filter = None
        self._wake_fd = None
        self._wake_lock = threading.Lock()
        self.search_box = AutocompleteWidget()
        self.list_box = NoteFilterListBox(on_changed=self.select)
        self._list_body = urwid.Padding(self.list_box, left=1, right=1)
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
        """Run inside `loop`: refresh from notebook changes and use its screen.

        Must be called before `loop.run()`, so that pending refilters run
        before the loop redraws.
        """
        self.loop = loop
        self._idle_handle = loop.event_loop.enter_idle(self.flush_filter)
        self._wake_fd = loop.watch_pipe(self._on_notebook_changed)
        os.set_blocking(self._wake_fd, False)
        self.tv_notebook.on_change = self._wake

    def close(self):
        """Stop the file watcher and release the main loop pipe."""
        self.tv_notebook.on_change = None
        self.tv_notebook.close()
        if self._idle_handle is not None:
            self.loop.event_loop.remove_enter_idle(self._idle_handle)
            self._idle_handle = None
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
        self.list_box.prune(set(self.tv_notebook))
        if self._pending_filter is None:
            selected = self.selected_note
            self._pending_filter = (selected, selected is not None)
        return True

    def flush_filter(self):
        """Run the pending refilter, if any."""
        if self._pending_filter is not None:
            keep, autoselect = self._pending_filter
            self.filter(self.search_box.edit_text, keep=keep, autoselect=autoselect)

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

    def show_message(self, text):
        """Show `text` in the one-row status line until the next keypress."""
        self.footer = urwid.Text(("status", text), wrap=urwid.ELLIPSIS)

    def quit(self):
        raise urwid.ExitMainLoop()

    def keypress(self, size, key):
        """Handle keypress events.

        Returns:
            Key if not handled, None if handled.
        """
        self.footer = None
        # Only typing with nothing selected can leave the list stale.
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
        elif key in ["ctrl q", "ctrl x"]:
            self.quit()
        elif key == "enter":
            self.open_or_create()
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
            self._pending_filter = (None, autoselect)
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
        self.filter(self.search_box.edit_text, keep=note)
        if self.selected_note != note:
            self.set_search_text(note.title, keep=note)

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

    def filter(self, query, keep=None, autoselect=True):
        """Show the notes matching `query`, newest first, and update the selection.

        `keep` stays selected if it still matches. Otherwise, if `autoselect`,
        the newest note whose title starts with `query` is selected.
        """
        self._pending_filter = None
        self.body = self._list_body if len(self.tv_notebook) else self._empty_body
        matching_notes = self.tv_notebook.search(query)
        matching_notes.sort(key=lambda x: x.mtime, reverse=True)
        self.list_box.filter(matching_notes)
        if keep is not None and keep in matching_notes:
            self.select(keep)
        elif autoselect and query:
            prefix = query.lower()
            self.select(
                next((n for n in matching_notes if n.title.lower().startswith(prefix)), None)
            )
        else:
            self.select(None)


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
        loop = urwid.MainLoop(frame, palette, screen=Screen())
        frame.attach(loop)
        loop.run()
    finally:
        frame.close()
