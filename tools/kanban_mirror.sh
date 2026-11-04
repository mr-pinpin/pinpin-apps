#!/bin/bash
# The board's git history: copies the Mini's <data>/kanban.json to quest-wall-screen docs/captain/kanban.json and
# regenerates docs/captain/KANBAN.md from it, one commit when it changed. Runs from tools/status_push.sh (every 5 min,
# root on the VPS) in the qws-ad worktree (never the shared checkout).
cd "$(dirname "$0")/.." || exit 1
APPS=$PWD
WT=${QWS_DOCS:-/root/work/qws-ad}
TMP=$(mktemp -d) || exit 1
trap 'rm -rf "$TMP"' EXIT
ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-mini 'cat ~/pinpin-apps-data/kanban.json' > "$TMP/k.json" 2>/dev/null || exit 0
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("cards") else 1)' "$TMP/k.json" 2>/dev/null || exit 0
python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import json, kanban; print(kanban.markdown(json.load(open(sys.argv[2]))))' \
  "$APPS/server" "$TMP/k.json" > "$TMP/KANBAN.md" || exit 1
G=(git -C "$WT" -c user.name='Mr. PinPin' -c user.email=miguelemosreverte@gmail.com)
for attempt in 1 2 3; do
  "${G[@]}" fetch -q origin && "${G[@]}" checkout -q --detach origin/main || exit 1
  cp "$TMP/k.json" "$WT/docs/captain/kanban.json"
  cp "$TMP/KANBAN.md" "$WT/docs/captain/KANBAN.md"
  "${G[@]}" add docs/captain/kanban.json docs/captain/KANBAN.md
  "${G[@]}" diff --cached --quiet && exit 0
  REV=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["rev"])' "$TMP/k.json")
  "${G[@]}" commit -q -m "captain: kanban mirror rev $REV" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" || exit 1
  "${G[@]}" push -q origin HEAD:main && { echo "$(date -u +%FT%TZ) kanban mirror rev $REV pushed"; exit 0; }
  sleep 3
done
echo "$(date -u +%FT%TZ) kanban mirror: push failed 3 times"
