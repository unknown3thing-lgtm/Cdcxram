#!/usr/bin/env bash
# Run the full pytest suite for CoinDCX Spot Lab.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pytest tests/ -v "$@"
