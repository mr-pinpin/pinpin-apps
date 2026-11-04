#!/bin/bash
# The read-only cursor watcher on the Mini (tools/cursor_watch.py, from the Mini's pinpin-apps checkout):
#   bash deploy/install-cursor-watch.sh        # log: ~/pinpin-apps-data/cursor-watch.log on the Mini
set -e
cd "$(dirname "$0")/.."
bash tools/sync.sh
scp -q -o BatchMode=yes deploy/io.mr-pinpin.cursor-watch.plist pinpin-mini:Library/LaunchAgents/
ssh -o BatchMode=yes pinpin-mini 'sed -i "" "s#__HOME__#$HOME#g" ~/Library/LaunchAgents/io.mr-pinpin.cursor-watch.plist;
  launchctl bootout gui/$(id -u)/io.mr-pinpin.cursor-watch 2>/dev/null; sleep 1;
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.mr-pinpin.cursor-watch.plist; sleep 3;
  tail -2 ~/pinpin-apps-data/cursor-watch.log; cat ~/pinpin-apps-data/cursor-watch.err 2>/dev/null | tail -3'
