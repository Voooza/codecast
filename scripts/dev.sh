#!/usr/bin/env bash
# Run the codecast studio with autoreload.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run codecast serve --reload "$@"
