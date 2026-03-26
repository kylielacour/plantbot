#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

set -a
source .env
set +a

PYTHON="./venv/bin/python"

echo "=== Sync completed waterings ==="
"$PYTHON" sync_completed_watering_tasks.py

echo "=== Create new watering tasks ==="
"$PYTHON" create_watering_tasks.py
