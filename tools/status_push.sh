#!/bin/bash
# Every 5 min from root's cron on the VPS: the VPS half of the status page (tools/status_collect.py) to the Mini.
#   */5 * * * * bash /root/work/pinpin-apps/tools/status_push.sh >> /tmp/pinpin-status-push.log 2>&1
cd "$(dirname "$0")/.." || exit 1
# (docs come from the qws-ad worktree, fetched inside status_collect.py; the shared checkout is only read)
python3 tools/status_collect.py | ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-mini \
  'mkdir -p ~/pinpin-apps-data/status && cat > ~/pinpin-apps-data/status/vps.json.tmp && mv ~/pinpin-apps-data/status/vps.json.tmp ~/pinpin-apps-data/status/vps.json' \
  || echo "$(date -u +%FT%TZ) status push failed"
# the kanban board's git mirror (docs/captain/kanban.json + KANBAN.md in quest-wall-screen)
bash tools/kanban_mirror.sh
