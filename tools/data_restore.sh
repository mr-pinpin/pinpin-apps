#!/bin/bash
# Restore the apps' data on the Mini from the VPS backup repo (tools/data_backup.sh).
#   bash tools/data_restore.sh [COMMIT|HEAD] [--into DIR]
#     default: put that snapshot back into ~/pinpin-apps-data on the Mini. Each file is written beside its target and
#              renamed over it (atomic per file); files the snapshot lacks are left alone. A fresh backup is taken first,
#              so a restore can itself be undone.
#     --into DIR: restore into DIR on the Mini instead (a scratch check; nothing live is touched).
set -e -o pipefail
REPO=${PINPIN_DATA_BACKUP:-/root/backups/pinpin-apps-data}
REV=${1:-HEAD}; [ "${1:-}" = "--into" ] && REV=HEAD
INTO=""; [ "${2:-}" = "--into" ] && INTO=$3; [ "${1:-}" = "--into" ] && INTO=$2
M="ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-mini"
git -C "$REPO" rev-parse --verify -q "$REV^{commit}" >/dev/null || { echo "no snapshot $REV in $REPO"; exit 1; }
if [ -z "$INTO" ]; then bash "$(dirname "$0")/data_backup.sh"; fi
t=$(mktemp); trap 'rm -f "$t"' EXIT
git -C "$REPO" archive --format=tar "$REV" > "$t"
scp -q -o BatchMode=yes "$t" pinpin-mini:/tmp/pp-restore.tar
$M "bash -s -- '${INTO}'" <<'REMOTE'
set -e
into="$1"; t=$(mktemp -d)
tar xf /tmp/pp-restore.tar -C "$t"; rm -f /tmp/pp-restore.tar
if [ -n "$into" ]; then mkdir -p "$into" && cp -R "$t"/. "$into"/ && echo "restored into $into"; rm -rf "$t"; exit 0; fi
cd "$t"
find . -type f | while read f; do
  dst=~/pinpin-apps-data/"${f#./}"; mkdir -p "$(dirname "$dst")"
  cp "$f" "$dst.restore-tmp" && mv "$dst.restore-tmp" "$dst"
done
echo "restored $(find . -type f | wc -l | tr -d ' ') files into ~/pinpin-apps-data"; rm -rf "$t"
# open pages hold the old copy in memory: reload every screen with an app so none of them saves it back
T=$(cat ~/pinpin-apps-data/token 2>/dev/null)
for sc in $(curl -s 127.0.0.1:57830/api/screens | python3 -c "import json,sys; print(' '.join(str(r['screen']) for r in json.load(sys.stdin) if r.get('app')))"); do
  curl -s -m5 -X POST -H Content-Type:application/json -H "Authorization: Bearer $T" -d '{}' 127.0.0.1:57830/api/screens/$sc/reload >/dev/null
done
REMOTE
