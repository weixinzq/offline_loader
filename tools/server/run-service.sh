#!/usr/bin/env bash
set -euo pipefail
umask 077
root=$(cd -- "$(dirname -- "$0")" && pwd -P)
mkdir -p "$root/logs" "$root/tmp" "$root/cache"
exec >> "$root/logs/service.log" 2>&1
"$root/.venv/bin/python" -B "$root/tools/server/manage.py" check-current
release=$(readlink -f -- "$root/current")
cd "$release"
export TMPDIR="$root/tmp" XDG_CACHE_HOME="$root/cache" PYTHONUTF8=1
unset AOLA_ADMIN_TOKEN
printf '\n[%s] Starting release=%s\n' "$(date -Is)" "$release"
exec "$root/.venv/bin/python" -B "$release/backend_main.py" \
  --host 127.0.0.1 --port 8765 --login-concurrency 44 \
  --data-dir "$root/data/service" --frontend-dir "$release/frontend/dist"
