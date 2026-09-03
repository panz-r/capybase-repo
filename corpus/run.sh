#!/bin/bash
# The corpus suite's ONLY entry point (plain Python, never pytest).
# Requires the datasets fetched (see corpus/README.md).
cd "$(dirname "$0")/.."
exec .venv/bin/python corpus/run.py "${1:-all}"
