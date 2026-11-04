#!/bin/bash
# The scratch house server (:57801) as a Mini LaunchAgent, its only owner (run from the VPS as root):
#   bash deploy/install-house-scratch.sh
# Stops any hand-started copy first (a second owner would fight over :57801), installs the plist, bootstraps it, checks.
set -e
cd "$(dirname "$0")/.."
M="ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-mini"
scp -q -o BatchMode=yes deploy/io.mr-pinpin.house-scratch.plist pinpin-mini:Library/LaunchAgents/io.mr-pinpin.house-scratch.plist
$M 'bash -s' <<'REMOTE'
set -e
launchctl bootout gui/$(id -u)/io.mr-pinpin.house-scratch 2>/dev/null || true
for p in $(lsof -nP -t -iTCP:57801 -sTCP:LISTEN 2>/dev/null); do echo "stopping hand-started $p: $(ps -o args= -p $p | cut -c1-120)"; kill $p; done
sleep 2
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.mr-pinpin.house-scratch.plist
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s -m2 127.0.0.1:57801/health >/dev/null && break; sleep 1; done
echo "owner: $(launchctl list | grep io.mr-pinpin.house-scratch)"
echo "listener: $(lsof -nP -iTCP:57801 -sTCP:LISTEN | awk 'NR==2{print $1, $2}') (one expected)"
curl -s -m3 127.0.0.1:57801/health | tr -d '\n '; echo
REMOTE
