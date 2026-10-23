#!/usr/bin/env bash
set -euo pipefail
umask 077
root=/root/workspace/offline_loader
unit=aola-cloud-battles-root.service
test "$(id -un)" = root
test "$(realpath "$root")" = "$root"
cd "$root"
owned_unit() {
  if test "$(systemctl show "$unit" -p LoadState --value)" != not-found; then
    case "$(systemctl show "$unit" -p ExecStart --value)" in
      *"$root/run_cloud_workflows.py"*) ;;
      *) printf 'Refusing to manage an unrelated unit: %s\n' "$unit" >&2; exit 1 ;;
    esac
  fi
}
action=${1:-status}
test "$#" -eq 0 || shift
owned_unit
case "$action" in
  start)
    if systemctl is-active --quiet "$unit"; then
      printf 'Battle workflow is already active\n'
      exit 0
    fi
    "$root/manage.sh" start
    mkdir -p logs
    systemd-run --unit="$unit" --collect --service-type=simple \
      --description='Offline Loader independent battle workflows' \
      --property="WorkingDirectory=$root" \
      --property=BindsTo=aola-offline-loader-root.service \
      --property=After=aola-offline-loader-root.service \
      --property=KillSignal=SIGINT --property=KillMode=mixed \
      --property=TimeoutStopSec=65 --property=Restart=no --property=UMask=0077 \
      --property="StandardOutput=append:$root/logs/cloud-battles-console.log" \
      --property="StandardError=append:$root/logs/cloud-battles-console.log" \
      "$root/.venv/bin/python" -B -u "$root/run_cloud_workflows.py" "$@"
    ;;
  stop)
    if test "$(systemctl show "$unit" -p LoadState --value)" != not-found; then
      systemctl stop "$unit"
    fi
    "$root/.venv/bin/python" -B "$root/run_cloud_workflows.py" --stop-owned
    printf 'Battle workflow stopped; ready sessions are retained\n'
    ;;
  status)
    systemctl show "$unit" -p LoadState -p ActiveState -p SubState -p MainPID -p ExecMainStatus
    "$root/.venv/bin/python" -B - <<'PY'
import collections
import json
from pathlib import Path
directories = sorted(Path('logs/cloud-battles').glob('*/status.json'))
if directories:
    path = directories[-1]
    status = json.loads(path.read_text())
    print('Latest run:', path.parent)
    print('Workflow:', status['state'])
    rows = [row for task in status['tasks'].values() if task['kind'] == 'execute' for row in task['accounts']]
    print('Battle accounts:', dict(collections.Counter(row['state'] for row in rows)))
PY
    ;;
  logs) tail -n 100 "$root/logs/cloud-battles-console.log" ;;
  *) printf 'Usage: %s {start [--exclude-account-id ID ...]|status|logs|stop}\n' "$0" >&2; exit 2 ;;
esac
