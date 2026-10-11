"""Persistent note storage and search."""

import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

logger = logging.getLogger("tv3")

WATCHDOG_STOP_TIMEOUT = 5  # seconds

EDITOR_TEMP_SUFFIXES = ("~", ".swp", ".swx")


class Error(Exception):
    """Base class for exceptions in this module."""


class NewNoteBookError(Error):
    """Raised when initialising a new NoteBook fails."""


class NewNoteError(Error):
    """Raised when creating a new note fails."""


class NoteAlreadyExistsError(NewNoteError):
    """Raised when creating a note whose file already exists.

    `note` is the existing PlainTextNote.
    """

    def __init__(self, message: str, note: "PlainTextNote"):
        super().__init__(message)
        self.note = note


class InvalidNoteTitleError(NewNoteError):
    """Raised when creating a note with a title that cannot be used."""


def _is_editor_temp(name: str) -> bool:
    """True for editor backup, swap and lock file names."""
    return (
        name.endswith(EDITOR_TEMP_SUFFIXES)
        or name.startswith(".#")
        or (len(name) > 1 and name.startswith("#") and name.endswith("#"))
    )


def _is_inside(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory + os.sep)


def _probe_case_insensitive(directory: str) -> bool:
    """Detect whether `directory` is on a case-insensitive filesystem."""
    try:
        with tempfile.NamedTemporaryFile(prefix=".tv3-CaseProbe-", dir=directory) as probe:
            head, name = os.path.split(probe.name)
            return os.path.exists(os.path.join(head, name.lower()))
    except OSError as e:
        logger.warning(f"Could not probe case sensitivity of {directory}: {e}")
        return False


class PlainTextNote:
    """A note, stored as a plain text file on disk."""

    def __init__(self, title: str, notebook: "PlainTextNoteBook", extension: str):
        self._title = title
        self._extension = extension
        self._abspath = os.path.join(notebook.path, title + extension)

    @property
    def title(self) -> str:
        return self._title

    @property
    def extension(self) -> str:
        return self._extension

    @property
    def abspath(self) -> str:
        return self._abspath

    @property
    def contents(self) -> str:
        """Read and return the contents of the note file."""
        try:
            with open(self.abspath, "rb") as fp:
                return fp.read().decode("utf-8", errors="replace")
        except OSError as e:
            logger.error(f"Error reading note {self.abspath}: {e}")
            return ""

    @property
    def mtime(self) -> float:
        """Get the modification time of the note file."""
        try:
            return os.path.getmtime(self.abspath)
        except OSError as e:
            logger.error(f"Error getting mtime for {self.abspath}: {e}")
            return 0.0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PlainTextNote):
            return NotImplemented
        return self.abspath == other.abspath

    def __hash__(self) -> int:
        return hash(self.abspath)

    def __repr__(self) -> str:
        return f"PlainTextNote({self.abspath!r})"


def brute_force_search(notebook, query):
    """Return all notes in `notebook` that match `query`.

    Args:
        notebook: The PlainTextNoteBook to search in.
        query: Space-separated search terms to match.

    Returns:
        List of PlainTextNote objects that match all search terms.

    The search is case-insensitive if all search words are lowercase,
    otherwise it's case-sensitive. All words must match (AND operation).
    """
    search_words = query.strip().split()
    matching_notes = []
    for note in notebook:
        match = True
        for search_word in search_words:
            if search_word.islower():
                in_title = search_word in note.title.lower()
                in_contents = search_word in note.contents.lower()
            else:
                in_title = search_word in note.title
                in_contents = search_word in note.contents
            if (not in_title) and (not in_contents):
                match = False
        if match:
            matching_notes.append(note)
    return matching_notes


class PlainTextNoteBook:
    """A NoteBook that stores its notes as a directory of plain text files.

    Notes are kept in a dict keyed by their path relative to the notebook
    (casefolded on case-insensitive filesystems). A watchdog observer keeps
    the dict in sync with the directory. Only `create` writes to disk.

    `on_change`, if set, is called with no arguments from the watcher thread
    after an event that added, removed or modified a note.
    """

    def __init__(
        self,
        path: str,
        extension: str,
        extensions: Iterable[str],
        search_function=brute_force_search,
        exclude: Iterable[str] | None = None,
    ):
        """Make a new PlainTextNoteBook for the given path."""
        expanded = os.path.expanduser(path)
        if not os.path.isdir(expanded):
            logger.debug(f"{expanded} doesn't exist, creating it")
            try:
                os.makedirs(expanded)
            except OSError as e:
                raise NewNoteBookError(f"{expanded} could not be created: {e}") from e
        self._path = os.path.realpath(expanded)
        self.extension = self._dotted(extension)
        self.extensions = [self._dotted(ext) for ext in extensions]
        if self.extension not in self.extensions:
            self.extensions.append(self.extension)
        self.search_function = search_function
        self.on_change: Callable[[], None] | None = None
        self._case_insensitive = _probe_case_insensitive(self._path)
        self.exclude = list(exclude or [])
        self._exclude = {self._fold(name) for name in self.exclude}
        self._notes: dict[str, PlainTextNote] = {}
        self._notes_lock = threading.Lock()

        self._observer: BaseObserver | None = Observer()
        self._observer.schedule(FileEventHandler(self), self._path, recursive=True)
        self._observer.start()
        try:
            self._register_tree(self._path)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _dotted(ext: str) -> str:
        return ext if not ext or ext.startswith(".") else "." + ext

    @property
    def path(self) -> str:
        return self._path

    def search(self, query):
        """Return a sequence of Notes that match the given query."""
        return self.search_function(self, query)

    def create(self, title: str) -> PlainTextNote:
        """Create an empty note file for `title` and add it to the notebook.

        A title ending in a recognised extension keeps it; otherwise the
        default extension is appended.

        Raises:
            InvalidNoteTitleError: the title cannot be used; nothing was written.
            NoteAlreadyExistsError: a note file already exists at that path.
            NewNoteError: the file could not be written.
        """
        title = title.strip()
        if not title:
            raise InvalidNoteTitleError("Note title is empty")
        if "\0" in title:
            raise InvalidNoteTitleError("Note title contains a NUL character")
        if os.path.isabs(title):
            raise InvalidNoteTitleError(f"Note title is an absolute path: {title}")
        has_extension = os.path.splitext(title)[1] in self.extensions
        relpath = title if has_extension else title + self.extension
        reason = self._rejection(relpath)
        if reason is None and _is_editor_temp(os.path.basename(title)):
            reason = f"looks like an editor backup file: {title}"
        if reason is not None:
            raise InvalidNoteTitleError(f"Invalid note title: {reason}")
        abspath = os.path.join(self._path, relpath)
        if not _is_inside(os.path.realpath(abspath), self._path):
            raise InvalidNoteTitleError(f"Note would be outside the notebook: {title}")

        with self._notes_lock:
            existing = self._notes.get(self._key(relpath))
            if existing is None and os.path.lexists(abspath):
                if not os.path.isfile(abspath):
                    raise InvalidNoteTitleError(f"Something other than a note exists at {relpath}")
                existing = self._add_locked(relpath)
            if existing is not None:
                raise NoteAlreadyExistsError(f"Note already exists: {existing.title}", existing)
            try:
                os.makedirs(os.path.dirname(abspath), exist_ok=True)
                with open(abspath, "x"):
                    pass
            except OSError as e:
                raise NewNoteError(f"Could not create {relpath}: {e.strerror or e}") from e
            note = self._add_locked(relpath)
        logger.debug(f"Created note {abspath}")
        return note

    def add_new(self, filename: str) -> PlainTextNote:
        """Alias of `create`, kept for callers not yet using it."""
        return self.create(filename)

    def _fold(self, s: str) -> str:
        return s.casefold() if self._case_insensitive else s

    def _key(self, relpath: str) -> str:
        return self._fold(relpath)

    def _rejection(self, relpath: str) -> str | None:
        """Why `relpath` (relative, with extension) is not a valid note path, or None."""
        parts = relpath.split(os.sep)
        for part in parts:
            if part == "":
                return f"empty path component in {relpath}"
            if part == os.pardir:
                return f"'..' is not allowed: {relpath}"
            if part.startswith("."):
                return f"hidden file or directory: {relpath}"
            if self._fold(part) in self._exclude:
                return f"'{part}' is excluded: {relpath}"
        name = parts[-1]
        if _is_editor_temp(name):
            return f"looks like an editor backup file: {relpath}"
        if os.path.splitext(name)[1] not in self.extensions:
            return f"unrecognised extension: {relpath}"
        return None

    def _relpath(self, abspath: str, real_parent: str | None = None) -> str | None:
        """`abspath` relative to the notebook, resolving symlinks in its directory.

        `real_parent`, if given, is the already-resolved directory of `abspath`.
        """
        if real_parent is None:
            real_parent = os.path.realpath(os.path.dirname(abspath))
        if not _is_inside(real_parent, self._path):
            return None
        return os.path.relpath(os.path.join(real_parent, os.path.basename(abspath)), self._path)

    def _index_relpath(self, abspath: str, real_parent: str | None = None) -> str | None:
        """The relative path of `abspath` if it should be indexed, else None."""
        relpath = self._relpath(abspath, real_parent)
        if relpath is None or relpath == os.curdir or self._rejection(relpath) is not None:
            return None
        if os.path.islink(abspath) and not _is_inside(os.path.realpath(abspath), self._path):
            return None
        return relpath

    def _should_index(self, abspath: str) -> bool:
        """True if `abspath` names a note file this notebook should track."""
        return self._index_relpath(abspath) is not None

    def _add_locked(self, relpath: str) -> PlainTextNote:
        title, extension = os.path.splitext(relpath)
        note = PlainTextNote(title, self, extension)
        self._notes[self._key(relpath)] = note
        return note

    def _register(self, abspath: str, real_parent: str | None = None) -> bool:
        """Track the existing note file at `abspath`. Never writes to disk.

        Returns True if a note was added.
        """
        relpath = self._index_relpath(abspath, real_parent)
        if relpath is None or not os.path.isfile(abspath):
            return False
        with self._notes_lock:
            if self._key(relpath) in self._notes:
                return False
            self._add_locked(relpath)
        return True

    def _unregister(self, abspath: str) -> bool:
        """Stop tracking the note at `abspath`. Returns True if one was removed."""
        relpath = self._relpath(abspath)
        if relpath is None:
            return False
        with self._notes_lock:
            return self._notes.pop(self._key(relpath), None) is not None

    def _register_tree(self, directory: str) -> bool:
        """Register every note file under `directory`. Returns True if any were added."""
        if not _is_inside(os.path.realpath(directory), self._path):
            return False
        added = False
        for root, dirs, files in os.walk(directory):
            dirs[:] = [
                d for d in dirs if not d.startswith(".") and self._fold(d) not in self._exclude
            ]
            real_root = os.path.realpath(root)
            for filename in files:
                if self._register(os.path.join(root, filename), real_root):
                    added = True
        return added

    def _unregister_tree(self, directory: str) -> bool:
        """Stop tracking every note under `directory`. Returns True if any were removed."""
        relpath = self._relpath(directory)
        if relpath is None:
            return False
        prefix = "" if relpath == os.curdir else self._key(relpath) + os.sep
        with self._notes_lock:
            keys = [key for key in self._notes if key.startswith(prefix)]
            for key in keys:
                del self._notes[key]
        return bool(keys)

    def _is_registered(self, abspath: str) -> bool:
        relpath = self._relpath(abspath)
        if relpath is None:
            return False
        with self._notes_lock:
            return self._key(relpath) in self._notes

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def close(self) -> None:
        """Stop the file watcher."""
        if self._observer is not None:
            logger.debug("Stopping file observer")
            self._observer.stop()
            self._observer.join(timeout=WATCHDOG_STOP_TIMEOUT)
            self._observer = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __len__(self) -> int:
        with self._notes_lock:
            return len(self._notes)

    def __iter__(self) -> Iterator[PlainTextNote]:
        with self._notes_lock:
            return iter(list(self._notes.values()))

    def __contains__(self, note: object) -> bool:
        if not isinstance(note, PlainTextNote):
            return False
        relpath = self._relpath(note.abspath)
        if relpath is None:
            return False
        with self._notes_lock:
            return self._notes.get(self._key(relpath)) == note


class FileEventHandler(FileSystemEventHandler):
    """Keep a notebook in sync with filesystem events.

    Errors are logged per event so the observer thread keeps running.
    """

    def __init__(self, notebook: PlainTextNoteBook):
        self._notebook = notebook
        super().__init__()

    def _handle(self, event: FileSystemEvent, apply: Callable[[], bool]) -> None:
        try:
            if apply():
                self._notebook._notify()
        except Exception:
            logger.exception(f"Error handling {event.event_type} event for {event.src_path!r}")

    def _added(self, path: str, is_directory: bool) -> bool:
        nb = self._notebook
        return nb._register_tree(path) if is_directory else nb._register(path)

    def _removed(self, path: str, is_directory: bool) -> bool:
        nb = self._notebook
        return nb._unregister_tree(path) if is_directory else nb._unregister(path)

    def on_created(self, event: FileSystemEvent) -> None:
        path = os.fsdecode(event.src_path)
        logger.debug(f"Detected created {path}")
        self._handle(event, lambda: self._added(path, event.is_directory))

    def on_deleted(self, event: FileSystemEvent) -> None:
        path = os.fsdecode(event.src_path)
        logger.debug(f"Detected deleted {path}")
        self._handle(event, lambda: self._removed(path, event.is_directory))

    def on_moved(self, event: FileSystemEvent) -> None:
        src = os.fsdecode(event.src_path)
        dest = os.fsdecode(event.dest_path)
        logger.debug(f"Detected move {src} -> {dest}")

        def apply() -> bool:
            removed = self._removed(src, event.is_directory)
            added = self._added(dest, event.is_directory)
            return removed or added or self._notebook._is_registered(dest)

        self._handle(event, apply)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = os.fsdecode(event.src_path)
        nb = self._notebook
        self._handle(event, lambda: nb._register(path) or nb._is_registered(path))
