"""Persistent note storage and search."""

import contextlib
import logging
import os
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger("tv3")

# Constants
WATCHDOG_STOP_TIMEOUT = 5  # seconds


class Error(Exception):
    """Base class for exceptions in this module."""

    pass


class NewNoteBookError(Error):
    """Raised when initialising a new NoteBook fails."""

    def __init__(self, value):
        self.value = value
        super().__init__(value)

    def __str__(self):
        return repr(self.value)


class NewNoteError(Error):
    """Raised when making a new Note or adding it to a Notebook fails."""

    def __init__(self, value):
        self.value = value
        super().__init__(value)

    def __str__(self):
        return repr(self.value)


class NoteAlreadyExistsError(NewNoteError):
    """Raised when trying to add a new note that already exists."""

    pass


class InvalidNoteTitleError(NewNoteError):
    """Raised when trying to add a new note with an invalid title."""

    pass


class DelNoteError(Error):
    """Raised when removing a Note from a NoteBook fails."""

    def __init__(self, value):
        self.value = value
        super().__init__(value)

    def __str__(self):
        return repr(self.value)


class PlainTextNote:
    """A note, stored as a plain text file on disk."""

    def __init__(self, title, notebook, extension):
        """Initialise a new PlainTextNote."""
        self._title = title
        self._notebook = notebook
        self._extension = extension
        self._filename = self.title + self._extension
        self._abspath = os.path.join(self._notebook.path, self._filename)
        directory = os.path.split(self.abspath)[0]
        if not os.path.isdir(directory):
            logger.debug(f"'{directory}' doesn't exist, creating it")
            try:
                os.makedirs(directory)
            except OSError as e:
                msg = f"{directory} could not be created: {e}"
                raise NewNoteError(msg) from e

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, new_title):
        raise NotImplementedError

    @property
    def extension(self):
        return self._extension

    @property
    def contents(self):
        """Read and return the contents of the note file."""
        try:
            with open(self.abspath, "rb") as fp:
                contents = fp.read()
            if contents is None:
                logger.error(f"Could not decode file contents: {self.abspath}")
                return ""
            return contents.decode("utf-8", errors="ignore")
        except OSError as e:
            logger.error(f"Error reading note {self.abspath}: {e}")
            return ""

    @property
    def mtime(self):
        """Get the modification time of the note file."""
        try:
            return os.path.getmtime(self.abspath)
        except OSError as e:
            logger.error(f"Error getting mtime for {self.abspath}: {e}")
            return 0.0

    @property
    def abspath(self):
        return self._abspath

    def __eq__(self, other):
        return getattr(other, "abspath", None) == self.abspath


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
    """A NoteBook that stores its notes as a directory of plain text files."""

    def __init__(
        self,
        path,
        extension,
        extensions,
        search_function=brute_force_search,
        exclude=None,
    ):
        """Make a new PlainTextNoteBook for the given path."""
        self._path = os.path.abspath(os.path.expanduser(path))
        if extension and not extension.startswith("."):
            extension = "." + extension
        self.extension = extension
        self.search_function = search_function
        self.exclude = exclude or []
        self.extensions = []
        for ext in extensions:
            if not ext.startswith("."):
                ext = "." + ext
            self.extensions.append(ext)
        if not os.path.isdir(self.path):
            logger.debug(f"{self.path} doesn't exist, creating it")
            try:
                os.makedirs(self.path)
            except OSError as e:
                msg = f"{self.path} could not be created: {e}"
                raise NewNoteBookError(msg) from e
        self._notes = []
        self._notes_lock = threading.Lock()
        for root, dirs, files in os.walk(self.path):
            for name in self.exclude:
                if name in dirs:
                    dirs.remove(name)
            for filename in files:
                # Skip files that cause errors during initialization
                with contextlib.suppress(NoteAlreadyExistsError, InvalidNoteTitleError):
                    self.add_new(filename, root=root)
        # Activate watchdog
        self._observer = Observer()
        self._fileEventHandler = FileEventHandler(self)
        self._observer.schedule(self._fileEventHandler, self.path, recursive=True)
        self._observer.start()

    @property
    def path(self):
        return self._path

    def search(self, query):
        """Return a sequence of Notes that match the given query."""
        return self.search_function(self, query)

    def add_new(self, filename, root=None):
        """Add a new note to the notebook.

        Args:
            filename: Name of the file to add (with extension).
            root: Optional root directory. Defaults to notebook path.

        Returns:
            PlainTextNote object or None if file should be skipped.

        Raises:
            NoteAlreadyExistsError: If note with same title already exists.
            InvalidNoteTitleError: If note title is invalid.
        """
        if filename in self.exclude:
            return None
        if filename.endswith("~"):
            return None

        # Check if file already exists (scanning) or we're creating it (user-initiated)
        if root is None:
            root = self._path
        abspath = os.path.join(root, filename)
        file_exists = os.path.exists(abspath)

        # For existing files (directory scanning), silently ignore dotfiles
        # For new files (user-initiated), validate and raise errors
        if filename.startswith("."):
            if file_exists:
                return None  # Silently ignore dotfiles during scanning
            else:
                raise InvalidNoteTitleError(f"Invalid note title: {filename}")

        # Always raise error for filenames starting with /
        if filename.startswith(os.sep):
            raise InvalidNoteTitleError(f"Invalid note title: {filename}")

        # Check extension
        if os.path.splitext(filename)[1] not in self.extensions:
            return None

        logger.debug(f"Creating filename: {filename}")

        # Security: Validate path doesn't escape notebook directory
        real_abspath = os.path.realpath(abspath)
        real_notebook_path = os.path.realpath(self.path)
        # Ensure we're checking directory boundaries, not just string prefixes
        # e.g., /notes2 should not match /notes
        try:
            os.path.commonpath([real_abspath, real_notebook_path])
            # Check if abspath is actually under notebook path
            rel_path = os.path.relpath(real_abspath, real_notebook_path)
            if rel_path.startswith(".."):
                msg = f"Note path {abspath} is outside notebook directory"
                raise InvalidNoteTitleError(msg)
        except ValueError as e:
            # Different drives on Windows
            msg = f"Note path {abspath} is outside notebook directory"
            raise InvalidNoteTitleError(msg) from e

        # Create file if it doesn't exist (with parent directories)
        if not file_exists:
            parent_dir = os.path.dirname(abspath)
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)
            with open(abspath, "a") as fp:
                fp.write("")

        title = os.path.relpath(abspath, self.path)
        title, extension = os.path.splitext(title)
        if title is None:
            logger.error(f"Could not decode filename: {title}")
            return None

        if extension is None:
            extension = self.extension
        if title.startswith(os.sep):
            title = title[len(os.sep) :]
        title = title.strip()
        if not os.path.split(title)[1]:
            msg = f"Invalid note title: {title}"
            raise InvalidNoteTitleError(msg)

        # Add to notebook with thread safety
        with self._notes_lock:
            for note in self._notes:
                if note.title == title and note.extension == extension:
                    msg = f"Note already in NoteBook: {note.title}"
                    raise NoteAlreadyExistsError(msg)
            note = PlainTextNote(title, self, extension)
            self._notes.append(note)
        return note

    def remove(self, filename, root=None):
        """Remove a note from the notebook.

        Args:
            filename: Name of the file to remove (with extension).
            root: Optional root directory. Defaults to notebook path.
        """
        logger.debug(f"Removing {filename}")
        if root is None:
            root = self._path
        abspath = os.path.join(root, filename)
        title = os.path.relpath(abspath, self.path)
        title, _ = os.path.splitext(title)
        if title is None:
            logger.error(f"Could not decode filename: {title}")
            return
        with self._notes_lock:
            for i in range(len(self._notes)):
                n = self._notes[i]
                if n.title == title:
                    logger.debug(f"Found note with index {i} and title {title}")
                    logger.debug(f"Current length is {len(self._notes)}")
                    self._notes = self._notes[:i] + self._notes[i + 1 :]
                    logger.debug(f"New length is {len(self._notes)}")
                    return

    def close(self):
        """Clean up resources, stopping the file watcher."""
        if hasattr(self, "_observer") and self._observer:
            logger.debug("Stopping file observer")
            self._observer.stop()
            self._observer.join(timeout=WATCHDOG_STOP_TIMEOUT)
            self._observer = None

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - cleanup resources."""
        self.close()
        return False

    def __len__(self):
        with self._notes_lock:
            return len(self._notes)

    def __getitem__(self, index):
        with self._notes_lock:
            return self._notes[index]

    def __delitem__(self, index):
        raise NotImplementedError

    def __iter__(self):
        with self._notes_lock:
            return iter(list(self._notes))

    def __reversed__(self):
        with self._notes_lock:
            return reversed(list(self._notes))

    def __contains__(self, note):
        with self._notes_lock:
            return note in self._notes


class FileEventHandler(FileSystemEventHandler):
    """Handle file system events for automatic note syncing."""

    def __init__(self, notebook):
        self._notebook = notebook
        super().__init__()

    def on_created(self, e):
        """Handle file creation events."""
        if not e.is_directory:
            logger.debug(f"Detected new file {e.src_path}")
            try:
                directory = os.path.dirname(e.src_path)
                filename = os.path.basename(e.src_path)
                self._notebook.add_new(filename, root=directory)
            except (NoteAlreadyExistsError, InvalidNoteTitleError) as ex:
                logger.debug(f"Skipping file event: {ex}")
        return super().on_created(e)

    def on_deleted(self, e):
        """Handle file deletion events."""
        if not e.is_directory:
            logger.debug(f"Detected deleted file {e.src_path}")
            try:
                directory = os.path.dirname(e.src_path)
                filename = os.path.basename(e.src_path)
                self._notebook.remove(filename, root=directory)
            except Exception as ex:
                logger.debug(f"Error handling delete event: {ex}")
        return super().on_deleted(e)
