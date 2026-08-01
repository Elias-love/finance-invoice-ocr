#!/bin/zsh
set -eu

PROJECT_DIR="/Users/wangshijin/finance-invoice-open-local"
PYTHON_BIN="/usr/local/bin/python3"

mkdir -p "$PROJECT_DIR/logs"
cd "$PROJECT_DIR"

exec "$PYTHON_BIN" app.py
