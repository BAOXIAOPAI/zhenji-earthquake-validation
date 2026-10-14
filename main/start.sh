#!/bin/zsh
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/python main/server.py "${1:-8791}"
