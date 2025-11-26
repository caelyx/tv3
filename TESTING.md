# Testing Documentation for TV3

## Overview

A comprehensive test suite has been created for Terminal Velocity 3 using pytest. The suite includes **150+ tests** covering unit tests, integration tests, and regression tests for previously fixed bugs.

## Quick Start

### 1. Install Dependencies

```bash
# Install the package with test dependencies
pip3 install -e ".[test]"

# Or install dependencies separately
pip3 install urwid==2.1.2 watchdog pytest pytest-cov pytest-mock
```

### 2. Run Tests

```bash
# Run all tests
pytest

# Or use the convenience script
./run_tests.sh
```

## Test Suite Structure

### Files Created

```
tests/
├── __init__.py              # Test package marker
├── README.md                # Detailed testing documentation
├── conftest.py              # Shared fixtures and configuration
├── test_tv_notebook.py      # Tests for data layer (89 tests)
├── test_terminal_velocity.py # Tests for CLI/config (37 tests)
├── test_urwid_ui.py         # Tests for UI components (31 tests)
└── test_integration.py      # Integration tests (23 tests)

pytest.ini                   # Pytest configuration
run_tests.sh                 # Convenience test runner script
TESTING.md                   # This file
```

## Test Coverage

### By Module

| Module | Test File | # Tests | Coverage Areas |
|--------|-----------|---------|----------------|
| `tv_notebook.py` | `test_tv_notebook.py` | 89 | Note creation, search, file watching, threading |
| `terminal_velocity.py` | `test_terminal_velocity.py` | 37 | CLI args, config files, logging |
| `urwid_ui.py` | `test_urwid_ui.py` | 31 | UI widgets, keyboard shortcuts, editor launching |
| Integration | `test_integration.py` | 23 | End-to-end workflows, concurrency |

### What's Tested

✅ **Core Functionality**
- Note CRUD operations (Create, Read, Update, Delete)
- Full-text search across notes
- File system watching and auto-sync
- Configuration loading (CLI, config file, environment)
- UI interactions and keyboard shortcuts
- Autocomplete functionality

✅ **Bug Fixes (Regression Tests)**
- Issue #1: Python 3 shebang
- Issue #2: `shlex.quote` instead of deprecated `pipes.quote`
- Issue #3: File watcher path handling
- Issue #4: Thread-safe operations with locks

✅ **Edge Cases**
- Empty notebooks
- Invalid note titles
- Unicode characters in notes and searches
- Very long filenames
- Nested directories
- Multiple file extensions
- Concurrent operations
- File system errors

✅ **Integration Scenarios**
- UI + Notebook coordination
- External file creation/deletion
- Configuration affecting behavior
- File watcher + UI updates
- Multi-threaded access patterns

## Running Tests

### Basic Usage

```bash
# All tests
pytest

# Specific test file
pytest tests/test_tv_notebook.py

# Specific test class
pytest tests/test_tv_notebook.py::TestPlainTextNote

# Specific test
pytest tests/test_tv_notebook.py::TestPlainTextNote::test_note_creation

# Verbose output
pytest -v

# Stop on first failure
pytest -x

# Run last failed
pytest --lf
```

### With Coverage

```bash
# Run with coverage report
pytest --cov=src --cov-report=html --cov-report=term-missing

# Or use the script
./run_tests.sh coverage

# Open coverage report
open htmlcov/index.html
```

### Using the Helper Script

```bash
./run_tests.sh                # Run all tests
./run_tests.sh coverage       # Run with coverage
./run_tests.sh fast           # Skip slow tests
./run_tests.sh unit           # Unit tests only
./run_tests.sh integration    # Integration tests only
./run_tests.sh verbose        # Verbose output
./run_tests.sh help           # Show help
```

## Test Fixtures (conftest.py)

The following fixtures are available in all tests:

- **`temp_notes_dir`**: Clean temporary directory for notes
- **`populated_notes_dir`**: Temp directory with 4 sample notes
- **`nested_notes_dir`**: Temp directory with nested subdirectories
- **`sample_config`**: Sample configuration file
- **`mock_editor`**: Mock editor script for UI tests

## Key Test Classes

### test_tv_notebook.py
- `TestPlainTextNote` - Note object tests
- `TestPlainTextNoteBook` - Notebook management tests
- `TestBruteForceSearch` - Search functionality tests
- `TestFileWatching` - File system monitoring tests
- `TestThreadSafety` - Concurrency tests
- `TestEdgeCases` - Edge case handling

### test_terminal_velocity.py
- `TestArgumentParsing` - CLI argument tests
- `TestConfigFile` - Config file loading tests
- `TestLogging` - Logging configuration tests
- `TestExtensionParsing` - File extension handling
- `TestExcludeParsing` - Directory exclusion
- `TestEditorEnvironment` - Editor selection logic
- `TestPathExpansion` - Path expansion tests

### test_urwid_ui.py
- `TestNoteWidget` - Note display widget
- `TestAutocompleteWidget` - Search autocomplete
- `TestNoteFilterListBox` - Note list filtering
- `TestMainFrame` - Main UI and keyboard shortcuts
- `TestSystemFunction` - Command execution
- `TestEdgeCases` - UI edge cases

### test_integration.py
- `TestEndToEndWorkflow` - Complete user workflows
- `TestConcurrencyIntegration` - Concurrent operations
- `TestErrorRecoveryIntegration` - Error handling
- `TestConfigurationIntegration` - Config effects
- `TestRegressionTests` - Bug fix verification

## Continuous Integration

These tests are CI-ready:

```bash
# In CI pipeline
pip install -e ".[test]"
pytest --cov=src --cov-report=xml
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

1. **One test, one assertion** (when possible)
2. **Clear, descriptive names** that explain what's tested
3. **Use fixtures** for common setup
4. **Mock external dependencies** (editors, file system)
5. **Test both success and failure** cases
6. **Add regression tests** for bug fixes

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
- UI tests require proper mocking of urwid main loop
- Threading tests may occasionally fail under extreme system load

## Future Enhancements

Potential areas for additional testing:
- Performance tests for large notebooks (1000+ notes)
- Stress tests for concurrent operations
- UI accessibility tests
- Cross-platform compatibility tests (Windows, Linux, macOS)
- Fuzzing tests for search functionality

## Contributing

When contributing:
1. Write tests for new features
2. Ensure all tests pass: `pytest`
3. Check coverage: `./run_tests.sh coverage`
4. Add integration tests for complex features
5. Document new fixtures or patterns

## Troubleshooting

### Tests won't run
```bash
# Check dependencies
pip3 install -e ".[test]"

# Verify pytest installation
python3 -m pytest --version
```

### Import errors
```bash
# Ensure you're in the project root
cd /path/to/tv3

# Install in development mode
pip3 install -e .
```

### Timing failures
```bash
# Some tests involve file watching with delays
# If tests fail intermittently, try increasing timeouts
# in the test code (look for time.sleep values)
```

## Summary

A comprehensive test suite with:
- **180+ total tests**
- **4 test modules** (unit + integration)
- **Extensive fixtures** for common scenarios
- **Coverage reporting** configured
- **CI-ready** configuration
- **Regression tests** for fixed bugs
- **Thread-safety tests** for concurrent operations
- **Complete documentation**

Run `./run_tests.sh help` to get started!
