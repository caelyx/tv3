"""A console user interface for Terminal Velocity."""

import contextlib
import logging
import os
import shlex
import subprocess

import urwid

from . import tv_notebook

logger = logging.getLogger("tv3")

# Color palette for the UI
palette = [
    ("autocomplete", "black", "brown"),
    ("notewidget focused", "black", "brown"),
    ("notewidget unfocused", "default", "default"),
    ("placeholder", "dark blue", "default"),
    ("search", "default", "default"),
]


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
    """A text editing widget that can show a completion of the typed text."""

    def __init__(self, *args, **kwargs):
        self.fake_focus = True
        self._autocomplete_text = None
        super().__init__(*args, **kwargs)

    def get_autocomplete_text(self):
        return self._autocomplete_text

    def set_autocomplete_text(self, text):
        self._autocomplete_text = text
        self._invalidate()

    autocomplete_text = property(get_autocomplete_text, set_autocomplete_text)

    def render(self, size, focus=False):
        """Render with fake focus to always show cursor."""
        return super().render(size, self.fake_focus)

    def get_text(self):
        """Return the text and attributes to display.

        A completion that extends the typed text is shown after it; any
        other completion replaces it.
        """
        if not self.edit_text and not self.autocomplete_text:
            placeholder = "Find or Create"
            return (placeholder, [("placeholder", len(placeholder))])
        if not self.autocomplete_text:
            return super().get_text()
        is_prefix = self.autocomplete_text.lower().startswith(self.edit_text.lower())
        if self.edit_text and is_prefix:
            text_to_show = self.autocomplete_text
            attrs = [
                ("search", len(self.edit_text)),
                ("autocomplete", len(text_to_show) - len(self.edit_text)),
            ]
            return (text_to_show, attrs)
        return (
            self.autocomplete_text,
            [("autocomplete", len(self.autocomplete_text))],
        )

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
        self._wake_fd = None
        self.search_box = AutocompleteWidget(wrap="clip")
        self.list_box = NoteFilterListBox(on_changed=self.on_list_box_changed)
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
        """Run inside `loop`: refresh from notebook changes and use its screen."""
        self.loop = loop
        self._wake_fd = loop.watch_pipe(self._on_notebook_changed)
        os.set_blocking(self._wake_fd, False)
        self.tv_notebook.on_change = self._wake

    def close(self):
        """Stop the file watcher and release the main loop pipe."""
        self.tv_notebook.on_change = None
        self.tv_notebook.close()
        if self._wake_fd is not None:
            self.loop.remove_watch_pipe(self._wake_fd)
            os.close(self._wake_fd)
            self._wake_fd = None

    def _wake(self):
        """Wake the main loop. Called on the watcher thread, so touches no widgets."""
        # A full pipe already holds a pending wake-up.
        with contextlib.suppress(BlockingIOError):
            os.write(self._wake_fd, b"\0")

    def _on_notebook_changed(self, _data):
        selected = self.selected_note
        self.list_box.prune(set(self.tv_notebook))
        self.filter(self.search_box.edit_text, keep=selected, autoselect=selected is not None)
        return True

    @property
    def selected_note(self):
        """The selected note, or None."""
        if self.list_box.fake_focus:
            return self.list_box.selected_note
        return None

    def select(self, note):
        """Select `note` (which must be in the list), or clear the selection."""
        if note is not None:
            self.list_box.focus_note(note)
        self.list_box.fake_focus = note is not None
        selected = self.selected_note
        self.search_box.autocomplete_text = selected.title if selected else None

    def show_message(self, text):
        """Show `text` in the status line until the next keypress."""
        self.footer = urwid.Text(text)

    def quit(self):
        raise urwid.ExitMainLoop()

    def keypress(self, size, key):
        """Handle keypress events.

        Returns:
            Key if not handled, None if handled.
        """
        self.footer = None
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
            return self._backspace(size, selected)
        else:
            return self._edit_search(size, key)
        return None

    def _edit_search(self, size, key, autoselect=True):
        """Pass `key` to the search box and refilter if its text changed."""
        before = self.search_box.edit_text
        result = self.header.keypress((size[0],), key)
        if self.search_box.edit_text != before:
            self.filter(self.search_box.edit_text, autoselect=autoselect)
        return result

    def _backspace(self, size, selected):
        """Remove a completion if one is showing, else delete a character.

        Deleting never autoselects a note. With a note selected whose title
        does not complete the typed text, the title is edited instead.
        """
        if selected is not None:
            typed = self.search_box.edit_text
            title = selected.title
            if title.lower().startswith(typed.lower()) and len(title) > len(typed):
                self.select(None)
                return None
            if title != typed:
                self.search_box.consume()
        return self._edit_search(size, "backspace", autoselect=False)

    def set_search_text(self, text):
        """Replace the search text, with the cursor at the end, and refilter."""
        self.search_box.set_edit_text(text)
        self.search_box.set_edit_pos(len(text))
        self.filter(text)

    def open_or_create(self):
        """Open the selected note, or create and open a note titled as typed."""
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
        self.filter(self.search_box.edit_text, keep=note)

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
            returncode = subprocess.call(command)
        except OSError as e:
            logger.error(f"Could not run editor {command}: {e}")
            self.show_message(f"Could not run editor {self.editor}: {e.strerror or e}")
            return
        finally:
            screen.start()
        if returncode != 0:
            logger.error(f"Editor exited with status {returncode}: {command}")
            self.show_message(f"Editor exited with status {returncode}")

    def filter(self, query, keep=None, autoselect=True):
        """Show the notes matching `query`, newest first, and update the selection.

        `keep` stays selected if it still matches. Otherwise, if `autoselect`,
        the newest note whose title starts with `query` is selected.
        """
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

    def on_list_box_changed(self, note):
        self.select(note)


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
        loop = urwid.MainLoop(frame, palette)
        frame.attach(loop)
        loop.run()
    finally:
        frame.close()
