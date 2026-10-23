#!/usr/bin/env bash
set -euo pipefail
umask 077
root=/root/workspace/offline_loader
test "$(realpath "$root")" = "$root"
cd "$root"
mkdir -p logs tmp cache
export TMPDIR="$root/tmp"
export XDG_CACHE_HOME="$root/cache"
export PYTHONUTF8=1
unset AOLA_ADMIN_TOKEN
exec >> "$root/logs/service.log" 2>&1
exec "$root/.venv/bin/python" -B "$root/backend_main.py" \
  --host 127.0.0.1 --port 8765 --max-online 500 --login-concurrency 20 \
  --data-dir "$root/data/service" --frontend-dir "$root/frontend/dist"
