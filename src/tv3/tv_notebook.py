"""Persistent note storage and search."""

import errno
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

# os.link errors meaning the filesystem cannot hard-link this file here.
_LINK_UNSUPPORTED = {
    errno.EPERM,
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
    errno.EXDEV,
    errno.EMLINK,
    errno.ENOSYS,
}


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


class DeleteNoteError(Error):
    """Raised when deleting a note fails."""


def _is_editor_temp(name: str) -> bool:
    """True for editor backup, swap and lock file names."""
    return (
        name.endswith(EDITOR_TEMP_SUFFIXES)
        or name.startswith(".#")
        or (len(name) > 1 and name.startswith("#") and name.endswith("#"))
    )


def _is_inside(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory + os.sep)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


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

    `text` is the title, a newline and the contents, all in Unicode
    normalisation form NFC; `folded` is the
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

    `title` is the file's path relative to the notebook, without its
    extension, in Unicode normalisation form NFC; the file's name on disk
    may be in another form. `contents` and `mtime` come from an in-memory
    copy of the file, which the notebook passes in as `cached` or which is
    read on first use. The notebook re-reads it when its watcher reports a
    change.
    """

    def __init__(
        self,
        title: str,
        notebook: "PlainTextNoteBook",
        extension: str,
        cached: _Stat | _Contents | None = None,
    ):
        self._relpath = title + extension
        self._title = _nfc(title)
        self._extension = extension
        self._abspath = os.path.join(notebook.path, self._relpath)
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
        """The first MAX_INDEXED_BYTES bytes of the note file, decoded as UTF-8, in NFC."""
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
        """The cached file, reading it first if it has not been read.

        A file that cannot be read is logged and read as empty.
        """
        cached = self._cached
        if isinstance(cached, _Contents):
            return cached
        contents = self._notebook._read_or_empty(self._abspath, self._title)
        self._store_if_unchanged(cached, contents)
        return contents

    def _refresh(self) -> bool:
        """Re-read the note file. Returns True if its mtime or contents changed.

        If another read is stored while this one runs, reads again, so what
        this stores is never older than what it replaces.

        Raises:
            FileNotFoundError: the file is gone; nothing is stored.
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

    `exact` is whether the note is an exact match: every query word occurs
    in its title or contents, with smart case (see `brute_force_search`).
    Every note is an exact match for a query without words. `prefix` is
    whether the note's title starts with the query, ignoring case and Unicode
    normalisation (`ranking.starts_with`); it is False for every note when
    the query has no words.

    Spans are half-open `(start, end)` character ranges, sorted and
    non-overlapping: `title_spans` index `note.title`, `snippet_spans` index
    `snippet`. `snippet` is a line from the note's contents, or None.

    Exact-match title spans and the snippet are worked out on first access,
    from the note's contents at that time, so a search costs nothing for
    results that are never shown. If the contents changed since `find`, the
    snippet reflects the new contents and is None if the query no longer
    matches them. These properties are not thread-safe: read them from one
    thread, the UI thread.
    """

    note: PlainTextNote
    exact: bool
    prefix: bool
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
    case-sensitively. The query is put in Unicode normalisation form NFC, as
    titles and contents are, so text matches whatever form it was typed or
    stored in. Searches the notes' cached contents; it reads only notes that
    have not been read yet.
    """
    words = [(word, word.islower()) for word in _nfc(query).split()]
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
    after an event that added, removed or modified a note, and from the
    background thread once its read has finished and `loaded` is true.
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
        self._loaded = threading.Event()
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

    @property
    def loaded(self) -> bool:
        """Whether the background read has finished, so a search will not wait for it."""
        return self._loaded.is_set()

    def search(self, query):
        """Return a list of the notes that match the given query.

        A query with any words waits for the background read of the notes to
        finish first: reading them on this thread as well would duplicate
        work and be slower. An empty query needs no contents and does not wait.
        """
        warmer = self._warmer
        if warmer is not None and query.split() and not self.loaded:
            warmer.join()
        return self.search_function(self, query)

    def find(self, query: str, snippet_width: int = ranking.SNIPPET_WIDTH) -> list[Result]:
        """The notes matching `query`, best first, with what to highlight.

        The notes are those `search` returns (exact matches), those whose
        title starts with the query (prefix matches; see `ranking.starts_with`)
        and those whose title fuzzy matches the whole query (see
        `ranking.fuzzy_needle`; it needs at least MIN_FUZZY_LENGTH
        characters). Prefix matching ignores case whatever the query's case:
        smart case applies only to exact matches. The fuzzy match is against
        the title's last path component, so a folder name does not
        fuzzy-match the notes in it, unless the query contains a path
        separator; then it is against the whole title. The query is put in
        Unicode normalisation form NFC, like titles and contents.

        A query without words gives every note, newest first. Otherwise, with
        titles and the query compared by `ranking.fold`:

        1. prefix matches whose title equals the query, newest first;
        2. other prefix matches, newest first;
        3. other fuzzy matches, best score first, newer first on ties;
        4. other exact matches, newest first.

        The results flagged `prefix` are those in groups 1 and 2, so they
        come first.

        A fuzzy match's title spans are the fuzzy-matched characters, whatever
        its group; other notes' title spans are the query words found in the
        title, with smart case. For exact matches in groups 1 and 2 both cover
        the matched prefix (spaces aside), except that query words with a
        capital letter are not found in a title that has them only in another
        case. Notes
        whose title lacks a query word get a snippet of `snippet_width`
        columns at most (see `ranking.snippet`).
        """
        query = _nfc(query)
        words = tuple(query.split())
        exact = self.search(query)
        if not words:
            exact.sort(key=lambda n: n.mtime, reverse=True)
            return [Result(note, True, False, words, snippet_width) for note in exact]
        needle = ranking.fuzzy_needle(query)
        fuzzy: dict[PlainTextNote, tuple[float, tuple[ranking.Span, ...]]] = {}
        if needle is not None:
            for note in self:
                match = ranking.fuzzy_match(needle, note.title)
                if match is not None:
                    fuzzy[note] = match
        folded = ranking.fold(query)
        equal: list[PlainTextNote] = []
        prefix: list[PlainTextNote] = []
        for note in self:
            if ranking.starts_with(note.title, query):
                (equal if ranking.fold(note.title) == folded else prefix).append(note)
        prefixed = {*equal, *prefix}
        rest = [note for note in exact if note not in prefixed and note not in fuzzy]
        fuzzy_rest = [
            (score, note.mtime, note) for note, (score, _) in fuzzy.items() if note not in prefixed
        ]
        fuzzy_rest.sort(key=lambda m: (m[0], m[1]), reverse=True)
        newest_first = sorted(equal, key=lambda n: n.mtime, reverse=True)
        newest_first += sorted(prefix, key=lambda n: n.mtime, reverse=True)
        ordered = newest_first + [note for _, _, note in fuzzy_rest]
        ordered += sorted(rest, key=lambda n: n.mtime, reverse=True)
        exact_set = set(exact)
        return [
            Result(
                note,
                note in exact_set,
                note in prefixed,
                words,
                snippet_width,
                fuzzy[note][1] if note in fuzzy else None,
            )
            for note in ordered
        ]

    def refresh(self, note: PlainTextNote) -> bool:
        """Re-read `note`'s file now instead of waiting for the watcher.

        For changes this process knows about, such as the editor exiting.
        Returns True if the notebook has a note at `note.abspath` and its
        mtime or contents changed.
        """
        return self._refresh(note.abspath)

    def _read(self, abspath: str, title: str) -> _Contents:
        """Read the note file at `abspath`, up to MAX_INDEXED_BYTES bytes of it.

        Any error but the file being gone is logged, and the file read as empty.

        Raises:
            FileNotFoundError: there is no file at `abspath`.
        """
        try:
            with open(abspath, "rb") as fp:
                mtime = os.fstat(fp.fileno()).st_mtime
                data = fp.read(MAX_INDEXED_BYTES + 1)
        except FileNotFoundError:
            raise
        except OSError as e:
            logger.error(f"Error reading note {abspath}: {e}")
            mtime, data = 0.0, b""
        return self._contents(abspath, title, mtime, data)

    def _read_or_empty(self, abspath: str, title: str) -> _Contents:
        """`_read`, but a missing file is logged as an error and read as empty.

        For reads of a file this process expects to exist, such as one it has
        just created; the watcher reports a file that is gone.
        """
        try:
            return self._read(abspath, title)
        except FileNotFoundError as e:
            logger.error(f"Error reading note {abspath}: {e}")
            return self._contents(abspath, title, 0.0, b"")

    def _contents(self, abspath: str, title: str, mtime: float, data: bytes) -> _Contents:
        """The cached form of a note file at `abspath` that holds `data`."""
        if len(data) > MAX_INDEXED_BYTES:
            data = data[:MAX_INDEXED_BYTES]
            self._log_truncated(abspath)
        contents = _nfc(data.decode("utf-8", errors="replace"))
        title = _nfc(title)
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
        return self._read_or_empty(os.path.join(self._path, relpath), os.path.splitext(relpath)[0])

    def _read_all(self) -> None:
        """Read every note not read yet, until done or the notebook is closed."""
        for note in self:
            if self._closed.is_set():
                return
            note._loaded()
        self._loaded.set()
        self._notify()

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
        relpath, abspath = self._new_note_path(title, self.extension)
        with self._notes_lock:
            self._check_free_locked(relpath, abspath)
            self._in_new_parents(
                abspath, lambda: open(abspath, "x").close(), f"Could not create {relpath}"
            )
            on_disk = self._on_disk_relpath(relpath)
            note = self._add_locked(on_disk, self._read_relpath(on_disk))
        logger.debug(f"Created note {abspath}")
        return note

    def rename(self, note: PlainTextNote, new_title: str) -> PlainTextNote:
        """Rename `note`'s file to `new_title` and return the renamed note.

        `new_title` is checked as `create` checks a title, but keeps
        `note`'s extension unless it ends in a recognised extension. Missing
        directories are created. A rename that changes only the case of the
        file name is allowed on a case-insensitive filesystem; changing only
        the case of a folder name is not. A rename to the same title returns
        `note` unchanged. `note` itself keeps its old path.

        An existing file is never replaced: the file is hard-linked to its
        new name, which fails if that name exists, and then unlinked from
        its old one. Where hard links are not supported, the target is
        checked and the file then renamed, so a file that another process
        creates at the target in between is replaced. A rename that changes
        only case or Unicode normalisation renames the file in place.

        Raises:
            InvalidNoteTitleError: the title cannot be used; nothing changed.
            NoteAlreadyExistsError: another note file exists at that path;
                nothing changed.
            NewNoteError: `note` is not the notebook's current note for its
                file, or the file could not be renamed; nothing changed.
        """
        relpath, abspath = self._new_note_path(new_title, note.extension)
        old_relpath = note._relpath
        failure = f"Could not rename {old_relpath} to {relpath}"
        with self._notes_lock:
            old_key = self._key(old_relpath)
            if self._notes.get(old_key) is not note:
                raise NewNoteError(f"'{note.title}' changed on disk; try again")
            if _nfc(relpath) == _nfc(old_relpath):
                return note
            if self._key(relpath) == old_key:
                if _nfc(os.path.dirname(relpath)) != _nfc(os.path.dirname(old_relpath)):
                    raise InvalidNoteTitleError(
                        "Invalid note title: can't change only the letter case of a folder"
                        f" name: {relpath}"
                    )
                self._in_new_parents(abspath, lambda: os.rename(note.abspath, abspath), failure)
            else:
                self._check_free_locked(relpath, abspath)
                self._in_new_parents(
                    abspath, lambda: self._move_locked(note.abspath, relpath, abspath), failure
                )
            del self._notes[old_key]
            on_disk = self._on_disk_relpath(relpath)
            renamed = self._add_locked(on_disk, self._read_relpath(on_disk))
        logger.debug(f"Renamed note {note.abspath} to {abspath}")
        return renamed

    def delete(self, note: PlainTextNote) -> None:
        """Delete `note`'s file and remove it from the notebook.

        A file that is already gone is not an error.

        Raises:
            DeleteNoteError: `note` is not the notebook's current note for its
                file, or the file could not be deleted; nothing changed.
        """
        relpath = note._relpath
        with self._notes_lock:
            current = self._notes.get(self._key(relpath))
            if current is None and not os.path.lexists(note.abspath):
                logger.debug(f"Note to delete was already gone: {note.abspath}")
                return
            if current is not note:
                raise DeleteNoteError(f"'{note.title}' changed on disk; try again")
            try:
                os.remove(note.abspath)
            except FileNotFoundError:
                logger.debug(f"Note to delete was already gone: {note.abspath}")
            except OSError as e:
                raise DeleteNoteError(f"Could not delete {relpath}: {e.strerror or e}") from e
            self._notes.pop(self._key(relpath), None)
        logger.debug(f"Deleted note {note.abspath}")

    def _new_note_path(self, title: str, extension: str) -> tuple[str, str]:
        """The relative and absolute path for a note titled `title`.

        `extension` is appended unless `title` ends in a recognised one.
        Raises InvalidNoteTitleError if the title cannot be used.
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
        typed_relpath = title if self._has_extension(title) else title + extension
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
        return relpath, abspath

    def _check_free_locked(self, relpath: str, abspath: str) -> None:
        """Raise unless no note exists at `relpath`. Call with the lock held.

        An indexed note whose file is gone is dropped. A note file on disk
        that is not indexed yet is registered, then reported.

        Raises:
            NoteAlreadyExistsError: a note file exists at `relpath`.
            InvalidNoteTitleError: something other than a note file is there.
        """
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

    def _move_locked(self, src: str, relpath: str, abspath: str) -> None:
        """Move the file `src` to `abspath` unless something exists there.

        Call with the lock held and `abspath`'s directory existing.

        Raises:
            NoteAlreadyExistsError: a note file exists at `relpath`.
            InvalidNoteTitleError: something other than a note file is there.
            OSError: the file could not be moved.
        """
        try:
            os.link(src, abspath, follow_symlinks=False)
        except FileExistsError:
            self._check_free_locked(relpath, abspath)
            raise
        except OSError as e:
            if e.errno not in _LINK_UNSUPPORTED:
                raise
            logger.debug(f"Cannot hard-link {src} ({e}); renaming it instead")
            self._check_free_locked(relpath, abspath)
            os.rename(src, abspath)
            return
        try:
            os.unlink(src)
        except OSError:
            try:
                os.unlink(abspath)
            except OSError as unlink_error:
                logger.error(
                    f"Could not remove {abspath} after failing to move {src}: {unlink_error}"
                )
            raise

    def _in_new_parents(self, abspath: str, make: Callable[[], None], failure: str) -> None:
        """Create any missing parent directories of `abspath`, then call `make`.

        If `make` raises, removes the directories it created. An OSError
        becomes NewNoteError: `failure`, a colon and the OS error's
        description; anything else propagates.
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
            make()
        except BaseException as e:
            for directory in reversed(created):
                try:
                    os.rmdir(directory)
                except OSError as rmdir_error:
                    logger.warning(f"Could not remove {directory}: {rmdir_error}")
            if isinstance(e, OSError):
                raise NewNoteError(f"{failure}: {e.strerror or e}") from e
            raise

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
        Returns True if a note was added. A file that is gone by the time it
        is read is not added: a later event reports where it went.
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
            try:
                cached = self._read(os.path.join(self._path, relpath), os.path.splitext(relpath)[0])
            except FileNotFoundError:
                logger.debug(f"{abspath} was gone before it could be read")
                return False
        else:
            cached = _Stat(mtime)
        with self._notes_lock:
            if key in self._notes:
                return False
            self._add_locked(relpath, cached)
        return True

    def _refresh(self, abspath: str) -> bool:
        """Re-read the note at `abspath`, if tracked. Returns True if it changed.

        A note whose file is gone is left as it was: the file was deleted or
        moved, and the watcher's event for that removes or moves the note.
        Dropping it here would lose a note whose file exists elsewhere.
        """
        note = self.note_at(abspath)
        if note is None:
            return False
        try:
            return note._refresh()
        except FileNotFoundError:
            logger.debug(f"{abspath} was gone when re-read; waiting for its event")
            return False

    def _same_key(self, src: str, dest: str) -> bool:
        """Whether `src` and `dest` differ only in case or Unicode normalisation.

        On a case-insensitive filesystem such paths name the same file.
        """
        src_relpath = self._relpath(src)
        dest_relpath = self._relpath(dest)
        if src_relpath is None or dest_relpath is None:
            return False
        return self._key(src_relpath) == self._key(dest_relpath)

    def _tracked_as(self, src: str, dest: str) -> bool:
        """Whether `src` and `dest` name the same note, already tracked as `dest`.

        True after `rename` changed only the case or Unicode normalisation of
        a title: on a case-insensitive filesystem both names have one key.
        """
        dest_relpath = self._relpath(dest)
        if dest_relpath is None or not self._same_key(src, dest):
            return False
        with self._notes_lock:
            note = self._notes.get(self._key(dest_relpath))
        return note is not None and note._relpath == dest_relpath

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
        return isinstance(note, PlainTextNote) and self.note_at(note.abspath) == note

    def note_at(self, abspath: str) -> PlainTextNote | None:
        """The note for the file at `abspath`, or None if the notebook has none.

        On a case-insensitive filesystem, `abspath` may differ from the
        note's path in case.
        """
        relpath = self._relpath(abspath)
        if relpath is None:
            return None
        with self._notes_lock:
            return self._notes.get(self._key(relpath))


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
            nb = self._notebook
            if not event.is_directory and nb._tracked_as(src, dest):
                return nb._refresh(dest)
            if nb._same_key(src, dest):
                # One file under two spellings: the source's entry is the
                # destination's, so it goes before the destination is added.
                removed = self._removed(src, event.is_directory)
                added = self._added(dest, event.is_directory)
                return removed or added
            # Events arrive after the fact, and the file may have moved again
            # since: a source that exists again stays. The destination is
            # added before the source is dropped, and if neither is then
            # tracked although one is a note file, the file moved between
            # the checks and both are checked again.
            added = self._added(dest, event.is_directory)
            removed = self._removed(src, event.is_directory, only_if_missing=True)
            while (
                removed
                and not event.is_directory
                and nb.note_at(src) is None
                and nb.note_at(dest) is None
                and (nb._indexable(src) is not None or nb._indexable(dest) is not None)
            ):
                added = self._added(dest, False) or self._added(src, False) or added
            return removed or added

        self._handle(event, apply)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = os.fsdecode(event.src_path)
        self._handle(event, lambda: self._added(path, is_directory=False))
