"""Terminal Velocity 3 - Fast note-taking for the command line.

Terminal Velocity is a fast note-taking application for the UNIX terminal.
It combines finding and creating notes in a single minimal interface and
delegates the note-taking itself to your $EDITOR.

Inspired by Notational Velocity (http://notational.net/).
"""

__version__ = "0.1.0"
__author__ = "Aramís Concepción Durán"
__license__ = "GNU General Public License, Version 3"
__url__ = "https://github.com/caelyx/tv3"

__all__ = ["terminal_velocity", "tv_notebook", "urwid_ui"]

# Expose main function for convenience
from .terminal_velocity import main
