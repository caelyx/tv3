"""A console user interface for Terminal Velocity."""

import logging
import shlex
import subprocess

import urwid

import tv_notebook

logger = logging.getLogger("tv3")

# Color palette for the UI
palette = [
    ("autocomplete", "black", "brown"),
    ("notewidget focused", "black", "brown"),
    ("notewidget unfocused", "default", "default"),
    ("placeholder", "dark blue", "default"),
    ("search", "default", "default"),
]


def system(cmd, loop):
    """Execute a system command in a subshell and return the exit status.

    Args:
        cmd: Command string to execute (will be safely split with shlex).
        loop: The urwid MainLoop instance.

    Returns:
        Return code from the subprocess.

    Raises:
        Exception: If the command execution fails.
    """
    safe_cmd = shlex.split(cmd)
    logger.debug(f"System command: {safe_cmd}")
    try:
        returncode = subprocess.check_call(safe_cmd)
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed with return code {e.returncode}: {safe_cmd}")
        raise
    except FileNotFoundError:
        logger.error(f"Command not found: {safe_cmd[0]}")
        raise
    except Exception as e:
        logger.exception(f"Unexpected error executing command: {e}")
        raise
    finally:
        # Clear and refresh the screen after command execution
        loop.screen.clear()
    return returncode


def placeholder_text(text):
    """Return a placeholder text widget with the given text.

    Args:
        text: The placeholder text to display.

    Returns:
        A urwid Filler widget containing the centered placeholder text.
    """
    text_widget = urwid.Text(("placeholder", text), align="center")
    filler_widget = urwid.Filler(text_widget)
    return filler_widget


class NoteWidget(urwid.Text):
    """Widget for displaying a note in the list."""

    def __init__(self, note):
        """Initialize a NoteWidget.

        Args:
            note: The PlainTextNote object to display.
        """
        self.note = note
        super().__init__(note.title)

    def selectable(self):
        """Mark this widget as selectable."""
        return True

    def keypress(self, size, key):
        """Pass through all keypresses to parent."""
        return key

    def render(self, size, focus=False):
        """Render the widget applying focused and unfocused display attrs.

        Args:
            size: Widget size tuple.
            focus: Whether this widget has focus.

        Returns:
            Canvas with appropriate styling applied.
        """
        attr_map = {None: "notewidget focused" if focus else "notewidget unfocused"}
        canv = super().render(size, focus=focus)
        canv = urwid.CompositeCanvas(canv)
        canv.fill_attr_apply(attr_map)
        return canv


class AutocompleteWidget(urwid.Edit):
    """A text editing widget with autocomplete support."""

    def __init__(self, *args, **kwargs):
        """Initialize an AutocompleteWidget."""
        self.fake_focus = True
        self._autocomplete_text = None
        super().__init__(*args, **kwargs)

    def get_autocomplete_text(self):
        """Get the current autocomplete text."""
        return self._autocomplete_text

    def set_autocomplete_text(self, text):
        """Set the autocomplete text and invalidate the widget."""
        self._autocomplete_text = text
        self._invalidate()

    autocomplete_text = property(get_autocomplete_text, set_autocomplete_text)

    def render(self, size, focus=False):
        """Render with fake focus to always show cursor."""
        return super().render(size, self.fake_focus)

    def get_text(self):
        """Get the text and attributes for display.

        Returns:
            Tuple of (text, attributes) for rendering.
        """
        if not self.edit_text and not self.autocomplete_text:
            placeholder = "Find or Create"
            return (placeholder, [("placeholder", len(placeholder))])
        if not self.autocomplete_text:
            return super().get_text()
        is_substring = self.autocomplete_text.lower().startswith(self.edit_text.lower())
        if self.edit_text and is_substring:
            text_to_show = self.edit_text + self.autocomplete_text[len(self.edit_text) :]
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
        """Consume the autocomplete text, turning it into typed text.

        Returns:
            True if autocomplete was consumed, False otherwise.
        """
        if self.autocomplete_text and len(self.edit_text) < len(self.autocomplete_text):
            self.set_edit_text(self.autocomplete_text)
            self.move_cursor_to_coords((1,), len(self.autocomplete_text), 0)
            self.autocomplete_text = None
            return True
        return False


class NoteFilterListBox(urwid.ListBox):
    """A filterable list of notes from a notebook."""

    def __init__(self, on_changed=None):
        """Initialise a new NoteFilterListBox.

        Args:
            on_changed: Optional callback function called when selection changes.
        """
        self._fake_focus = False
        self.list_walker = urwid.SimpleFocusListWalker([])
        self.widgets = {}
        super().__init__(self.list_walker)
        self.on_changed = on_changed

    def get_selected_note(self):
        """Get the currently selected note."""
        if self.focus:
            return self.focus.note
        return None

    selected_note = property(get_selected_note)

    def get_fake_focus(self):
        """Get the fake focus state."""
        return self._fake_focus

    def set_fake_focus(self, value):
        """Set the fake focus state and invalidate."""
        self._fake_focus = value
        self._invalidate()

    fake_focus = property(get_fake_focus, set_fake_focus)

    def render(self, size, focus=False):
        """Render the list box or a placeholder if empty."""
        if len(self.list_walker) == 0:
            placeholder = placeholder_text("No matching notes, press Enter to create a new note")
            return placeholder.render(size)
        return super().render(size, self.fake_focus)

    def filter(self, matching_notes):
        """Filter this listbox to show only widgets for matching notes.

        Args:
            matching_notes: List of PlainTextNote objects to display.
        """
        matching_widgets = []
        for note in matching_notes:
            widget = self.widgets.get(note.abspath)
            if widget:
                matching_widgets.append(widget)
            else:
                widget = NoteWidget(note)
                self.widgets[note.abspath] = widget
                matching_widgets.append(widget)
        del self.list_walker[:]
        for widget in matching_widgets:
            self.list_walker.append(widget)

    def focus_note(self, note):
        """Focus the widget for the given note.

        Args:
            note: The PlainTextNote to focus.
        """
        for widget in self.list_walker:
            if widget.note == note:
                self.list_walker.set_focus(self.list_walker.index(widget))
                break

    def keypress(self, size, key):
        """Handle keypress and notify callback of changes."""
        result = super().keypress(size, key)
        if self.on_changed and self.selected_note:
            self.on_changed(self.selected_note)
        return result

    def mouse_event(self, size, event, button, col, row, focus):
        """Handle mouse events and notify callback of changes."""
        result = super().mouse_event(size, event, button, col, row, focus)
        if self.on_changed and self.selected_note:
            self.on_changed(self.selected_note)
        return result


class MainFrame(urwid.Frame):
    """The topmost urwid widget."""

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
            editor: Editor command to use.
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
        self.suppress_filter = False
        self.suppress_focus = False
        self._selected_note = None
        self.search_box = AutocompleteWidget(wrap="clip")
        self.list_box = NoteFilterListBox(on_changed=self.on_list_box_changed)
        urwid.connect_signal(
            self.search_box,
            "change",
            self.on_search_box_changed,
        )
        super().__init__(
            header=urwid.LineBox(self.search_box),
            body=None,
            focus_part="body",
        )
        self.filter(self.search_box.edit_text)

    def get_selected_note(self):
        """Get the currently selected note."""
        return self._selected_note

    def set_selected_note(self, note):
        """Select the given note.

        Args:
            note: The PlainTextNote to select, or None to clear selection.
        """
        if self.suppress_focus:
            return
        if note:
            self.search_box.autocomplete_text = note.title
            self.list_box.fake_focus = True
            self.list_box.focus_note(note)
        else:
            self.search_box.autocomplete_text = None
            self.list_box.fake_focus = False
        self._selected_note = note

    selected_note = property(get_selected_note, set_selected_note)

    def quit(self):
        """Quit the app and clean up resources."""
        # Clean up the notebook (stop file watcher)
        if hasattr(self, "tv_notebook"):
            self.tv_notebook.close()
        raise urwid.ExitMainLoop()

    def keypress(self, size, key):
        """Handle keypress events.

        Args:
            size: Widget size tuple.
            key: Key string.

        Returns:
            Key if not handled, None if handled.
        """
        maxcol, _maxrow = size
        self.suppress_filter = False
        self.suppress_focus = False
        if key in ["esc", "ctrl d", "ctrl u"]:
            if self.selected_note:
                self.selected_note = None
                return None
            if self.search_box.edit_text:
                self.search_box.set_edit_text("")
                return None
            self.quit()
        elif key in ["ctrl q", "ctrl x"]:
            self.quit()
        elif key == "enter":
            if self.selected_note:
                note_path = shlex.quote(self.selected_note.abspath)
                system(f"{self.editor} {note_path}", self.loop)
            elif self.search_box.edit_text:
                try:
                    note = self.tv_notebook.add_new(
                        self.search_box.edit_text + self.tv_notebook.extension
                    )
                    note_path = shlex.quote(note.abspath)
                    system(f"{self.editor} {note_path}", self.loop)
                except tv_notebook.NoteAlreadyExistsError:
                    # Note exists, open it anyway
                    note_path = self.search_box.edit_text + self.tv_notebook.extension
                    note_path = shlex.quote(note_path)
                    system(f"{self.editor} {note_path}", self.loop)
                except tv_notebook.InvalidNoteTitleError:
                    # Invalid title, skip silently
                    pass
            self.suppress_focus = True
            self.filter(self.search_box.edit_text)
            return None
        elif self.selected_note and key in ["tab", "left", "right"]:
            if self.search_box.consume():
                return None
            return self.search_box.keypress((maxcol,), key)
        elif key == "down":
            if not self.list_box.fake_focus:
                self.list_box.fake_focus = True
                if self.list_box.selected_note:
                    self.on_list_box_changed(self.list_box.selected_note)
                return None
            return self.list_box.keypress(size, key)
        elif key in ["up", "page up", "page down"]:
            return self.list_box.keypress(size, key)
        elif key == "backspace":
            consume = False
            if self.selected_note:
                if self.search_box.edit_text == "":
                    consume = True
                else:
                    title = self.selected_note.title.lower()
                    typed = self.search_box.edit_text.lower()
                    if not title.startswith(typed):
                        consume = True
            if consume:
                self.search_box.consume()
            else:
                self.selected_note = None
            self.suppress_focus = True
            return self.search_box.keypress((maxcol,), key)
        else:
            return self.search_box.keypress((maxcol,), key)

    def filter(self, query):
        """Do the synchronised list box filter and search box autocomplete.

        Args:
            query: Search query string.
        """
        if self.suppress_filter:
            return
        if len(self.tv_notebook) == 0:
            self.body = placeholder_text(
                "You have no notes yet, to create a note type a note title then press Enter"
            )
        else:
            self.body = urwid.Padding(self.list_box, left=1, right=1)
        matching_notes = self.tv_notebook.search(query)
        matching_notes.sort(key=lambda x: x.mtime, reverse=True)
        self.list_box.filter(matching_notes)
        autocompletable_matches = []
        if query:
            for note in matching_notes:
                if note.title.lower().startswith(query.lower()):
                    autocompletable_matches.append(note)
        if autocompletable_matches:
            self.selected_note = autocompletable_matches[0]
        else:
            self.selected_note = None

    def on_search_box_changed(self, edit, new_edit_text):
        """Handle search box text changes."""
        self.filter(new_edit_text)

    def on_list_box_changed(self, note):
        """Handle list box selection changes."""
        self.selected_note = note


def launch(notes_dir, editor, extension, extensions, exclude=None):
    """Launch the user interface.

    Args:
        notes_dir: Path to the notes directory.
        editor: Editor command to use.
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
    frame.loop = urwid.MainLoop(frame, palette)
    try:
        frame.loop.run()
    finally:
        # Ensure cleanup happens even if loop exits unexpectedly
        if hasattr(frame, "tv_notebook"):
            frame.tv_notebook.close()
