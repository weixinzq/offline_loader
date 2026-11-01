#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "$0")" && pwd -P)
exec "$root/.venv/bin/python" -B "$root/tools/server/manage.py" "$@"
