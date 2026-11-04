#!/bin/bash
# Every 5 min from root's cron on the VPS: the VPS half of the status page (tools/status_collect.py) to the Mini.
#   */5 * * * * bash /root/work/pinpin-apps/tools/status_push.sh >> /tmp/pinpin-status-push.log 2>&1
cd "$(dirname "$0")/.." || exit 1
cd /root/work/quest-wall-screen && git pull -q --rebase --autostash 2>/dev/null; cd - >/dev/null
python3 tools/status_collect.py | ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-mini \
  'mkdir -p ~/pinpin-apps-data/status && cat > ~/pinpin-apps-data/status/vps.json.tmp && mv ~/pinpin-apps-data/status/vps.json.tmp ~/pinpin-apps-data/status/vps.json' \
  || echo "$(date -u +%FT%TZ) status push failed"
