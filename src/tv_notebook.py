"""Persistent note storage and search."""

import logging
logger = logging.getLogger("tv3")
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import os
import threading


class Error(Exception):
    """Base class for exceptions in this module."""
    pass


class NewNoteBookError(Error):
    """Raised when initialising a new NoteBook fails."""

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)


class NewNoteError(Error):
    """Raised when making a new Note or adding it to a Notebook fails."""

    def __init__(self, value):
        self.value = value

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

    def __str__(self):
        return repr(self.value)


class PlainTextNote(object):
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
            message = '\'{} doesn\'t exist, creating it'
            logger.debug(message.format(directory))
            try:
                os.makedirs(directory)
            except os.error as e:
                message = '{} could not be created: {}'
                raise NewNoteError(message.format(directory, e))

    @property
    def title(self):
        return self._title

    @title.setter
    def set_title(self, new_title):
        raise NotImplementedError

    @property
    def extension(self):
        return self._extension

    @property
    def contents(self):
        with open(self.abspath, 'rb') as fp:
            contents = fp.read()
        if contents is None:
            message = 'Could not decode file contents: {}'
            logger.error(message.format(self.abspath))
            return ''
        else:
            return contents.decode('utf-8', errors='ignore')

    @property
    def mtime(self):
        return os.path.getmtime(self.abspath)

    @property
    def abspath(self):
        return self._abspath

    def __eq__(self, other):
        return getattr(other, 'abspath', None) == self.abspath


def brute_force_search(notebook, query):
    """Return all notes in `notebook` that match `query`."""
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


class PlainTextNoteBook(object):
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
        if extension and not extension.startswith('.'):
            extension = '.' + extension
        self.extension = extension
        self.search_function = search_function
        self.exclude = exclude
        if not self.exclude:
            self.exclude = []
        self.extensions = []
        for extension in extensions:
            if not extension.startswith('.'):
                extension = '.' + extension
            self.extensions.append(extension)
        if not os.path.isdir(self.path):
            message = '{} doesn\'t exist, creating it'
            logger.debug(message.format(self.path))
            try:
                os.makedirs(self.path)
            except os.error as e:
                message = '{} could not be created: {}'
                raise NewNoteBookError(message.format(self.path, e))
        self._notes = []
        self._notes_lock = threading.Lock()
        for root, dirs, files in os.walk(self.path):
            for name in self.exclude:
                if name in dirs:
                    dirs.remove(name)
            for filename in files:
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
        if filename in self.exclude:
            return None
        if filename.startswith('.') or filename.endswith('~'):
            return None
        if os.path.splitext(filename)[1] not in self.extensions:
            return None
        if root is None:
            root = self._path
        logger.debug("Creating filename: {}".format(filename))
        abspath = os.path.join(root, filename)
        with open(abspath, 'a') as fp:
            fp.write("")
        title = os.path.relpath(abspath, self.path)
        title, extension = os.path.splitext(title)
        if title is None:
            message = 'Could not decode filename: {}'
            logger.error(message.format(title))
            return None

        """Create a new Note and add it to this NoteBook."""
        if extension is None:
            extension = self.extension
        if title.startswith(os.sep):
            title = title[len(os.sep):]
        title = title.strip()
        if not os.path.split(title)[1]:
            message = 'Invalid note title: {}'
            raise InvalidNoteTitleError(message.format(title))
        with self._notes_lock:
            for note in self._notes:
                if note.title == title and note.extension == extension:
                    message = 'Note already in NoteBook: {}'
                    raise NoteAlreadyExistsError(message.format(note.title))
            note = PlainTextNote(title, self, extension)
            self._notes.append(note)
        return note

    def remove(self, filename, root=None):
        logger.debug("Removing {}".format(filename))
        if root is None:
            root = self._path
        abspath = os.path.join(root, filename)
        title = os.path.relpath(abspath, self.path)
        title, _ = os.path.splitext(title)
        if title is None:
            message = 'Could not decode filename: {}'
            logger.error(message.format(title))
            return
        with self._notes_lock:
            for i in range(len(self._notes)):
                n = self._notes[i]
                if n.title == title:
                    logger.debug("Found note with index {} and title {}".format(i, title))
                    logger.debug("Current length is {}".format(len(self._notes)))
                    self._notes = self._notes[:i] + self._notes[i+1:]
                    logger.debug("New length is {}".format(len(self._notes)))
                    return

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
            return (note in self._notes)

class FileEventHandler(FileSystemEventHandler):
    def __init__(self, notebook):
        self._notebook = notebook
    def on_created(self, e):
        if not e.is_directory:
            logger.debug("Detected new file {}".format(e.src_path))
            try:
                directory = os.path.dirname(e.src_path)
                filename = os.path.basename(e.src_path)
                self._notebook.add_new(filename, root=directory)
            except NoteAlreadyExistsError:
                return super().on_created(e)
        return super().on_created(e)
    def on_deleted(self, e):
        if not e.is_directory:
            logger.debug("Detected deleted file {}".format(e.src_path))
            directory = os.path.dirname(e.src_path)
            filename = os.path.basename(e.src_path)
            self._notebook.remove(filename, root=directory)
        return super().on_deleted(e)
