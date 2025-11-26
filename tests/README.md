# TV3 Test Suite

This directory contains comprehensive tests for Terminal Velocity 3.

## Installation

Install test dependencies:

```bash
pip install -e ".[test]"
```

Or install manually:

```bash
pip install pytest pytest-cov pytest-mock
```

## Running Tests

### Run all tests:
```bash
pytest
```

### Run specific test file:
```bash
pytest tests/test_tv_notebook.py
pytest tests/test_terminal_velocity.py
pytest tests/test_urwid_ui.py
pytest tests/test_integration.py
```

### Run specific test class:
```bash
pytest tests/test_tv_notebook.py::TestPlainTextNote
```

### Run specific test:
```bash
pytest tests/test_tv_notebook.py::TestPlainTextNote::test_note_creation
```

### Run with coverage:
```bash
pytest --cov=src --cov-report=html
```

Then open `htmlcov/index.html` in a browser to view coverage report.

### Run only fast tests (skip slow integration tests):
```bash
pytest -m "not slow"
```

### Run with verbose output:
```bash
pytest -v
```

### Run and stop at first failure:
```bash
pytest -x
```

### Run last failed tests:
```bash
pytest --lf
```

## Test Organization

### `conftest.py`
Shared fixtures and test configuration:
- `temp_notes_dir`: Clean temporary directory for tests
- `populated_notes_dir`: Temp directory with sample notes
- `nested_notes_dir`: Temp directory with nested subdirectories
- `sample_config`: Sample configuration file
- `mock_editor`: Mock editor for UI tests

### `test_tv_notebook.py`
Tests for the data layer (`tv_notebook.py`):
- **TestPlainTextNote**: Note creation, properties, content handling
- **TestPlainTextNoteBook**: Notebook initialization, note management
- **TestBruteForceSearch**: Search functionality and filtering
- **TestFileWatching**: File system watching with watchdog
- **TestThreadSafety**: Concurrent operations and race conditions
- **TestEdgeCases**: Edge cases and error handling

### `test_terminal_velocity.py`
Tests for CLI and configuration (`terminal_velocity.py`):
- **TestArgumentParsing**: Command-line argument handling
- **TestConfigFile**: Configuration file loading and parsing
- **TestLogging**: Logging configuration
- **TestExtensionParsing**: File extension handling
- **TestExcludeParsing**: Directory exclusion
- **TestEditorEnvironment**: Editor selection logic
- **TestPrintConfig**: Configuration display
- **TestPathExpansion**: Path expansion (~, relative paths)
- **TestHelpAndVersion**: Help and version output

### `test_urwid_ui.py`
Tests for UI components (`urwid_ui.py`):
- **TestNoteWidget**: Individual note display widget
- **TestAutocompleteWidget**: Search box with autocomplete
- **TestNoteFilterListBox**: Filtered note list
- **TestMainFrame**: Main UI frame and keyboard shortcuts
- **TestSystemFunction**: External command execution
- **TestPlaceholderText**: Placeholder text display
- **TestEdgeCases**: UI edge cases and error handling

### `test_integration.py`
End-to-end integration tests:
- **TestEndToEndWorkflow**: Complete user workflows
- **TestConcurrencyIntegration**: Concurrent operations across modules
- **TestErrorRecoveryIntegration**: Error handling across system
- **TestConfigurationIntegration**: Configuration affecting behavior
- **TestRegressionTests**: Tests for previously fixed bugs

## Test Coverage

The test suite provides comprehensive coverage of:

✅ **Core Functionality**
- Note creation, reading, updating, deletion
- Search and filtering
- File watching and automatic synchronization
- Configuration loading and parsing
- UI interactions and keyboard shortcuts

✅ **Bug Fixes** (Regression Tests)
- Python 3 shebang (Issue #1)
- `shlex.quote` instead of deprecated `pipes.quote` (Issue #2)
- File watcher path handling (Issue #3)
- Thread-safe note list operations (Issue #4)

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

### Mocking UI Components
```python
@patch('urwid_ui.system')
def test_ui_action(mock_system, temp_notes_dir):
    """Mock external commands to test UI without side effects."""
    frame = urwid_ui.MainFrame(temp_notes_dir, 'vim', '.txt', ['.txt'])
    frame.loop = Mock()
    # Test UI actions
```

## Continuous Integration

These tests are designed to run in CI environments. Make sure to:
- Run tests before committing
- Maintain > 80% code coverage
- Add tests for new features
- Add regression tests for bug fixes

## Known Issues

- Some file watching tests may be flaky on slow systems due to timing
- UI tests require mocking urwid's main loop
- Thread safety tests may occasionally fail under high system load

## Contributing

When adding new functionality:
1. Write tests first (TDD)
2. Ensure all tests pass
3. Check code coverage
4. Add integration tests if needed
5. Document any new fixtures or test patterns
