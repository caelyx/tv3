# TODO: Terminal Velocity 3 Improvements

This document tracks improvements identified in the comprehensive codebase review.

## Status Legend
- [ ] Not started
- [x] Completed
- [~] In progress

---

## CRITICAL Priority (Do First)

### 2. Add Path Traversal Protection 🔒
**Location:** `src/tv3/tv_notebook.py` (`_new_note_path`, `_rejection`)
**Issue:** No validation to prevent creating notes outside the notes directory using `../` in titles.
**Security Risk:** HIGH - files could be created anywhere the user has write permissions.

**Action Items:**
- [x] Add path normalization and validation (`add_new()` was replaced by `create()`; `_new_note_path` rejects absolute paths and `..` components)
- [x] Ensure `os.path.realpath()` checks that paths stay within notes directory (a title through a symlink out of the notebook is rejected)
- [x] Add test cases for path traversal attempts
- [ ] Document security considerations

**Implementation:**
```python
# Validate title doesn't escape notes directory
abspath = os.path.realpath(os.path.join(self.path, title + extension))
if not abspath.startswith(os.path.realpath(self.path)):
    raise InvalidNoteTitleError("Note title must be within notes directory")
```

---

### 3. Fix Watchdog Observer Cleanup 🧹
**Location:** `src/tv3/tv_notebook.py` (`PlainTextNoteBook.close`)
**Issue:** Observer thread is never stopped or cleaned up.
**Impact:** Thread leaks; prevents proper shutdown.

**Action Items:**
- [x] Add `close()` method to `PlainTextNoteBook` class
- [x] Call `observer.stop()` and `observer.join()` in cleanup
- [x] Add context manager support (`__enter__`/`__exit__`)
- [ ] Update documentation about proper notebook lifecycle
- [x] Add tests for cleanup behavior

**Implementation:**
```python
def close(self):
    """Clean up resources."""
    if self._observer:
        self._observer.stop()
        self._observer.join(timeout=5)


def __enter__(self):
    return self


def __exit__(self, exc_type, exc_val, exc_tb):
    self.close()
    return False
```

---

### 4. Re-enable Error Handling in main()
**Location:** `src/tv3/terminal_velocity.py` (`main`)
**Issue:** The try/except block is commented out, so errors crash the app ungracefully.
**Impact:** Poor user experience; no error recovery.

**Action Items:**
- [x] Uncomment and fix the try/except block
- [x] Add specific exception handlers for common errors
- [x] Display user-friendly error messages
- [x] Log exceptions properly
- [x] Add graceful exit with cleanup (`launch` closes the frame and its notebook in a `finally`)
- [x] Test error scenarios

**Implementation:**
```python
try:
    urwid_ui.launch(...)
except KeyboardInterrupt:
    logger.info("Interrupted by user")
    sys.exit(0)
except Exception as e:
    logger.exception("Fatal error: %s", e)
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)
```

---

### 5. Pin All Dependencies Consistently 📌
**Files:** `pyproject.toml`, `uv.lock`
**Issue:** Inconsistent version pinning; `watchdog` not pinned at all.
**Impact:** Unpredictable behavior across environments.

**Action Items:**
- [x] Pin `watchdog` version in pyproject.toml (`watchdog>=6,<7`)
- [x] Declare dependencies only in pyproject.toml
- [x] ~~Document why urwid is pinned to 2.1.2~~ (pin removed: urwid 2.1.2 cannot start on Python 3.14; now `urwid>=4.2.5,<5`)
- [x] Test with specified versions (CI runs `uv sync --locked` before the tests)
- [x] Lock dependencies with uv (`uv.lock`)

---

### 6. Update GitHub Actions to v4+
**Locations:** `.github/workflows/pythonapp.yml` and `codeql-analysis.yml`
**Issue:** Using deprecated GitHub Actions v1/v2.
**Impact:** May break in future; security vulnerabilities.

**Action Items:**
- [x] Update `actions/checkout@v1` → `actions/checkout@v4` (now `@v7`)
- [x] ~~Update `actions/setup-python@v1` → `actions/setup-python@v5`~~ (superseded: CI uses `astral-sh/setup-uv`)
- [x] Update `github/codeql-action/*@v1` → `github/codeql-action/*@v3` (now `@v4`)
- [ ] Test workflows locally with `act` if possible
- [ ] Verify workflows run successfully in GitHub

---

## HIGH Priority (Do Soon)

### 7. Add Type Hints 📝
**Analysis:** `tv_notebook.py` and `ranking.py` are mostly annotated; `terminal_velocity.py` and `urwid_ui.py` have no annotations.
**Issue:** Static type checking covers only part of the code; harder to maintain and refactor.

**Action Items:**
- [ ] Add type hints to `tv_notebook.py` module
  - [x] `PlainTextNote` class
  - [ ] `PlainTextNoteBook` class
  - [ ] `brute_force_search()` function
  - [x] `FileEventHandler` class
- [ ] Add type hints to `terminal_velocity.py` module
  - [ ] `main()` function
- [ ] Add type hints to `urwid_ui.py` module
  - [ ] All widget classes
  - [ ] `launch()` function
  - [x] ~~`system()` function~~ (no longer exists; the editor runs from `MainFrame.edit`)
- [x] ~~Add `from typing import` imports (List, Dict, Optional, Union)~~ (superseded: annotations use built-in generics and `X | None`)
- [ ] Add `py.typed` marker file
- [x] Configure mypy in `pyproject.toml` (`[tool.mypy]`)
- [ ] Add mypy to pre-commit hooks
- [x] Run mypy and fix all errors (`uv run mypy src` passes; CI runs it)

**Resources:**
- Python typing documentation: https://docs.python.org/3/library/typing.html
- mypy documentation: https://mypy.readthedocs.io/

---

### 8. Modernize Python Code
**Issue:** Old Python 2 patterns still present.

**Action Items:**

#### a) Remove explicit `object` inheritance (Python 2 style)
- [x] Remove `(object)` from `PlainTextNote` class
- [x] Remove `(object)` from `PlainTextNoteBook` class
- [x] Verify all classes use implicit new-style classes

#### b) Fix property setter syntax
- [x] ~~Fix `@title.setter` method name in `PlainTextNote`~~ (no longer applies: `title` is read-only; `rename()` returns a new note)
- [x] ~~Should be `def title(self, ...)` not `def set_title(self, ...)`~~ (no setter)
- [x] ~~Test property setter behavior~~ (no setter)

#### c) Use modern string formatting
- [x] Replace all `%` formatting with f-strings
- [x] Replace all `.format()` calls with f-strings (src has none; `tests/conftest.py` fills a script template with `.format()`)
- [x] Update tests to use f-strings
- [x] Verify no formatting regressions

---

### 9. Add Tests to CI Workflow
**Location:** `.github/workflows/pythonapp.yml`
**Issue:** Workflow only runs linting, not tests!

**Action Items:**
- [x] Add pytest execution step to workflow
- [x] Add coverage reporting (xml and term-missing)
- [x] Upload coverage to Codecov or similar
- [ ] Add test results reporting
- [x] Set coverage threshold (e.g., 80%)
- [x] Fail build if tests fail or coverage drops

**Implementation:**
```yaml
- name: Run tests with coverage
  run: |
    uv sync --locked
    uv run pytest --cov=src/tv3 --cov-report=xml --cov-report=term-missing --cov-fail-under=80
- name: Upload coverage
  uses: codecov/codecov-action@v7
  with:
    files: ./coverage.xml
```

---

### 10. Fix Documentation Inconsistencies

**Action Items:**

#### a) Fix README.md vs pyproject.toml contradiction
- [x] ~~Update README.md line 34 (says urwid 1.1.1, should be 2.1.2)~~ (no longer applies: README no longer mentions urwid versions)
- [x] ~~Document why urwid is pinned to 2.1.2~~ (pin removed: urwid 2.1.2 cannot start on Python 3.14; now `urwid>=4.2.5,<5`)

#### b) Fix CLAUDE.md incorrect default
- [x] Update CLAUDE.md line 40 about default editor
- [x] Should mention `$EDITOR` environment variable first, then `pico`

#### c) General documentation cleanup
- [ ] Ensure all code examples are correct
- [ ] Update version numbers where applicable
- [ ] Verify installation instructions work

---

### 11. Add Proper .gitignore Entries

**Action Items:**
- [x] Add `__pycache__/` (Python cache directories)
- [x] Add `.pytest_cache/` (Pytest cache)
- [x] Add `.coverage` (Coverage data file)
- [x] Add `htmlcov/` (Coverage HTML reports)
- [x] Add `.mypy_cache/` (Mypy cache)
- [x] Add `*.log` (Log files)
- [x] Add `.tvlog` (App log file)
- [x] Add `.tvrc` (User config file)
- [x] Add `.ruff_cache/` (Ruff cache)
- [x] Add `dist/` (Distribution files)
- [ ] Review and test .gitignore

---

### 12. Standardize on f-strings
**Issue:** Mix of %, .format(), and no f-strings.

**Action Items:**
- [x] Replace all `%` formatting with f-strings
- [x] Replace all `.format()` calls with f-strings (src has none; `tests/conftest.py` fills a script template with `.format()`)
- [x] Update logging calls to use f-strings (or lazy % for performance)
- [x] Update tests to use f-strings
- [x] Run ruff to catch remaining issues
- [x] Verify no regressions

**Examples:**
```python
# Before
message = "'{} doesn't exist, creating it"
logger.debug(message.format(directory))
logger.debug("Creating filename: {}".format(filename))

# After
logger.debug(f"'{directory}' doesn't exist, creating it")
logger.debug(f"Creating filename: {filename}")
```

---

## MEDIUM Priority (Improve Quality)

### 13. Add Comprehensive Error Handling

**Action Items:**

#### a) File system errors in note contents
- [x] Wrap file operations in try/except in `PlainTextNote.contents` property (in `PlainTextNoteBook._read`)
- [x] Handle `PermissionError`, `FileNotFoundError`, `IOError` (as `OSError`)
- [x] Log errors appropriately
- [x] Return empty string or raise custom exception (empty string)
- [x] Add tests for error scenarios

#### b) Config file parsing errors
- [x] Validate config file syntax in `terminal_velocity.py`
- [x] Show clear error messages for invalid config
- [x] Test with malformed config files
- [x] Document config file format (`tv3 --help`)

#### c) Editor execution errors
- [x] Handle editor command failures (`urwid_ui.system()` is gone; `MainFrame.edit` handles them)
- [x] Show user-friendly error messages (in the status line)
- [x] Test with non-existent editors

#### d) Watchdog observer errors
- [x] Handle exceptions in `FileEventHandler` methods
- [x] Log errors without crashing
- [ ] Test with permission issues

---

### 14. Improve Docstring Coverage

**Action Items:**
- [ ] Add module-level docstrings to all modules
- [ ] Add class-level docstrings to all classes
- [ ] Add function/method docstrings to all public functions
- [ ] Choose docstring format (recommend Google style)
- [ ] Document parameters, return values, and exceptions
- [ ] Add usage examples in docstrings
- [ ] Use pydocstyle or similar to validate
- [ ] Generate API documentation with Sphinx

**Example Google-style docstring:**
```python
def brute_force_search(notebook: PlainTextNoteBook, query: str) -> List[PlainTextNote]:
    """Search for notes matching the query string.

    Performs a case-insensitive search (for lowercase queries) or case-sensitive
    search (for queries with uppercase letters) across note titles and contents.
    All search words must match for a note to be included in results.

    Args:
        notebook: The notebook to search in.
        query: Space-separated search terms.

    Returns:
        List of notes that match all search terms.

    Examples:
        >>> notes = brute_force_search(notebook, "python tutorial")
        >>> len(notes)
        3
    """
```

---

### 15. Add Pre-commit Hooks
**Issue:** No automated code quality checks.

**Action Items:**
- [ ] Install pre-commit: `uv tool install pre-commit`
- [ ] Create `.pre-commit-config.yaml`
- [ ] Add ruff (replaces flake8)
- [ ] Add black for formatting
- [ ] Add isort for import sorting
- [ ] Add mypy for type checking
- [ ] Add trailing whitespace removal
- [ ] Add YAML/JSON validation
- [ ] Run `pre-commit install`
- [ ] Run `pre-commit run --all-files`
- [ ] Document in README

**Sample .pre-commit-config.yaml:**
```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.4.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.10.0
    hooks:
      - id: mypy
        additional_dependencies: [types-all]
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.5.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-json
```

---

### 16. Extract Magic Numbers to Constants

**Action Items:**
- [x] Create constants module or section in each file
- [x] Extract log file size: `LOG_MAX_BYTES = 1_000_000` (terminal_velocity.py)
- [x] ~~Extract watchdog settle time: `WATCHDOG_SETTLE_TIME = 0.5` (tests)~~ (superseded: tests poll with the `wait_until` fixture)
- [x] Extract log backup count: `LOG_BACKUP_COUNT = 1` (terminal_velocity.py)
- [x] Extract default extensions list as constant
- [x] Extract default exclude list as constant
- [x] Use SCREAMING_SNAKE_CASE for constants
- [ ] Document what each constant represents

**Example:**
```python
# Configuration constants
DEFAULT_EDITOR = "pico"
DEFAULT_EXTENSION = ".txt"
DEFAULT_EXTENSIONS = [".txt", ".md", ".markdown", ".rst"]
DEFAULT_EXCLUDE_DIRS = ["src", "backup", "ignore", "tmp", "old"]

# Logging constants
LOG_MAX_BYTES = 1_000_000  # 1 MB
LOG_BACKUP_COUNT = 1

# File watching constants
WATCHDOG_SETTLE_TIME = 0.5  # seconds to wait for filesystem to settle
```

---

### 17. Make Tests Less Timing-Sensitive

**Action Items:**
- [x] Replace hardcoded `time.sleep()` with polling functions
- [x] Create utility function `wait_for_condition(condition, timeout)` (the `wait_until` fixture in `tests/conftest.py`)
- [x] Update file watching tests to use polling
- [ ] Make timeouts configurable via environment variable
- [ ] Document timing requirements in test docstrings
- [ ] Test on slow CI environments

**Implementation:**
```python
def wait_for_condition(
    condition: Callable[[], bool], timeout: float = 2.0, poll_interval: float = 0.1
) -> bool:
    """Wait for a condition to become true.

    Args:
        condition: Callable that returns True when condition is met.
        timeout: Maximum time to wait in seconds.
        poll_interval: Time between polls in seconds.

    Returns:
        True if condition was met, False if timeout occurred.
    """
    start = time.time()
    while time.time() - start < timeout:
        if condition():
            return True
        time.sleep(poll_interval)
    return False


# Usage in tests:
assert wait_for_condition(lambda: len(notebook) == expected_count)
```

---

### 18. Add `__version__` to `__init__.py`

**Action Items:**
- [x] Add version string to `src/tv3/__init__.py`
- [ ] Add author information
- [x] Add package docstring
- [ ] Define `__all__` for clean imports
- [x] Consider using single-source versioning (`importlib.metadata`)
- [x] Keep the version in pyproject.toml and `__init__.py` in sync
- [ ] Add version display to CLI (`--version` flag)

**Implementation:**
```python
"""Terminal Velocity 3 - Fast note-taking for the command line.

Terminal Velocity is a fast note-taking application for the UNIX terminal.
It combines finding and creating notes in a single minimal interface and
delegates the note-taking itself to your $EDITOR.
"""

from importlib.metadata import version

__version__ = version("tv3")
__author__ = "Aramís Concepción Durán"
__license__ = "GPL-3.0-only"
__all__ = ["terminal_velocity", "tv_notebook", "urwid_ui"]

# Could also import main for convenience
from .terminal_velocity import main
```

---

## Additional Tasks

### Replace flake8 with ruff

**Action Items:**
- [x] Remove flake8 from CI workflow
- [x] Add ruff to CI workflow
- [x] Configure ruff in `pyproject.toml` or `ruff.toml`
- [x] Run `ruff check --fix .` to auto-fix issues
- [x] Run `ruff format .` to format code
- [x] Review and commit changes
- [x] Update documentation (CLAUDE.md lists the ruff commands)

**Ruff Configuration (pyproject.toml):**
```toml
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = [
    "E",   # pycodestyle errors
    "W",   # pycodestyle warnings
    "F",   # pyflakes
    "I",   # isort
    "B",   # flake8-bugbear
    "C4",  # flake8-comprehensions
    "UP",  # pyupgrade
]
ignore = [
    "E501",  # line too long (handled by formatter)
]

[tool.ruff.lint.per-file-ignores]
"__init__.py" = ["F401"]  # unused imports OK in __init__
```

---

## Testing Checklist

After implementing all changes, verify:
- [x] All tests pass: `pytest`
- [x] Coverage is adequate: `pytest --cov=src --cov-report=term-missing` (CI fails under 80%)
- [x] Type checking passes: `mypy src/`
- [x] Linting passes: `ruff check .`
- [x] Formatting is correct: `ruff format --check .`
- [ ] Pre-commit hooks pass: `pre-commit run --all-files`
- [ ] CI workflows pass on GitHub
- [ ] Manual testing of core functionality
- [ ] Documentation is up to date

---

## Progress Tracking

**Total Items:** 18 major tasks + numerous subtasks
**Completed:** 5 (4, 5, 8, 12, Replace flake8 with ruff)
**In Progress:** 11 (2, 3, 6, 7, 9, 10, 11, 13, 16, 17, 18)
**Not Started:** 2 (14, 15)

**Target Completion:** [Set date]

---

## Notes

- Keep this document updated as work progresses
- Mark items complete with [x] when done
- Add any issues or blockers encountered
- Reference this document in commit messages
- Update estimates if scope changes

---

**Last Updated:** 2026-10-12
**Maintainer:** [Your Name]
