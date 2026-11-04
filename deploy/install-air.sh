#!/bin/bash
# Install or update the apps runner on the laptop (run from the VPS as root, inside a bench-lock slot):
#   bash deploy/install-air.sh
# Copies runner/apps_runner.py to ~/.local/share/pinpin-apps and (re)loads the LaunchAgent io.mr-pinpin.apps-runner.
# The runner only opens windows for screens that have an app in house.json, so installing it changes nothing on screen
# until an app is assigned. No rm on the laptop: old files are overwritten in place.
set -e
cd "$(dirname "$0")/.."
A="ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-air"
$A 'mkdir -p ~/.local/share/pinpin-apps ~/Library/LaunchAgents'
scp -q -o BatchMode=yes runner/apps_runner.py pinpin-air:.local/share/pinpin-apps/
scp -q -o BatchMode=yes deploy/io.mr-pinpin.apps-runner.plist pinpin-air:Library/LaunchAgents/
$A 'sed -i "" "s#__HOME__#$HOME#g" ~/Library/LaunchAgents/io.mr-pinpin.apps-runner.plist;
    launchctl bootout gui/$(id -u)/io.mr-pinpin.apps-runner 2>/dev/null; sleep 1;
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.mr-pinpin.apps-runner.plist; sleep 3;
    tail -3 ~/.local/share/pinpin-apps/runner.log'
