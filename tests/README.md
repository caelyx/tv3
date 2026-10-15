# TV3 Test Suite

This directory contains the pytest suite for Terminal Velocity 3. To list the current tests and count:

```bash
uv run pytest --collect-only -q
```

## Installation

Create the development environment (tv3 plus the dev dependency group) from `uv.lock`:

```bash
uv sync
```

## Running Tests

### Run all tests:
```bash
uv run pytest
```

### Run specific test file:
```bash
uv run pytest tests/test_tv_notebook.py
uv run pytest tests/test_terminal_velocity.py
uv run pytest tests/test_urwid_ui.py
uv run pytest tests/test_ranking.py
uv run pytest tests/test_integration.py
```

### Run specific test class:
```bash
uv run pytest tests/test_tv_notebook.py::TestPlainTextNote
```

### Run specific test:
```bash
uv run pytest tests/test_tv_notebook.py::TestPlainTextNote::test_contents
```

### Run with coverage:
```bash
uv run pytest --cov=src/tv3 --cov-report=html
```

Then open `htmlcov/index.html` in a browser to view coverage report.

### Skip tests marked slow (real filesystem-watcher tests):
```bash
uv run pytest -m "not slow"
```

### Run with verbose output:
```bash
uv run pytest -v
```

### Run and stop at first failure:
```bash
uv run pytest -x
```

### Run last failed tests:
```bash
uv run pytest --lf
```

## Test Organization

### `conftest.py`
Shared fixtures:
- `temp_notes_dir`: Empty notes directory under pytest's `tmp_path`
- `populated_notes_dir`: Notes directory with 4 sample `.txt`/`.md` notes
- `nested_notes_dir`: Notes directory with `work/`, `personal/` and `archive/` subdirectories
- `stub_editor` / `make_stub_editor`: A real executable standing in for `$EDITOR`; records each run's arguments and can be made to fail
- `make_frame`: A `MainFrame` on a real notebook in `notes_dir`, run by a headless `urwid_ui.MainLoop` (see `tests/ui_helpers.py`)

### Test modules
- `test_tv_notebook.py`: data layer (`tv_notebook.py`)
- `test_terminal_velocity.py`: CLI and configuration (`terminal_velocity.py`)
- `test_urwid_ui.py`: UI components (`urwid_ui.py`)
- `test_ranking.py`: fuzzy title matching, match spans and snippets (`ranking.py`)
- `ui_helpers.py`: not a test module; a headless screen and helpers for driving and inspecting a `MainFrame`
- `test_integration.py`: end-to-end, integration and regression tests

See each file for its test classes.

## Test Coverage

The test suite provides comprehensive coverage of:

✅ **Core Functionality**
- Note creation, reading, updating, deletion
- Search and filtering
- File watching and automatic synchronization
- Configuration loading and parsing
- UI interactions and keyboard shortcuts

✅ **Edge Cases**
- Empty notebooks
- Invalid note titles
- Unicode characters
- Very long file names
- Concurrent operations
- File system errors
- Missing directories

✅ **Integration**
- UI + Notebook interaction
- File watcher + UI updates
- Configuration + behavior
- External file creation/deletion
- Multi-threaded scenarios

## Writing New Tests

### Test Structure
```python
def test_descriptive_name(fixture1, fixture2):
    """Clear description of what is being tested."""
    # Arrange
    setup_code()

    # Act
    result = function_under_test()

    # Assert
    assert result == expected
```

### Using Fixtures
```python
def test_with_temp_dir(temp_notes_dir):
    """Temp directory is automatically created and cleaned up."""
    note_path = Path(temp_notes_dir) / "test.txt"
    note_path.write_text("content")
    assert note_path.exists()
```

### Testing Against Real Code
- Use real files in `tmp_path`; don't mock the file system.
- Launch the editor as a real subprocess running a small stub editor script, and assert on its effect (e.g. the file it wrote).
- Patch only at process or terminal boundaries (e.g. the urwid main loop, `sys.argv`), and assert on what crossed the boundary.
- Never assert only that a mock was called; check observable behaviour of the code under test.

## Continuous Integration

These tests are designed to run in CI environments. Make sure to:
- Run tests before committing
- Maintain > 80% code coverage
- Add tests for new features
- Add regression tests for bug fixes

## Known Issues

- Some file watching tests may be flaky on slow systems due to timing
- Thread safety tests may occasionally fail under high system load

## Contributing

When adding new functionality:
1. Write tests first (TDD)
2. Ensure all tests pass
3. Check code coverage
4. Add integration tests if needed
5. Document any new fixtures or test patterns
