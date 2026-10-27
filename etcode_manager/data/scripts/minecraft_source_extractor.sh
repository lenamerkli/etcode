#!/bin/bash

# Allow Python version to be overridden via environment variable
ETCODE_PYTHON_VERSION="${ETCODE_PYTHON_VERSION:-/opt/etcode/.venv/bin/python3}"
ETCODE_PYTHON_SCRIPT="/opt/etcode/minecraft_source_extractor.py"

# Execute
"$ETCODE_PYTHON_VERSION" "$ETCODE_PYTHON_SCRIPT" "$@"
