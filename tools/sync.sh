#!/bin/bash
# VPS checkout <-> the Mini's repo (where the assistant's commits land) <-> GitHub (mr-pinpin/pinpin-apps).
# Rebases local work on top of the Mini's commits, pushes to the Mini (its worktree updates in place), then GitHub.
# Safe from cron:   */10 * * * * cd /root/work/pinpin-apps && bash tools/sync.sh >> /tmp/pinpin-apps-sync.log 2>&1
cd "$(dirname "$0")/.." || exit 1
exec 9>/tmp/pinpin-apps-sync.lock; flock -n 9 || { echo "sync already running"; exit 0; }
git fetch -q mini main 2>/dev/null && { git rebase -q --autostash mini/main || { git rebase --abort; echo "sync: rebase conflict, not pushed"; exit 1; }; }
git push -q mini HEAD:main || { echo "sync: push to Mini refused"; exit 1; }
git push -q origin HEAD:main && echo "$(date -u +%FT%TZ) synced $(git rev-parse --short HEAD)"
