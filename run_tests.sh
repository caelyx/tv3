#!/bin/bash
# Convenience script for running tests

set -e

echo "===================================="
echo "Terminal Velocity 3 - Test Runner"
echo "===================================="
echo ""

# Check if pytest is installed
if ! command -v pytest &> /dev/null; then
    echo "pytest not found. Installing test dependencies..."
    pip install -e ".[test]"
fi

# Parse arguments
if [ "$1" == "coverage" ]; then
    echo "Running tests with coverage..."
    pytest --cov=src --cov-report=html --cov-report=term-missing
    echo ""
    echo "Coverage report generated in htmlcov/index.html"
elif [ "$1" == "fast" ]; then
    echo "Running fast tests only (skipping slow tests)..."
    pytest -m "not slow"
elif [ "$1" == "unit" ]; then
    echo "Running unit tests only..."
    pytest tests/test_tv_notebook.py tests/test_terminal_velocity.py tests/test_urwid_ui.py
elif [ "$1" == "integration" ]; then
    echo "Running integration tests only..."
    pytest tests/test_integration.py
elif [ "$1" == "verbose" ]; then
    echo "Running tests with verbose output..."
    pytest -vv
elif [ "$1" == "help" ] || [ "$1" == "-h" ] || [ "$1" == "--help" ]; then
    echo "Usage: ./run_tests.sh [option]"
    echo ""
    echo "Options:"
    echo "  (none)        Run all tests"
    echo "  coverage      Run with coverage report"
    echo "  fast          Run fast tests only"
    echo "  unit          Run unit tests only"
    echo "  integration   Run integration tests only"
    echo "  verbose       Run with verbose output"
    echo "  help          Show this help message"
    echo ""
    exit 0
else
    echo "Running all tests..."
    pytest
fi

echo ""
echo "===================================="
echo "Tests completed!"
echo "===================================="
