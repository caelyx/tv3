# Testing Documentation for TV3

## Overview

Terminal Velocity 3 is tested with pytest. The suite covers the data layer, the CLI/config entry point, the urwid UI, and integration and regression scenarios. To see the current tests and count, run:

```bash
uv run pytest --collect-only -q
```

## Quick Start

### 1. Install Dependencies

```bash
# Create .venv with tv3 (editable) and the dev dependency group
uv sync
```

### 2. Run Tests

```bash
# Run all tests
uv run pytest

# Or use the convenience script
./run_tests.sh
```

## Test Suite Structure

```
tests/
├── README.md                 # Test suite notes
├── conftest.py               # Shared fixtures
├── ui_helpers.py             # Headless screen and helpers for driving MainFrame
├── test_tv_notebook.py       # Data layer (tv_notebook.py)
├── test_terminal_velocity.py # CLI and config (terminal_velocity.py)
├── test_urwid_ui.py          # UI components (urwid_ui.py)
├── test_ranking.py           # Fuzzy matching, spans and snippets (ranking.py)
└── test_integration.py       # Integration and regression tests

pyproject.toml                # Pytest configuration ([tool.pytest.ini_options])
run_tests.sh                  # Convenience test runner script
```

See the test files themselves for the test classes and what each covers.

## Running Tests

### Basic Usage

```bash
# All tests
uv run pytest

# Specific test file
uv run pytest tests/test_tv_notebook.py

# Specific test class
uv run pytest tests/test_tv_notebook.py::TestPlainTextNote

# Specific test
uv run pytest tests/test_tv_notebook.py::TestPlainTextNote::test_contents

# Verbose output
uv run pytest -v

# Stop on first failure
uv run pytest -x

# Run last failed
uv run pytest --lf
```

### With Coverage

```bash
# Run with coverage report
uv run pytest --cov=src/tv3 --cov-report=html --cov-report=term-missing

# Or use the script
./run_tests.sh coverage

# Open coverage report
open htmlcov/index.html
```

### Using the Helper Script

```bash
./run_tests.sh                # Run all tests
./run_tests.sh coverage       # Run with coverage
./run_tests.sh fast           # Skip tests marked slow (real filesystem-watcher tests)
./run_tests.sh unit           # Unit tests only
./run_tests.sh integration    # Integration tests only
./run_tests.sh verbose        # Verbose output
./run_tests.sh help           # Show help
```

## Test Fixtures (conftest.py)

- **`temp_notes_dir`**: Empty notes directory under pytest's `tmp_path`
- **`populated_notes_dir`**: Notes directory with 4 sample `.txt`/`.md` notes
- **`nested_notes_dir`**: Notes directory with `work/`, `personal/` and `archive/` subdirectories
- **`stub_editor`** / **`make_stub_editor`**: A real executable standing in for `$EDITOR`; records each run's arguments and can be made to fail
- **`make_frame`**: A `MainFrame` on a real notebook in `notes_dir`, run by a headless `urwid_ui.MainLoop` (see `tests/ui_helpers.py`)

## UI Helpers (tests/ui_helpers.py)

UI tests drive a real `MainFrame` with keys and assert on what it draws:

- **`press`** / **`type_text`**: Send keys, or type text, to the frame
- **`screen_lines`**, **`search_line`**, **`status_line`**: The rendered screen's rows, search row and status row
- **`list_lines`**: The list's rows, titles and snippets, without padding
- **`list_titles`**: The list's titles in order, without snippet rows
- **`highlighted`**: Titles of rows drawn as selected
- **`marked`**: The text drawn with the match highlight on a row
- **`footer_rows`**: The status line's rows as `(text, attributes)`, or `[]` when there is none (a failed rename shows two: the error and the prompt)
- **`RecordingScreen`** / **`ScriptedScreen`** / **`run_headless`**: A headless screen for running a real `urwid_ui.MainLoop`

## Continuous Integration

These tests are CI-ready:

```bash
# In CI pipeline
uv sync --locked
uv run pytest --cov=src/tv3 --cov-report=xml
```

## Writing New Tests

### Test Template

```python
def test_descriptive_name(fixture1, fixture2):
    """Clear description of what is being tested."""
    # Arrange - Set up test data
    setup_code()

    # Act - Perform the action
    result = function_under_test()

    # Assert - Verify the result
    assert result == expected
```

### Guidelines

1. **Exercise real code.** Use real files in `tmp_path` rather than mocking the file system.
2. **Launch the editor for real.** Use a real subprocess running a small stub editor script, then assert on its effect (e.g. the file it wrote).
3. **Patch only at process or terminal boundaries** (e.g. the urwid main loop, `sys.argv`), and assert on what crossed the boundary.
4. **Never assert only that a mock was called.** A test must check observable behaviour of the code under test.
5. **Clear, descriptive names** that explain what's tested
6. **Test both success and failure** cases
7. **Add regression tests** for bug fixes

## Coverage Goals

- **Minimum**: 80% code coverage
- **Target**: 90%+ code coverage
- **Critical paths**: 100% coverage

Run coverage report to see current stats:
```bash
./run_tests.sh coverage
```

## Known Issues

- File watching tests may be timing-sensitive on slow systems
- Threading tests may occasionally fail under extreme system load

## Future Enhancements

Potential areas for additional testing:
- Performance tests for large notebooks (1000+ notes)
- Stress tests for concurrent operations
- UI accessibility tests
- Windows compatibility tests (CI covers Linux and macOS)
- Fuzzing tests for search functionality

## Contributing

When contributing:
1. Write tests for new features
2. Ensure all tests pass: `uv run pytest`
3. Check coverage: `./run_tests.sh coverage`
4. Add integration tests for complex features
5. Document new fixtures or patterns

## Troubleshooting

### Tests won't run / import errors
```bash
# From the project root, recreate the environment from uv.lock
uv sync

# Verify pytest installation
uv run pytest --version
```

### Timing failures
```bash
# Some tests involve file watching with delays.
# Skip tests marked slow (real filesystem-watcher tests) with:
./run_tests.sh fast
```

Run `./run_tests.sh help` to get started!
