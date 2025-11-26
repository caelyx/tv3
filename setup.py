#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import setuptools

with open('README.md') as f_readme:
    readme_text = f_readme.read()

setuptools.setup(
    author='Aramís Concepción Durán',
    description='A text-based note-taking application',
    entry_points='[console_scripts]\ntv3=terminal_velocity:main\n',
    install_requires=['urwid==2.1.2', 'watchdog'],
    extras_require={
        'test': [
            'pytest>=7.0.0',
            'pytest-cov>=4.0.0',
            'pytest-mock>=3.10.0',
        ],
    },
    license='GNU General Public License, Version 3',
    long_description=readme_text,
    name='tv3',
    package_dir={'': 'src'},
    py_modules=['tv_notebook', 'terminal_velocity', 'urwid_ui'],
    url='github.com/caelyx/tv3',
    version='0.1',
)
