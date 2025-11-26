# TODO: Terminal Velocity 3 Improvements

This document tracks improvements identified in the comprehensive codebase review.

## Status Legend
- [ ] Not started
- [x] Completed
- [~] In progress

---

## CRITICAL Priority (Do First)

### 2. Add Path Traversal Protection 🔒
**Location:** `src/tv_notebook.py:200-222`
**Issue:** No validation to prevent creating notes outside the notes directory using `../` in titles.
**Security Risk:** HIGH - files could be created anywhere the user has write permissions.

**Action Items:**
- [ ] Add path normalization and validation in `PlainTextNoteBook.add_new()`
- [ ] Ensure `os.path.realpath()` checks that paths stay within notes directory
- [ ] Add test cases for path traversal attempts
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
**Location:** `src/tv_notebook.py:174-177`
**Issue:** Observer thread is never stopped or cleaned up.
**Impact:** Thread leaks; prevents proper shutdown.

**Action Items:**
- [ ] Add `close()` method to `PlainTextNoteBook` class
- [ ] Call `observer.stop()` and `observer.join()` in cleanup
- [ ] Add context manager support (`__enter__`/`__exit__`)
- [ ] Update documentation about proper notebook lifecycle
- [ ] Add tests for cleanup behavior

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
**Location:** `src/terminal_velocity.py:140`
**Issue:** The try/except block is commented out, so errors crash the app ungracefully.
**Impact:** Poor user experience; no error recovery.

**Action Items:**
- [ ] Uncomment and fix the try/except block
- [ ] Add specific exception handlers for common errors
- [ ] Display user-friendly error messages
- [ ] Log exceptions properly
- [ ] Add graceful exit with cleanup
- [ ] Test error scenarios

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
**Files:** `requirements.txt` vs `setup.py`
**Issue:** Inconsistent version pinning; `watchdog` not pinned at all.
**Impact:** Unpredictable behavior across environments.

**Action Items:**
- [ ] Pin `watchdog` version in setup.py (e.g., `watchdog>=2.0.0,<5.0.0`)
- [ ] Ensure requirements.txt matches setup.py
- [ ] Document why urwid is pinned to 2.1.2
- [ ] Test with specified versions
- [ ] Consider using `pip-tools` for dependency management

---

### 6. Update GitHub Actions to v4+
**Locations:** `.github/workflows/pythonapp.yml` and `codeql-analysis.yml`
**Issue:** Using deprecated GitHub Actions v1/v2.
**Impact:** May break in future; security vulnerabilities.

**Action Items:**
- [ ] Update `actions/checkout@v1` → `actions/checkout@v4`
- [ ] Update `actions/setup-python@v1` → `actions/setup-python@v5`
- [ ] Update `github/codeql-action/*@v1` → `github/codeql-action/*@v3`
- [ ] Test workflows locally with `act` if possible
- [ ] Verify workflows run successfully in GitHub

---

## HIGH Priority (Do Soon)

### 7. Add Type Hints 📝
**Analysis:** 0 type hints found in entire codebase (789 LOC)
**Issue:** No static type checking possible; harder to maintain and refactor.

**Action Items:**
- [ ] Add type hints to `tv_notebook.py` module
  - [ ] `PlainTextNote` class
  - [ ] `PlainTextNoteBook` class
  - [ ] `brute_force_search()` function
  - [ ] `FileEventHandler` class
- [ ] Add type hints to `terminal_velocity.py` module
  - [ ] `main()` function
- [ ] Add type hints to `urwid_ui.py` module
  - [ ] All widget classes
  - [ ] `launch()` function
  - [ ] `system()` function
- [ ] Add `from typing import` imports (List, Dict, Optional, Union)
- [ ] Add `py.typed` marker file
- [ ] Configure mypy in setup.cfg
- [ ] Add mypy to pre-commit hooks
- [ ] Run mypy and fix all errors

**Resources:**
- Python typing documentation: https://docs.python.org/3/library/typing.html
- mypy documentation: https://mypy.readthedocs.io/

---

### 8. Modernize Python Code
**Issue:** Old Python 2 patterns still present.

**Action Items:**

#### a) Remove explicit `object` inheritance (Python 2 style)
- [ ] Remove `(object)` from `PlainTextNote` class (tv_notebook.py:57)
- [ ] Remove `(object)` from `PlainTextNoteBook` class (tv_notebook.py:132)
- [ ] Verify all classes use implicit new-style classes

#### b) Fix property setter syntax
- [ ] Fix `@title.setter` method name in `PlainTextNote` (tv_notebook.py:82-83)
- [ ] Should be `def title(self, ...)` not `def set_title(self, ...)`
- [ ] Test property setter behavior

#### c) Use modern string formatting
- [ ] Replace all `%` formatting with f-strings
- [ ] Replace all `.format()` calls with f-strings
- [ ] Update tests to use f-strings
- [ ] Verify no formatting regressions

---

### 9. Add Tests to CI Workflow
**Location:** `.github/workflows/pythonapp.yml`
**Issue:** Workflow only runs linting, not tests!

**Action Items:**
- [ ] Add pytest execution step to workflow
- [ ] Add coverage reporting (xml and term-missing)
- [ ] Upload coverage to Codecov or similar
- [ ] Add test results reporting
- [ ] Set coverage threshold (e.g., 80%)
- [ ] Fail build if tests fail or coverage drops

**Implementation:**
```yaml
- name: Run tests with coverage
  run: |
    pip install -e ".[test]"
    pytest --cov=src --cov-report=xml --cov-report=term-missing --cov-fail-under=80
- name: Upload coverage
  uses: codecov/codecov-action@v4
  with:
    file: ./coverage.xml
```

---

### 10. Fix Documentation Inconsistencies

**Action Items:**

#### a) Fix README.md vs setup.py contradiction
- [ ] Update README.md line 34 (says urwid 1.1.1, should be 2.1.2)
- [ ] Document why urwid is pinned to 2.1.2

#### b) Fix CLAUDE.md incorrect default
- [ ] Update CLAUDE.md line 40 about default editor
- [ ] Should mention `$EDITOR` environment variable first, then `pico`

#### c) General documentation cleanup
- [ ] Ensure all code examples are correct
- [ ] Update version numbers where applicable
- [ ] Verify installation instructions work

---

### 11. Add Proper .gitignore Entries

**Action Items:**
- [ ] Add `__pycache__/` (Python cache directories)
- [ ] Add `.pytest_cache/` (Pytest cache)
- [ ] Add `.coverage` (Coverage data file)
- [ ] Add `htmlcov/` (Coverage HTML reports)
- [ ] Add `.mypy_cache/` (Mypy cache)
- [ ] Add `*.log` (Log files)
- [ ] Add `.tvlog` (App log file)
- [ ] Add `.tvrc` (User config file)
- [ ] Add `.ruff_cache/` (Ruff cache)
- [ ] Add `dist/` (Distribution files)
- [ ] Review and test .gitignore

---

### 12. Standardize on f-strings
**Issue:** Mix of %, .format(), and no f-strings.

**Action Items:**
- [ ] Replace all `%` formatting with f-strings
- [ ] Replace all `.format()` calls with f-strings
- [ ] Update logging calls to use f-strings (or lazy % for performance)
- [ ] Update tests to use f-strings
- [ ] Run ruff to catch remaining issues
- [ ] Verify no regressions

**Examples:**
```python
# Before
message = '\'{} doesn\'t exist, creating it'
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
- [ ] Wrap file operations in try/except in `PlainTextNote.contents` property
- [ ] Handle `PermissionError`, `FileNotFoundError`, `IOError`
- [ ] Log errors appropriately
- [ ] Return empty string or raise custom exception
- [ ] Add tests for error scenarios

#### b) Config file parsing errors
- [ ] Validate config file syntax in `terminal_velocity.py`
- [ ] Show clear error messages for invalid config
- [ ] Test with malformed config files
- [ ] Document config file format

#### c) Editor execution errors
- [ ] Handle editor command failures in `urwid_ui.system()`
- [ ] Show user-friendly error messages
- [ ] Test with non-existent editors

#### d) Watchdog observer errors
- [ ] Handle exceptions in `FileEventHandler` methods
- [ ] Log errors without crashing
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
- [ ] Install pre-commit: `pip install pre-commit`
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
- [ ] Create constants module or section in each file
- [ ] Extract log file size: `LOG_MAX_BYTES = 1_000_000` (terminal_velocity.py:129)
- [ ] Extract watchdog settle time: `WATCHDOG_SETTLE_TIME = 0.5` (tests)
- [ ] Extract log backup count: `LOG_BACKUP_COUNT = 0` (terminal_velocity.py:130)
- [ ] Extract default extensions list as constant
- [ ] Extract default exclude list as constant
- [ ] Use SCREAMING_SNAKE_CASE for constants
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
LOG_BACKUP_COUNT = 0

# File watching constants
WATCHDOG_SETTLE_TIME = 0.5  # seconds to wait for filesystem to settle
```

---

### 17. Make Tests Less Timing-Sensitive

**Action Items:**
- [ ] Replace hardcoded `time.sleep()` with polling functions
- [ ] Create utility function `wait_for_condition(condition, timeout)`
- [ ] Update file watching tests to use polling
- [ ] Make timeouts configurable via environment variable
- [ ] Document timing requirements in test docstrings
- [ ] Test on slow CI environments

**Implementation:**
```python
def wait_for_condition(condition: Callable[[], bool],
                      timeout: float = 2.0,
                      poll_interval: float = 0.1) -> bool:
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
- [ ] Add version string to `src/__init__.py`
- [ ] Add author information
- [ ] Add package docstring
- [ ] Define `__all__` for clean imports
- [ ] Consider using single-source versioning
- [ ] Update setup.py to read version from `__init__.py`
- [ ] Add version display to CLI (`--version` flag)

**Implementation:**
```python
"""Terminal Velocity 3 - Fast note-taking for the command line.

Terminal Velocity is a fast note-taking application for the UNIX terminal.
It combines finding and creating notes in a single minimal interface and
delegates the note-taking itself to your $EDITOR.
"""

__version__ = '0.1.0'
__author__ = 'Aramís Concepción Durán'
__license__ = 'GNU General Public License, Version 3'
__all__ = ['terminal_velocity', 'tv_notebook', 'urwid_ui']

# Could also import main for convenience
from .terminal_velocity import main
```

---

## Additional Tasks

### Replace flake8 with ruff

**Action Items:**
- [ ] Remove flake8 from CI workflow
- [ ] Add ruff to CI workflow
- [ ] Configure ruff in `pyproject.toml` or `ruff.toml`
- [ ] Run `ruff check --fix .` to auto-fix issues
- [ ] Run `ruff format .` to format code
- [ ] Review and commit changes
- [ ] Update documentation

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
- [ ] All tests pass: `pytest`
- [ ] Coverage is adequate: `pytest --cov=src --cov-report=term-missing`
- [ ] Type checking passes: `mypy src/`
- [ ] Linting passes: `ruff check .`
- [ ] Formatting is correct: `ruff format --check .`
- [ ] Pre-commit hooks pass: `pre-commit run --all-files`
- [ ] CI workflows pass on GitHub
- [ ] Manual testing of core functionality
- [ ] Documentation is up to date

---

## Progress Tracking

**Total Items:** 18 major tasks + numerous subtasks
**Completed:** 0
**In Progress:** 0
**Not Started:** 18

**Target Completion:** [Set date]

---

## Notes

- Keep this document updated as work progresses
- Mark items complete with [x] when done
- Add any issues or blockers encountered
- Reference this document in commit messages
- Update estimates if scope changes

---

**Last Updated:** 2025-11-26
**Maintainer:** [Your Name]
