"""Tests for urwid_ui.py - UI components and interactions."""

import os
import sys
import pytest
from unittest.mock import Mock, MagicMock, patch, call
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import urwid
import urwid_ui
import tv_notebook


class TestNoteWidget:
    """Tests for the NoteWidget class."""

    def test_note_widget_creation(self, temp_notes_dir):
        """Test creating a NoteWidget."""
        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt']
        )
        note = tv_notebook.PlainTextNote("test", notebook, ".txt")
        widget = urwid_ui.NoteWidget(note)

        assert widget.note == note
        assert widget.selectable()

    def test_note_widget_displays_title(self, temp_notes_dir):
        """Test that NoteWidget displays the note title."""
        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt']
        )
        note = tv_notebook.PlainTextNote("my_note", notebook, ".txt")
        widget = urwid_ui.NoteWidget(note)

        assert widget.get_text()[0] == "my_note"

    def test_note_widget_keypress_passthrough(self, temp_notes_dir):
        """Test that NoteWidget passes through keypresses."""
        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt']
        )
        note = tv_notebook.PlainTextNote("test", notebook, ".txt")
        widget = urwid_ui.NoteWidget(note)

        # Keys should be passed through
        assert widget.keypress((10,), 'enter') == 'enter'
        assert widget.keypress((10,), 'up') == 'up'

    def test_note_widget_render_focused(self, temp_notes_dir):
        """Test rendering a focused NoteWidget."""
        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt']
        )
        note = tv_notebook.PlainTextNote("test", notebook, ".txt")
        widget = urwid_ui.NoteWidget(note)

        canvas = widget.render((20,), focus=True)
        assert canvas is not None

    def test_note_widget_render_unfocused(self, temp_notes_dir):
        """Test rendering an unfocused NoteWidget."""
        notebook = tv_notebook.PlainTextNoteBook(
            temp_notes_dir, '.txt', ['.txt']
        )
        note = tv_notebook.PlainTextNote("test", notebook, ".txt")
        widget = urwid_ui.NoteWidget(note)

        canvas = widget.render((20,), focus=False)
        assert canvas is not None


class TestAutocompleteWidget:
    """Tests for the AutocompleteWidget class."""

    def test_autocomplete_widget_creation(self):
        """Test creating an AutocompleteWidget."""
        widget = urwid_ui.AutocompleteWidget()
        assert widget.fake_focus == True
        assert widget.autocomplete_text is None

    def test_set_autocomplete_text(self):
        """Test setting autocomplete text."""
        widget = urwid_ui.AutocompleteWidget()
        widget.autocomplete_text = "suggested text"
        assert widget.autocomplete_text == "suggested text"

    def test_autocomplete_display_with_matching_prefix(self):
        """Test autocomplete display when typed text matches."""
        widget = urwid_ui.AutocompleteWidget()
        widget.set_edit_text("hel")
        widget.autocomplete_text = "hello"

        text, attrs = widget.get_text()
        assert text == "hello"
        assert len(attrs) == 2
        # First part should be the typed text
        assert attrs[0][0] == 'search'
        # Second part should be autocomplete
        assert attrs[1][0] == 'autocomplete'

    def test_autocomplete_case_insensitive(self):
        """Test that autocomplete is case-insensitive."""
        widget = urwid_ui.AutocompleteWidget()
        widget.set_edit_text("HEL")
        widget.autocomplete_text = "hello"

        text, attrs = widget.get_text()
        assert text == "hello"

    def test_autocomplete_display_no_match(self):
        """Test autocomplete when typed text doesn't match."""
        widget = urwid_ui.AutocompleteWidget()
        widget.set_edit_text("xyz")
        widget.autocomplete_text = "hello"

        text, attrs = widget.get_text()
        assert text == "hello"
        assert attrs[0][0] == 'autocomplete'

    def test_placeholder_when_empty(self):
        """Test placeholder text when widget is empty."""
        widget = urwid_ui.AutocompleteWidget()

        text, attrs = widget.get_text()
        assert "Find or Create" in text
        assert attrs[0][0] == 'placeholder'

    def test_consume_autocomplete(self):
        """Test consuming autocomplete text."""
        widget = urwid_ui.AutocompleteWidget()
        widget.set_edit_text("hel")
        widget.autocomplete_text = "hello"

        result = widget.consume()
        assert result == True
        assert widget.edit_text == "hello"
        assert widget.autocomplete_text is None

    def test_consume_no_autocomplete(self):
        """Test consuming when no autocomplete is set."""
        widget = urwid_ui.AutocompleteWidget()
        widget.set_edit_text("hello")

        result = widget.consume()
        assert result == False

    def test_consume_already_complete(self):
        """Test consuming when text is already complete."""
        widget = urwid_ui.AutocompleteWidget()
        widget.set_edit_text("hello")
        widget.autocomplete_text = "hello"

        result = widget.consume()
        assert result == False


class TestNoteFilterListBox:
    """Tests for the NoteFilterListBox class."""

    def test_listbox_creation(self):
        """Test creating a NoteFilterListBox."""
        listbox = urwid_ui.NoteFilterListBox()
        assert listbox.fake_focus == False
        assert len(listbox.list_walker) == 0

    def test_listbox_with_callback(self):
        """Test creating listbox with on_changed callback."""
        callback = Mock()
        listbox = urwid_ui.NoteFilterListBox(on_changed=callback)
        assert listbox.on_changed == callback

    def test_filter_notes(self, populated_notes_dir):
        """Test filtering the note list."""
        notebook = tv_notebook.PlainTextNoteBook(
            populated_notes_dir, '.txt', ['.txt', '.md']
        )
        listbox = urwid_ui.NoteFilterListBox()

        notes = list(notebook)
        listbox.filter(notes)

        assert len(listbox.list_walker) == len(notes)

    def test_filter_empty_list(self):
        """Test filtering with empty note list."""
        listbox = urwid_ui.NoteFilterListBox()
        listbox.filter([])
        assert len(listbox.list_walker) == 0

    def test_filter_updates_widgets(self, populated_notes_dir):
        """Test that filtering creates/updates widgets."""
        notebook = tv_notebook.PlainTextNoteBook(
            populated_notes_dir, '.txt', ['.txt', '.md']
        )
        listbox = urwid_ui.NoteFilterListBox()

        notes = list(notebook)
        listbox.filter(notes)

        # Widgets should be created
        for note in notes:
            assert note.abspath in listbox.widgets

    def test_filter_reuses_widgets(self, populated_notes_dir):
        """Test that filtering reuses existing widgets."""
        notebook = tv_notebook.PlainTextNoteBook(
            populated_notes_dir, '.txt', ['.txt', '.md']
        )
        listbox = urwid_ui.NoteFilterListBox()

        notes = list(notebook)
        listbox.filter(notes)
        first_widget_id = id(listbox.widgets[notes[0].abspath])

        # Filter again with same notes
        listbox.filter(notes)
        second_widget_id = id(listbox.widgets[notes[0].abspath])

        # Should reuse the same widget
        assert first_widget_id == second_widget_id

    def test_focus_note(self, populated_notes_dir):
        """Test focusing a specific note."""
        notebook = tv_notebook.PlainTextNoteBook(
            populated_notes_dir, '.txt', ['.txt', '.md']
        )
        listbox = urwid_ui.NoteFilterListBox()

        notes = list(notebook)
        listbox.filter(notes)

        # Focus a specific note
        listbox.focus_note(notes[1])
        assert listbox.selected_note == notes[1]

    def test_render_empty_placeholder(self):
        """Test rendering placeholder when no notes."""
        listbox = urwid_ui.NoteFilterListBox()
        canvas = listbox.render((40, 10))
        assert canvas is not None

    def test_render_with_notes(self, populated_notes_dir):
        """Test rendering with notes."""
        notebook = tv_notebook.PlainTextNoteBook(
            populated_notes_dir, '.txt', ['.txt', '.md']
        )
        listbox = urwid_ui.NoteFilterListBox()
        notes = list(notebook)
        listbox.filter(notes)

        canvas = listbox.render((40, 10), focus=True)
        assert canvas is not None


class TestMainFrame:
    """Tests for the MainFrame class."""

    def test_mainframe_creation(self, temp_notes_dir):
        """Test creating a MainFrame."""
        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )

        assert frame.editor == 'vim'
        assert frame.tv_notebook is not None
        assert frame.search_box is not None
        assert frame.list_box is not None

    def test_mainframe_loads_notebook(self, populated_notes_dir):
        """Test that MainFrame loads the notebook."""
        frame = urwid_ui.MainFrame(
            populated_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )

        assert len(frame.tv_notebook) == 4

    def test_search_filters_notes(self, populated_notes_dir):
        """Test that search filters notes correctly."""
        frame = urwid_ui.MainFrame(
            populated_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )

        frame.filter("first")
        assert len(frame.list_box.list_walker) == 1

    def test_search_autocomplete(self, populated_notes_dir):
        """Test that search sets autocomplete."""
        frame = urwid_ui.MainFrame(
            populated_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )

        frame.search_box.set_edit_text("fir")
        frame.filter("fir")

        # Should autocomplete to "first_note"
        assert frame.selected_note is not None
        assert "first" in frame.selected_note.title

    def test_empty_search_shows_all(self, populated_notes_dir):
        """Test that empty search shows all notes."""
        frame = urwid_ui.MainFrame(
            populated_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )

        frame.filter("")
        assert len(frame.list_box.list_walker) == 4

    def test_keypress_quit_ctrl_q(self, temp_notes_dir):
        """Test that Ctrl-Q quits the application."""
        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.txt', ['.txt']
        )

        with pytest.raises(urwid.ExitMainLoop):
            frame.keypress((80, 24), 'ctrl q')

    def test_keypress_quit_ctrl_x(self, temp_notes_dir):
        """Test that Ctrl-X quits the application."""
        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.txt', ['.txt']
        )

        with pytest.raises(urwid.ExitMainLoop):
            frame.keypress((80, 24), 'ctrl x')

    def test_keypress_quit_esc_when_empty(self, temp_notes_dir):
        """Test that ESC quits when search is empty."""
        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.txt', ['.txt']
        )

        with pytest.raises(urwid.ExitMainLoop):
            frame.keypress((80, 24), 'esc')

    def test_keypress_esc_clears_search(self, populated_notes_dir):
        """Test that ESC clears search."""
        frame = urwid_ui.MainFrame(
            populated_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )

        frame.search_box.set_edit_text("test")
        frame.keypress((80, 24), 'esc')

        assert frame.search_box.edit_text == ""

    def test_keypress_esc_clears_selection(self, populated_notes_dir):
        """Test that ESC clears selection first."""
        frame = urwid_ui.MainFrame(
            populated_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )

        # Set a selection
        frame.search_box.set_edit_text("first")
        frame.filter("first")
        assert frame.selected_note is not None

        # First ESC should clear selection
        result = frame.keypress((80, 24), 'esc')
        assert frame.selected_note is None
        assert result is None

    def test_keypress_down_moves_to_list(self, populated_notes_dir):
        """Test that DOWN key moves focus to list."""
        frame = urwid_ui.MainFrame(
            populated_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )

        frame.filter("")
        frame.list_box.fake_focus = False

        frame.keypress((80, 24), 'down')
        assert frame.list_box.fake_focus == True

    def test_keypress_tab_consumes_autocomplete(self, populated_notes_dir):
        """Test that TAB consumes autocomplete."""
        frame = urwid_ui.MainFrame(
            populated_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )

        frame.search_box.set_edit_text("fir")
        frame.filter("fir")

        # Should have autocomplete
        assert frame.selected_note is not None

        frame.keypress((80, 24), 'tab')
        # Autocomplete should be consumed
        assert frame.search_box.edit_text.startswith("fir")

    @patch('urwid_ui.system')
    def test_keypress_enter_opens_note(self, mock_system, populated_notes_dir):
        """Test that ENTER opens the selected note."""
        frame = urwid_ui.MainFrame(
            populated_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )
        frame.loop = Mock()

        frame.search_box.set_edit_text("first")
        frame.filter("first")

        frame.keypress((80, 24), 'enter')
        mock_system.assert_called_once()

    @patch('urwid_ui.system')
    def test_keypress_enter_creates_note(self, mock_system, temp_notes_dir):
        """Test that ENTER creates a new note."""
        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.txt', ['.txt']
        )
        frame.loop = Mock()

        frame.search_box.set_edit_text("new_note")
        frame.keypress((80, 24), 'enter')

        # Should have called system to open editor
        mock_system.assert_called_once()

        # Note should now exist
        assert len(frame.tv_notebook) == 1

    def test_on_search_box_changed(self, populated_notes_dir):
        """Test that search box changes trigger filtering."""
        frame = urwid_ui.MainFrame(
            populated_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )

        frame.on_search_box_changed(frame.search_box, "first")
        assert len(frame.list_box.list_walker) == 1

    def test_on_list_box_changed(self, populated_notes_dir):
        """Test that list box selection changes update selected note."""
        frame = urwid_ui.MainFrame(
            populated_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )

        note = list(frame.tv_notebook)[0]
        frame.on_list_box_changed(note)
        assert frame.selected_note == note

    def test_suppress_filter_flag(self, populated_notes_dir):
        """Test that suppress_filter prevents filtering."""
        frame = urwid_ui.MainFrame(
            populated_notes_dir, 'vim', '.txt', ['.txt', '.md']
        )

        initial_count = len(frame.list_box.list_walker)
        frame.suppress_filter = True
        frame.filter("first")

        # Should not have filtered
        assert len(frame.list_box.list_walker) == initial_count

    def test_empty_notebook_shows_placeholder(self, temp_notes_dir):
        """Test that empty notebook shows placeholder."""
        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.txt', ['.txt']
        )

        # Should show placeholder
        assert frame.body is not None


class TestSystemFunction:
    """Tests for the system() function."""

    @patch('subprocess.check_call')
    def test_system_executes_command(self, mock_check_call):
        """Test that system executes the command."""
        mock_loop = Mock()
        mock_loop.screen = Mock()

        urwid_ui.system("echo test", mock_loop)
        mock_check_call.assert_called_once()

    @patch('subprocess.check_call')
    def test_system_splits_command_safely(self, mock_check_call):
        """Test that system uses shlex to split commands."""
        mock_loop = Mock()
        mock_loop.screen = Mock()

        urwid_ui.system("vim 'file with spaces.txt'", mock_loop)
        mock_check_call.assert_called_once()
        # Should have split correctly
        args = mock_check_call.call_args[0][0]
        assert isinstance(args, list)

    @patch('subprocess.check_call')
    def test_system_clears_screen(self, mock_check_call):
        """Test that system clears the screen after command."""
        mock_loop = Mock()
        mock_loop.screen = Mock()

        urwid_ui.system("echo test", mock_loop)
        mock_loop.screen.clear.assert_called_once()

    @patch('subprocess.check_call', side_effect=Exception("Command failed"))
    def test_system_propagates_exceptions(self, mock_check_call):
        """Test that system propagates exceptions."""
        mock_loop = Mock()
        mock_loop.screen = Mock()

        with pytest.raises(Exception, match="Command failed"):
            urwid_ui.system("false", mock_loop)


class TestPlaceholderText:
    """Tests for the placeholder_text() function."""

    def test_placeholder_text_creation(self):
        """Test creating placeholder text."""
        widget = urwid_ui.placeholder_text("Test message")
        assert widget is not None

    def test_placeholder_text_renders(self):
        """Test that placeholder text renders."""
        widget = urwid_ui.placeholder_text("Test message")
        canvas = widget.render((40, 10))
        assert canvas is not None


class TestEdgeCases:
    """Tests for edge cases and error conditions."""

    def test_invalid_note_title_silently_fails(self, temp_notes_dir):
        """Test that invalid note titles fail gracefully."""
        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.txt', ['.txt']
        )
        frame.loop = Mock()

        frame.search_box.set_edit_text("/")
        # Should not crash
        frame.keypress((80, 24), 'enter')

    def test_very_long_note_list(self, temp_notes_dir):
        """Test handling a large number of notes."""
        # Create many notes
        for i in range(100):
            Path(temp_notes_dir, f"note_{i}.txt").write_text(f"Content {i}")

        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.txt', ['.txt']
        )

        assert len(frame.tv_notebook) == 100

        # Filtering should still work
        frame.filter("note_5")
        # Should find note_5, note_50-59
        assert len(frame.list_box.list_walker) >= 1

    def test_unicode_in_search(self, temp_notes_dir):
        """Test searching with unicode characters."""
        Path(temp_notes_dir, "unicode_note.txt").write_text("こんにちは")

        frame = urwid_ui.MainFrame(
            temp_notes_dir, 'vim', '.txt', ['.txt']
        )

        # Should not crash
        frame.filter("こんにちは")

    @patch('urwid_ui.system')
    def test_editor_with_spaces_in_path(self, mock_system, temp_notes_dir):
        """Test handling editor with spaces in path."""
        # Create the file before initializing the frame so the notebook picks it up
        Path(temp_notes_dir, "test.txt").write_text("test")

        frame = urwid_ui.MainFrame(
            temp_notes_dir, '/usr/local/my editor', '.txt', ['.txt']
        )
        frame.loop = Mock()

        note = list(frame.tv_notebook)[0]
        frame.selected_note = note

        frame.keypress((80, 24), 'enter')
        mock_system.assert_called_once()
