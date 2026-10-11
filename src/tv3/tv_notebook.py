"""Persistent note storage and search."""

import logging
import os
import stat
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from functools import cached_property
from typing import NamedTuple

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from tv3 import ranking

logger = logging.getLogger("tv3")

WATCHDOG_STOP_TIMEOUT = 5  # seconds

EDITOR_TEMP_SUFFIXES = ("~", ".swp", ".swx")

MAX_INDEXED_BYTES = 1024 * 1024


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


class _Stat(NamedTuple):
    """A note file's modification time, before its contents have been read."""

    mtime: float


class _Contents(NamedTuple):
    """A note file as it was when last read.

    `text` is the title, a newline and the contents; `folded` is the
    lowercased title and the lowercased contents, joined the same way, and is
    `text` itself when lowercasing changes nothing. A search word never
    contains whitespace, so it occurs in `text` exactly when it occurs in the
    title or in the contents. The contents are the first MAX_INDEXED_BYTES
    bytes of the file.
    """

    mtime: float
    text: str
    folded: str


class PlainTextNote:
    """A note, stored as a plain text file on disk.

    `contents` and `mtime` come from an in-memory copy of the file, which
    the notebook passes in as `cached` or which is read on first use. The
    notebook re-reads it when its watcher reports a change.
    """

    def __init__(
        self,
        title: str,
        notebook: "PlainTextNoteBook",
        extension: str,
        cached: _Stat | _Contents | None = None,
    ):
        self._title = title
        self._extension = extension
        self._abspath = os.path.join(notebook.path, title + extension)
        self._notebook = notebook
        self._lock = notebook._notes_lock
        self._cached = cached

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
        """The first MAX_INDEXED_BYTES bytes of the note file, decoded as UTF-8."""
        return self._loaded().text[len(self._title) + 1 :]

    @property
    def mtime(self) -> float:
        """The modification time of the note file."""
        cached = self._cached
        if cached is None:
            cached = self._stat()
            self._store_if_unchanged(None, cached)
        return cached.mtime

    def _loaded(self) -> _Contents:
        """The cached file, reading it first if it has not been read."""
        cached = self._cached
        if isinstance(cached, _Contents):
            return cached
        contents = self._read()
        self._store_if_unchanged(cached, contents)
        return contents

    def _refresh(self) -> bool:
        """Re-read the note file. Returns True if its mtime or contents changed.

        If another read is stored while this one runs, reads again, so what
        this stores is never older than what it replaces.
        """
        old = self._cached
        seen = old
        while True:
            new = self._read()
            if self._store_if_unchanged(seen, new):
                break
            seen = self._cached
        if old is None or old.mtime != new.mtime:
            return True
        return isinstance(old, _Contents) and old.text != new.text

    def _store_if_unchanged(self, seen: _Stat | _Contents | None, new: _Stat | _Contents) -> bool:
        """Cache `new` unless the cache has changed since it was `seen`.

        A read that raced with `_refresh` must not overwrite what `_refresh`
        stored, which is at least as recent. Returns True if `new` was stored.
        """
        with self._lock:
            if self._cached is not seen:
                return False
            self._cached = new
            return True

    def _stat(self) -> _Stat:
        try:
            return _Stat(os.stat(self._abspath).st_mtime)
        except OSError as e:
            logger.error(f"Error getting mtime for {self._abspath}: {e}")
            return _Stat(0.0)

    def _read(self) -> _Contents:
        return self._notebook._read(self._abspath, self._title)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PlainTextNote):
            return NotImplemented
        return self.abspath == other.abspath

    def __hash__(self) -> int:
        return hash(self.abspath)

    def __repr__(self) -> str:
        return f"PlainTextNote({self.abspath!r})"


@dataclass(frozen=True, eq=False)
class Result:
    """A note found by `PlainTextNoteBook.find`, with what to highlight.

    Spans are half-open `(start, end)` character ranges, sorted and
    non-overlapping: `title_spans` index `note.title`, `snippet_spans` index
    `snippet`. `snippet` is a line from the note's contents, or None.

    Exact-match title spans and the snippet are worked out on first access,
    from the note's contents at that time, so a search costs nothing for
    results that are never shown.
    """

    note: PlainTextNote
    _words: tuple[str, ...] = field(repr=False)
    _snippet_width: int = field(repr=False)
    _fuzzy_spans: tuple[ranking.Span, ...] | None = field(default=None, repr=False)

    @cached_property
    def title_spans(self) -> tuple[ranking.Span, ...]:
        if self._fuzzy_spans is not None:
            return self._fuzzy_spans
        return ranking.word_spans(self.note.title, self._words)

    @cached_property
    def _snippet(self) -> tuple[str, tuple[ranking.Span, ...]] | None:
        return ranking.snippet(
            self.note.title, self.note.contents, self._words, self._snippet_width
        )

    @property
    def snippet(self) -> str | None:
        snippet = self._snippet
        return snippet[0] if snippet is not None else None

    @property
    def snippet_spans(self) -> tuple[ranking.Span, ...]:
        snippet = self._snippet
        return snippet[1] if snippet is not None else ()


def brute_force_search(notebook, query):
    """Return all notes in `notebook` that match `query`.

    Args:
        notebook: The PlainTextNoteBook to search in.
        query: Space-separated search terms to match.

    Returns:
        List of PlainTextNote objects that match all search terms.

    Every word must occur in the note's title or contents (AND). A word that
    is all lowercase matches case-insensitively, any other word matches
    case-sensitively. Searches the notes' cached contents; it reads only
    notes that have not been read yet.
    """
    words = [(word, word.islower()) for word in query.split()]
    if not words:
        return list(notebook)
    matching_notes = []
    for note in notebook:
        cached = note._loaded()
        for word, fold in words:
            if word not in (cached.folded if fold else cached.text):
                break
        else:
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

    Each note keeps its first MAX_INDEXED_BYTES bytes and its mtime in
    memory, so a search reads no files. A background thread reads every
    note found by the initial scan, and a search with any words waits for
    it; notes the watcher adds are read on the watcher thread. Watcher
    events re-read the note they name, and `refresh` re-reads one at once.
    A change the watcher does not report is not seen until the next event
    for that file.

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
        self._truncation_logged: set[str] = set()
        self._truncation_lock = threading.Lock()

        self._closed = threading.Event()
        self._warmer: threading.Thread | None = None

        self._observer: BaseObserver | None = Observer()
        self._observer.schedule(FileEventHandler(self), self._path, recursive=True)
        self._observer.start()
        try:
            self._register_tree(self._path)
        except BaseException:
            self.close()
            raise
        self._warmer = threading.Thread(target=self._read_all, name="tv3-read-notes", daemon=True)
        self._warmer.start()

    @staticmethod
    def _dotted(ext: str) -> str:
        return ext if not ext or ext.startswith(".") else "." + ext

    @property
    def path(self) -> str:
        return self._path

    def search(self, query):
        """Return a list of the notes that match the given query.

        A query with any words waits for the background read of the notes to
        finish first: reading them on this thread as well would duplicate
        work and be slower. An empty query needs no contents and does not wait.
        """
        warmer = self._warmer
        if warmer is not None and query.split():
            warmer.join()
        return self.search_function(self, query)

    def find(self, query: str, snippet_width: int = ranking.SNIPPET_WIDTH) -> list[Result]:
        """The notes matching `query`, best first, with what to highlight.

        The notes are those `search` returns plus those whose title fuzzy
        matches the whole query (see `ranking.fuzzy_needle`). An empty query
        gives every note, newest first. Otherwise fuzzy title matches come
        first, best score first, then the other notes newest first. Ties go
        to the newer note.

        A fuzzy match's title spans are the fuzzy-matched characters; other
        notes' title spans are the query words found in the title. Notes
        whose title lacks a query word get a snippet of `snippet_width`
        characters at most (see `ranking.snippet`).
        """
        words = tuple(query.split())
        exact = self.search(query)
        needle = ranking.fuzzy_needle(query)
        fuzzy = []
        if needle is not None:
            for note in self:
                match = ranking.fuzzy_match(needle, note.title)
                if match is not None:
                    fuzzy.append((match[0], note.mtime, note, match[1]))
        fuzzy.sort(key=lambda m: (m[0], m[1]), reverse=True)
        fuzzy_notes = {m[2] for m in fuzzy}
        rest = [note for note in exact if note not in fuzzy_notes]
        rest.sort(key=lambda n: n.mtime, reverse=True)
        return [Result(note, words, snippet_width, spans) for _, _, note, spans in fuzzy] + [
            Result(note, words, snippet_width) for note in rest
        ]

    def refresh(self, note: PlainTextNote) -> bool:
        """Re-read `note`'s file now instead of waiting for the watcher.

        For changes this process knows about, such as the editor exiting.
        Returns True if the notebook has a note at `note.abspath` and its
        mtime or contents changed.
        """
        return self._refresh(note.abspath)

    def _read(self, abspath: str, title: str) -> _Contents:
        """Read the note file at `abspath`, up to MAX_INDEXED_BYTES bytes of it."""
        try:
            with open(abspath, "rb") as fp:
                mtime = os.fstat(fp.fileno()).st_mtime
                data = fp.read(MAX_INDEXED_BYTES + 1)
        except OSError as e:
            logger.error(f"Error reading note {abspath}: {e}")
            mtime, data = 0.0, b""
        if len(data) > MAX_INDEXED_BYTES:
            data = data[:MAX_INDEXED_BYTES]
            self._log_truncated(abspath)
        contents = data.decode("utf-8", errors="replace")
        text = title + "\n" + contents
        folded = title.lower() + "\n" + contents.lower()
        return _Contents(mtime, text, text if folded == text else folded)

    def _log_truncated(self, abspath: str) -> None:
        with self._truncation_lock:
            if abspath in self._truncation_logged:
                return
            self._truncation_logged.add(abspath)
        logger.info(
            f"{abspath} truncated for search: only its first {MAX_INDEXED_BYTES} bytes are searched"
        )

    def _read_relpath(self, relpath: str) -> _Contents:
        return self._read(os.path.join(self._path, relpath), os.path.splitext(relpath)[0])

    def _read_all(self) -> None:
        """Read every note not read yet, until done or the notebook is closed."""
        for note in self:
            if self._closed.is_set():
                return
            note._loaded()

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
        if title.endswith(os.sep) or (os.altsep is not None and title.endswith(os.altsep)):
            raise InvalidNoteTitleError(f"Invalid note title: ends with a path separator: {title}")
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
                on_disk = self._on_disk_relpath(relpath)
                existing = self._add_locked(on_disk, self._read_relpath(on_disk))
            if existing is not None:
                raise NoteAlreadyExistsError(f"Note already exists: {existing.title}", existing)
            self._write_empty_file(abspath, relpath)
            on_disk = self._on_disk_relpath(relpath)
            note = self._add_locked(on_disk, self._read_relpath(on_disk))
        logger.debug(f"Created note {abspath}")
        return note

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

    def _indexable(self, abspath: str, real_parent: str | None = None) -> tuple[str, float] | None:
        """The relative path and mtime of `abspath` if it is a note file to index, else None.

        The one test of what is a note, shared by the scan and the watcher.
        """
        relpath = self._relpath(abspath, real_parent)
        if relpath is None or relpath == os.curdir or self._rejection(relpath) is not None:
            return None
        try:
            st = os.lstat(abspath)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        return relpath, st.st_mtime

    def _add_locked(self, relpath: str, cached: _Stat | _Contents) -> PlainTextNote:
        title, extension = os.path.splitext(relpath)
        note = PlainTextNote(title, self, extension, cached)
        self._notes[self._key(relpath)] = note
        return note

    def _register(self, abspath: str, real_parent: str | None = None, read: bool = False) -> bool:
        """Track the existing note file at `abspath`. Never writes to disk.

        With `read`, reads the file now; otherwise caches only its mtime.
        Returns True if a note was added.
        """
        indexable = self._indexable(abspath, real_parent)
        if indexable is None:
            return False
        relpath, mtime = indexable
        key = self._key(relpath)
        cached: _Stat | _Contents
        if read:
            with self._notes_lock:
                if key in self._notes:
                    return False
            cached = self._read_relpath(relpath)
        else:
            cached = _Stat(mtime)
        with self._notes_lock:
            if key in self._notes:
                return False
            self._add_locked(relpath, cached)
        return True

    def _refresh(self, abspath: str) -> bool:
        """Re-read the note at `abspath`, if tracked. Returns True if it changed."""
        relpath = self._relpath(abspath)
        if relpath is None:
            return False
        with self._notes_lock:
            note = self._notes.get(self._key(relpath))
        return note is not None and note._refresh()

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

    def _register_tree(self, directory: str, read: bool = False) -> bool:
        """Register every note file under `directory`. Returns True if any were added.

        With `read`, reads each file now; otherwise caches only its mtime.
        """
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
                if self._register(os.path.join(root, filename), real_root, read):
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

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def close(self) -> None:
        """Stop the file watcher. Safe to call more than once, from any thread."""
        self._closed.set()
        with self._notes_lock:
            observer, self._observer = self._observer, None
            warmer, self._warmer = self._warmer, None
        if warmer is not None and threading.current_thread() is not warmer:
            warmer.join()
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
        """Track and read the note or notes at `path`, or re-read a tracked note file.

        Returns True if a note was added or a note file changed.
        """
        nb = self._notebook
        if is_directory:
            return nb._register_tree(path, read=True)
        return nb._register(path, read=True) or nb._refresh(path)

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
            return removed or added

        self._handle(event, apply)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = os.fsdecode(event.src_path)
        self._handle(event, lambda: self._added(path, is_directory=False))
