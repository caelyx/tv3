"""Integration tests: the notebook, the UI and a real editor working together."""

import fcntl
import os
import select
import shlex
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

import pytest

from tests.ui_helpers import (
    highlighted,
    list_lines,
    press,
    run_headless,
    search_line,
    status_line,
    type_text,
)
from tv3 import tv_notebook


class TestEndToEndWorkflow:
    def test_create_edit_and_find_note_through_ui(
        self, make_frame, stub_editor, notes_dir, monkeypatch
    ):
        monkeypatch.setenv("STUB_EDITOR_APPEND", "remember the milk")
        frame = make_frame({"older.txt": "something else"})

        type_text(frame, "shopping")
        press(frame, "enter")
        assert (notes_dir / "shopping.txt").read_text() == "remember the milk"
        assert stub_editor.calls() == [[str(notes_dir / "shopping.txt")]]

        press(frame, "esc", "esc")
        assert list_lines(frame) == ["shopping", "older"]

        type_text(frame, "milk")
        assert list_lines(frame) == ["shopping", "remember the milk"]

    def test_search_and_clear(self, make_frame, populated_notes_dir):
        frame = make_frame()
        assert list_lines(frame) == ["todo", "meeting_notes", "second_note", "first_note"]

        type_text(frame, "first")
        assert list_lines(frame) == ["first_note"]
        assert search_line(frame) == "first_note"

        press(frame, "esc", "esc")
        assert list_lines(frame) == ["todo", "meeting_notes", "second_note", "first_note"]

    def test_nested_notes(self, make_frame, nested_notes_dir, stub_editor, notes_dir):
        frame = make_frame()
        assert sorted(note.title for note in frame.tv_notebook) == [
            "archive/old_note",
            "personal/journal",
            "root_note",
            "work/project_a",
            "work/project_b",
        ]

        type_text(frame, "project")
        assert list_lines(frame) == ["work/project_b", "work/project_a"]
        assert highlighted(frame) == []

        press(frame, "esc")
        type_text(frame, "work/new_project.md")
        press(frame, "enter")
        assert (notes_dir / "work" / "new_project.md").is_file()
        assert stub_editor.calls() == [[str(notes_dir / "work" / "new_project.md")]]

    def test_only_configured_extensions_are_listed(self, make_frame, write_notes, notes_dir):
        write_notes(notes_dir, {"note1.txt": "", "note2.md": "", "note3.rst": ""})
        frame = make_frame()
        assert list_lines(frame) == ["note2", "note1"]

    def test_excluded_directories_are_not_listed(self, notebook, notes_dir):
        for directory in ("active", "archive", "backup"):
            (notes_dir / directory).mkdir()
            (notes_dir / directory / "note.txt").write_text("")

        nb = notebook(notes_dir, exclude=["archive", "backup"])

        assert [note.title for note in nb] == ["active/note"]

    def test_new_notes_use_configured_extension(self, make_frame, notes_dir):
        frame = make_frame(extension=".md", extensions=[".txt", ".md"])
        type_text(frame, "new_note")
        press(frame, "enter")
        assert sorted(os.listdir(notes_dir)) == ["new_note.md"]


class TestWatcherIntegration:
    @pytest.mark.slow
    def test_external_notes_appear_in_running_ui(self, make_frame, notes_dir):
        frame = make_frame({"initial.txt": ""})
        expected = {"initial", *(f"note_{i}" for i in range(5))}

        def create(loop, _data):
            for i in range(5):
                (notes_dir / f"note_{i}.txt").write_text(f"Note {i}")

        frame.loop.set_alarm_in(0, create)
        assert run_headless(frame.loop, lambda: set(list_lines(frame)) == expected)

    @pytest.mark.slow
    def test_external_and_api_notes_are_both_tracked(self, notebook, notes_dir, wait_until):
        nb = notebook(notes_dir)
        nb.create("api_note_1")
        nb.create("api_note_2")
        (notes_dir / "external_1.txt").write_text("External 1")
        (notes_dir / "external_2.txt").write_text("External 2")

        wait_until(lambda: len(nb) == 4)
        assert sorted(note.title for note in nb.search("")) == [
            "api_note_1",
            "api_note_2",
            "external_1",
            "external_2",
        ]

    def test_concurrent_creates(self, notebook, notes_dir):
        nb = notebook(notes_dir)
        errors = []

        def create_notes():
            try:
                for i in range(10):
                    nb.create(f"concurrent_{threading.current_thread().name}_{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_notes, name=f"t{i}") for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(nb) == 30
        assert len(os.listdir(notes_dir)) == 30


class TestErrorRecovery:
    def test_invalid_title_then_valid_title(self, notebook, notes_dir):
        nb = notebook(notes_dir)
        with pytest.raises(tv_notebook.InvalidNoteTitleError):
            nb.create("/absolute")

        note = nb.create("valid_note")

        assert nb.search("valid") == [note]
        assert os.listdir(notes_dir) == ["valid_note.txt"]

    def test_duplicate_title_reports_existing_note(self, notebook, notes_dir):
        nb = notebook(notes_dir)
        first = nb.create("duplicate")

        with pytest.raises(tv_notebook.NoteAlreadyExistsError) as excinfo:
            nb.create("duplicate")

        assert excinfo.value.note == first
        assert nb.search("") == [first]

    def test_ui_keeps_working_after_editor_failure(
        self, make_frame, stub_editor, notes_dir, monkeypatch
    ):
        monkeypatch.setenv("STUB_EDITOR_EXIT", "1")
        frame = make_frame({"test.txt": "Test", "other.txt": ""})
        type_text(frame, "test")
        press(frame, "enter")
        assert status_line(frame) == "Editor exited with status 1"
        assert highlighted(frame) == ["test"]

        press(frame, "esc", "esc")
        type_text(frame, "oth")
        assert list_lines(frame) == ["other"]
        assert search_line(frame) == "other"


class Terminal:
    """tv3 running under `sh` on a pseudo-terminal.

    The shell turns off IXANY (so a stopped terminal is resumed only by
    Ctrl-Q, as on Linux), runs tv3, then records tv3's exit status and the
    terminal settings left behind.
    """

    def __init__(self, tmp_path, notes_dir, editor):
        self.status_file = tmp_path / "status"
        self.stty_after = tmp_path / "stty-after"
        tv3 = shlex.join([sys.executable, "-m", "tv3", "-e", editor, str(notes_dir)])
        script = (
            f"stty -ixany; {tv3}; echo $? > {shlex.quote(str(self.status_file))}; "
            f"stty -a > {shlex.quote(str(self.stty_after))}"
        )
        env = dict(os.environ, HOME=str(tmp_path), TERM="xterm-256color")
        self.output = b""
        self.fd, terminal = os.openpty()
        fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self.process = subprocess.Popen(
            ["/bin/sh", "-c", script],
            stdin=terminal,
            stdout=terminal,
            stderr=terminal,
            env=env,
            start_new_session=True,
        )
        os.close(terminal)

    def read(self, timeout):
        """Read output for up to `timeout` seconds; return what arrived."""
        received = b""
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            readable, _, _ = select.select([self.fd], [], [], remaining)
            if not readable:
                break
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                break
            if not data:
                break
            received += data
        self.output += received
        return received

    def read_until(self, text, count=1, timeout=10):
        """Read until `text` has been output `count` times in all; return whether it was."""
        deadline = time.monotonic() + timeout
        while self.output.count(text) < count and time.monotonic() < deadline:
            self.read(0.05)
        return self.output.count(text) >= count

    def send(self, data):
        os.write(self.fd, data)

    def wait(self, timeout=10):
        """Wait for the shell to exit; return tv3's exit status, or None on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read(0.05)
            if self.process.poll() is not None:
                return int(self.status_file.read_text())
        return None

    def close(self):
        """Kill anything still running; closing the terminal first releases output it stopped."""
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
        os.close(self.fd)
        self.process.wait()


BOX_CORNER = "┌".encode()


def _stty_flags(text):
    return set(text.split())


@pytest.fixture
def terminal(tmp_path, notes_dir):
    """Factory: tv3 on a pseudo-terminal with the given editor command."""
    terminals = []

    def start(editor="true"):
        term = Terminal(tmp_path, notes_dir, editor)
        terminals.append(term)
        assert term.read_until(b"Find or Create"), term.output
        return term

    yield start
    for term in terminals:
        term.close()


@pytest.mark.slow
class TestRealTerminal:
    def test_ctrl_q_quits(self, terminal):
        term = terminal()
        term.send(b"\x11")
        assert term.wait() == 0

    def test_ctrl_s_does_not_stop_output(self, terminal):
        term = terminal()
        term.send(b"\x13")

        term.send(b"qzx")

        assert term.read_until(b"qzx"), term.output
        term.send(b"\x18")
        assert term.wait() == 0

    def test_flow_control_is_on_in_the_editor_and_after_exit(
        self, terminal, tmp_path, write_notes, notes_dir
    ):
        write_notes(notes_dir, {"a.txt": ""})
        stty_editor = tmp_path / "stty-editor"
        stty_in_editor = tmp_path / "stty-in-editor"
        stty_editor.write_text(f"#!/bin/sh\nstty -a > {shlex.quote(str(stty_in_editor))}\n")
        stty_editor.chmod(0o755)
        term = terminal(editor=str(stty_editor))
        drawn = term.output.count(BOX_CORNER)

        term.send(b"\x1b[B\r")
        assert term.read_until(BOX_CORNER, count=drawn + 1), term.output
        assert stty_in_editor.exists()
        term.send(b"\x11")

        assert term.wait() == 0
        assert {"ixon", "-ixany"} <= _stty_flags(stty_in_editor.read_text())
        assert {"ixon", "-ixany"} <= _stty_flags(term.stty_after.read_text())

    def test_rename_delete_and_preview_keys_reach_tv3(
        self, terminal, write_notes, notes_dir, wait_until
    ):
        write_notes(notes_dir, {"a.txt": "alpha body", "b.txt": "bravo body"})
        term = terminal()

        term.send(b"\x10")
        assert term.read_until(b"bravo body"), term.output

        term.send(b"\x1b[B\x12")
        assert term.read_until(b"Rename:"), term.output
        term.send(b"2\r")
        wait_until(lambda: sorted(p.name for p in notes_dir.iterdir()) == ["a.txt", "b2.txt"])

        term.send(b"\x0b")
        assert term.read_until(b"Delete 'b2'? (y/N)"), term.output
        term.send(b"Y")
        assert term.read_until(b"Deleted 'b2'"), term.output
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt"]

        term.send(b"\x18")
        assert term.wait() == 0

    def test_ctrl_q_quits_from_a_prompt(self, terminal, write_notes, notes_dir):
        write_notes(notes_dir, {"a.txt": ""})
        term = terminal()

        term.send(b"\x1b[B\x12")
        assert term.read_until(b"Rename:"), term.output
        term.send(b"\x11")

        assert term.wait() == 0
        assert sorted(p.name for p in notes_dir.iterdir()) == ["a.txt"]
