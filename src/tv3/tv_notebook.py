"""Persistent note storage and search."""

import logging
import os
import tempfile
import threading
import unicodedata
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


def _swap_ascii_case(name: str) -> str:
    return "".join(c.swapcase() if c.isascii() else c for c in name)


def _case_variant_is_same(path: str) -> bool | None:
    """Whether `path` with its last component's case swapped names the same entry.

    None if that cannot be told: the name has no ASCII letters or `path` is
    unreadable.
    """
    head, name = os.path.split(path)
    swapped = _swap_ascii_case(name)
    if swapped == name:
        return None
    try:
        original = os.lstat(path)
    except OSError:
        return None
    try:
        return os.path.samestat(original, os.lstat(os.path.join(head, swapped)))
    except FileNotFoundError:
        return False
    except OSError:
        return None


def _probe_case_insensitive(directory: str) -> bool:
    """Detect whether `directory` is on a case-insensitive filesystem.

    Looks up an existing entry, then the directory itself, under a case-swapped
    name. Only if neither has a usable name does it write a temporary file.
    If that fails too, it assumes case-insensitive: the notebook then treats
    case variants as one note, so it never indexes two notes for one file or
    creates a second file that differs only in case.
    """
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                same = _case_variant_is_same(entry.path)
                if same is not None:
                    return same
    except OSError as e:
        logger.debug(f"Could not list {directory} to probe case sensitivity: {e}")
    same = _case_variant_is_same(directory)
    if same is not None:
        return same
    try:
        with tempfile.NamedTemporaryFile(prefix=".tv3-CaseProbe-", dir=directory) as probe:
            same = _case_variant_is_same(probe.name)
            if same is not None:
                return same
    except OSError as e:
        logger.warning(f"Could not probe case sensitivity of {directory}: {e}")
    logger.warning(f"Assuming {directory} is on a case-insensitive filesystem")
    return True


def _path_limit(path: str, name: str) -> int | None:
    """`os.pathconf(path, name)`, or None if there is no known limit."""
    try:
        limit = os.pathconf(path, name)
    except (AttributeError, ValueError, OSError):
        return None
    return limit if limit > 0 else None


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

    Notes are kept in a dict keyed by their path relative to the notebook,
    NFC-normalised and, on case-insensitive filesystems, casefolded. A
    watchdog observer keeps the dict in sync with the directory. Only
    `create` writes to disk.

    Symlinks: the notebook directory itself may be a symlink and is resolved.
    Inside it, symlinked files and symlinked directories are not indexed, so
    no note appears twice and symlink loops are not followed. A title that
    passes through a symlinked directory is stored under its resolved path.

    If the notebook directory is deleted or moved, every note is dropped, an
    error is logged and the watcher stops; a new notebook must be opened.

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
        self._extension_keys = {self._fold(ext) for ext in self.extensions}
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
        default extension is appended. The returned note's title uses the
        names found on disk, which can differ from `title` in case or Unicode
        normalisation.

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
        typed_relpath = title if self._has_extension(title) else title + self.extension
        reason = self._rejection(typed_relpath)
        if reason is None and _is_editor_temp(os.path.basename(title)):
            reason = f"looks like an editor backup file: {title}"
        if reason is not None:
            raise InvalidNoteTitleError(f"Invalid note title: {reason}")
        relpath = self._relpath(os.path.join(self._path, typed_relpath))
        if relpath is None or relpath == os.curdir:
            raise InvalidNoteTitleError(f"Note would be outside the notebook: {title}")
        abspath = os.path.join(self._path, relpath)
        reason = self._rejection(relpath) or self._length_rejection(relpath, abspath)
        if reason is not None:
            raise InvalidNoteTitleError(f"Invalid note title: {reason}")

        with self._notes_lock:
            key = self._key(relpath)
            existing = self._notes.get(key)
            if existing is not None and not os.path.isfile(existing.abspath):
                del self._notes[key]
                existing = None
            if existing is None and os.path.lexists(abspath):
                if os.path.islink(abspath) or not os.path.isfile(abspath):
                    raise InvalidNoteTitleError(f"Something other than a note exists at {relpath}")
                existing = self._add_locked(self._on_disk_relpath(relpath))
            if existing is not None:
                raise NoteAlreadyExistsError(f"Note already exists: {existing.title}", existing)
            self._write_empty_file(abspath, relpath)
            note = self._add_locked(self._on_disk_relpath(relpath))
        logger.debug(f"Created note {abspath}")
        return note

    def add_new(self, filename: str) -> PlainTextNote:
        """Same as `create`."""
        return self.create(filename)

    def _write_empty_file(self, abspath: str, relpath: str) -> None:
        """Create `abspath` and any missing parent directories.

        On failure, removes the directories it created and raises NewNoteError.
        """
        missing = []
        directory = os.path.dirname(abspath)
        while directory != self._path and not os.path.lexists(directory):
            missing.append(directory)
            directory = os.path.dirname(directory)
        created = []
        try:
            for directory in reversed(missing):
                os.mkdir(directory)
                created.append(directory)
            with open(abspath, "x"):
                pass
        except OSError as e:
            for directory in reversed(created):
                try:
                    os.rmdir(directory)
                except OSError as rmdir_error:
                    logger.warning(f"Could not remove {directory}: {rmdir_error}")
            raise NewNoteError(f"Could not create {relpath}: {e.strerror or e}") from e

    def _fold(self, s: str) -> str:
        return s.casefold() if self._case_insensitive else s

    def _key(self, relpath: str) -> str:
        return self._fold(unicodedata.normalize("NFC", relpath))

    def _has_extension(self, name: str) -> bool:
        return self._fold(os.path.splitext(name)[1]) in self._extension_keys

    def _on_disk_relpath(self, relpath: str) -> str:
        """`relpath` spelled with the names of the existing entries it matches."""
        parts = []
        directory = self._path
        for part in relpath.split(os.sep):
            try:
                names = os.listdir(directory)
            except OSError:
                names = []
            if part not in names:
                key = self._key(part)
                part = next((name for name in names if self._key(name) == key), part)
            parts.append(part)
            directory = os.path.join(directory, part)
        return os.path.join(*parts)

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
        if not self._has_extension(name):
            return f"unrecognised extension: {relpath}"
        return None

    def _length_rejection(self, relpath: str, abspath: str) -> str | None:
        """Why `relpath` is too long for the filesystem, or None."""
        name_max = _path_limit(self._path, "PC_NAME_MAX")
        if name_max is not None:
            for part in relpath.split(os.sep):
                if len(os.fsencode(part)) > name_max:
                    return f"name longer than {name_max} bytes: {part}"
        path_max = _path_limit(self._path, "PC_PATH_MAX")
        if path_max is not None and len(os.fsencode(abspath)) >= path_max:
            return f"path longer than {path_max - 1} bytes: {relpath}"
        return None

    def _relpath(self, abspath: str, real_parent: str | None = None) -> str | None:
        """`abspath` relative to the notebook, resolving symlinks in its directory.

        `real_parent`, if given, is the already-resolved directory of `abspath`.
        The notebook directory itself is `os.curdir`.
        """
        if abspath == self._path:
            return os.curdir
        if real_parent is None:
            real_parent = os.path.realpath(os.path.dirname(abspath))
        if not _is_inside(real_parent, self._path):
            return None
        return os.path.relpath(os.path.join(real_parent, os.path.basename(abspath)), self._path)

    def _indexable_relpath(self, abspath: str, real_parent: str | None = None) -> str | None:
        """The relative path of `abspath` if it is a note file to index, else None.

        The one test of what is a note, shared by the scan and the watcher.
        """
        relpath = self._relpath(abspath, real_parent)
        if relpath is None or relpath == os.curdir or self._rejection(relpath) is not None:
            return None
        if os.path.islink(abspath) or not os.path.isfile(abspath):
            return None
        return relpath

    def _add_locked(self, relpath: str) -> PlainTextNote:
        title, extension = os.path.splitext(relpath)
        note = PlainTextNote(title, self, extension)
        self._notes[self._key(relpath)] = note
        return note

    def _register(self, abspath: str, real_parent: str | None = None) -> bool:
        """Track the existing note file at `abspath`. Never writes to disk.

        Returns True if a note was added.
        """
        relpath = self._indexable_relpath(abspath, real_parent)
        if relpath is None:
            return False
        with self._notes_lock:
            if self._key(relpath) in self._notes:
                return False
            self._add_locked(relpath)
        return True

    def _unregister(self, abspath: str, only_if_missing: bool = False) -> bool:
        """Stop tracking the note at `abspath`. Returns True if one was removed.

        With `only_if_missing`, a note whose file exists is kept.
        """
        relpath = self._relpath(abspath)
        if relpath is None:
            return False
        with self._notes_lock:
            key = self._key(relpath)
            note = self._notes.get(key)
            if note is None or (only_if_missing and os.path.isfile(note.abspath)):
                return False
            del self._notes[key]
        return True

    def _register_tree(self, directory: str) -> bool:
        """Register every note file under `directory`. Returns True if any were added."""
        if directory != self._path and os.path.islink(directory):
            return False
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

    def _unregister_tree(self, directory: str, only_if_missing: bool = False) -> bool:
        """Stop tracking every note under `directory`. Returns True if any were removed.

        With `only_if_missing`, notes whose files exist are kept. The notebook
        directory itself going away drops every note.
        """
        relpath = self._relpath(directory)
        if relpath is None:
            return False
        if relpath == os.curdir:
            self._root_removed()
            return True
        prefix = self._key(relpath) + os.sep
        with self._notes_lock:
            keys = [
                key
                for key, note in self._notes.items()
                if key.startswith(prefix) and not (only_if_missing and os.path.isfile(note.abspath))
            ]
            for key in keys:
                del self._notes[key]
        return bool(keys)

    def _root_removed(self) -> None:
        with self._notes_lock:
            self._notes.clear()
        logger.error(
            f"Notes directory {self._path} was deleted or moved; no longer watching it for changes"
        )

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
        """Stop the file watcher. Safe to call more than once, from any thread."""
        with self._notes_lock:
            observer, self._observer = self._observer, None
        if observer is None:
            return
        logger.debug("Stopping file observer")
        observer.stop()
        if threading.current_thread() is not observer:
            observer.join(timeout=WATCHDOG_STOP_TIMEOUT)

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

    def _removed(self, path: str, is_directory: bool, only_if_missing: bool = False) -> bool:
        nb = self._notebook
        if is_directory:
            return nb._unregister_tree(path, only_if_missing)
        return nb._unregister(path, only_if_missing)

    def on_created(self, event: FileSystemEvent) -> None:
        path = os.fsdecode(event.src_path)
        logger.debug(f"Detected created {path}")
        self._handle(event, lambda: self._added(path, event.is_directory))

    def on_deleted(self, event: FileSystemEvent) -> None:
        # Events arrive after the fact; a file recreated since its deletion stays.
        path = os.fsdecode(event.src_path)
        logger.debug(f"Detected deleted {path}")
        self._handle(event, lambda: self._removed(path, event.is_directory, only_if_missing=True))

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
