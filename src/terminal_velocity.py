#!/usr/bin/env python3
"""A fast note-taking app for the UNIX terminal"""

import argparse
import configparser
import logging
import logging.handlers
import os
import sys
import urwid_ui


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        '-c',
        '--config',
        action='store',
        default='~/.tvrc',
        dest='config',
        help='the config file to use (default: %(default)s)',
    )
    (args, _) = parser.parse_known_args()
    config_file = os.path.abspath(os.path.expanduser(args.config))
    config = configparser.ConfigParser()
    config.read(config_file)
    defaults = dict(config.items('DEFAULT'))
    description = __doc__
    epilog = (
        'the config file can be used to override the defaults for the\n'
        'optional arguments, example config file contents:\n'
        '\n'
        '    [DEFAULT]\n'
        '    editor = pico\n'
        '    # The filename extension to use for new files.\n'
        '    extension = .txt\n'
        '    # The filename extensions to recognize in the notes dir.\n'
        '    extensions = .txt, .md, .markdown, .rst\n'
        '    notes_dir = ~/Notes\n'
        ''
        'if there is no config file (or an argument is missing from the)\n'
        'config file the default default will be used\n')
    parser = argparse.ArgumentParser(
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[parser],
    )
    parser.add_argument(
        '-e',
        '--editor',
        action='store',
        default=defaults.get('editor', os.getenv('EDITOR', 'pico')),
        dest='editor',
        help='the text editor to use (default: %(default)s)',
    )
    parser.add_argument(
        '-x',
        '--extension',
        action='store',
        default=defaults.get('extension', 'txt'),
        dest='extension',
        help='the filename extension for new notes (default: %(default)s)',
    )
    parser.add_argument(
        '--extensions',
        action='store',
        default=defaults.get('extensions', '.txt, .md, .markdown, .rst'),
        dest='extensions',
        help=('the filename extensions to recognize in the notes dir, a '
              'comma-separated list (default: %(default)s)'),
    )
    parser.add_argument(
        '--exclude',
        action='store',
        default=defaults.get('exclude', 'src, backup, ignore, tmp, old'),
        dest='exclude',
        help=('the file/directory names to skip while recursively searching '
              'the notes dir for notes, a comma-separated list '
              '(default: %(default)s)'),
    )
    parser.add_argument(
        '-d',
        '--debug',
        action='store_true',
        default=defaults.get('debug', False),
        dest='debug',
        help='debug logging on or off (default: off)',
    )
    parser.add_argument(
        '-l',
        '--log-file',
        action='store',
        default=defaults.get('log_file', '~/.tvlog'),
        dest='log_file',
        help='the file to log to (default: %(default)s)',
    )
    parser.add_argument(
        '-p',
        '--print-config',
        action='store_true',
        default=False,
        dest='print_config',
        help='print your configuration settings then exit',
    )
    parser.add_argument(
        'notes_dir',
        action='store',
        default=defaults.get('notes_dir', '~/Notes'),
        help='the notes directory to use (default: %(default)s)',
        nargs='?',
    )
    args = parser.parse_args()
    extensions = []
    for extension in args.extensions.split(','):
        extensions.append(extension.strip())
    args.extensions = extensions
    exclude = []
    for name in args.exclude.split(','):
        exclude.append(name.strip())
    args.exclude = exclude

    # Set up logging before print_config so log file is created
    logger = logging.getLogger("tv3")
    logger.setLevel(logging.DEBUG)
    fh = logging.handlers.RotatingFileHandler(
        os.path.abspath(os.path.expanduser(args.log_file)),
        maxBytes=1000000,  # 1 megabyte.
        backupCount=0)
    if args.debug:
        fh.setLevel(logging.DEBUG)
    else:
        fh.setLevel(logging.WARNING)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.CRITICAL)
    logger.addHandler(sh)
    logger.debug(args)

    if args.print_config:
        print(args)
        sys.exit(0)
    #try:
    urwid_ui.launch(
        notes_dir=args.notes_dir,
        editor=args.editor,
        extension=args.extension,
        extensions=args.extensions,
        exclude=args.exclude)


if __name__ == '__main__':
    main()
