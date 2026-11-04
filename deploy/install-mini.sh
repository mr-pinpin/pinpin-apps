#!/bin/bash
# Install or update the apps server on the Mini (run from the VPS checkout):   bash deploy/install-mini.sh
# The Mini's ~/pinpin-apps is a git repo that the assistant's commits land in. Code arrives by git push
# (receive.denyCurrentBranch=updateInstead), never by copying over it. Data (~/pinpin-apps-data) is never touched.
set -e
cd "$(dirname "$0")/.."
M="ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-mini"
if ! $M 'test -d ~/pinpin-apps/.git'; then
  $M 'git init -q -b main ~/pinpin-apps && git -C ~/pinpin-apps config receive.denyCurrentBranch updateInstead'
fi
git remote get-url mini >/dev/null 2>&1 || git remote add mini pinpin-mini:pinpin-apps
bash tools/sync.sh
$M 'mkdir -p ~/pinpin-apps-data ~/Library/LaunchAgents'
scp -q -o BatchMode=yes deploy/io.mr-pinpin.apps.plist pinpin-mini:Library/LaunchAgents/
$M 'sed -i "" "s#__HOME__#$HOME#g" ~/Library/LaunchAgents/io.mr-pinpin.apps.plist;
    launchctl bootout gui/$(id -u)/io.mr-pinpin.apps 2>/dev/null; sleep 1;
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.mr-pinpin.apps.plist'
for i in $(seq 1 10); do
  h=$($M 'curl -sf -m3 127.0.0.1:57830/health' 2>/dev/null) && { echo "apps server up: $h"; exit 0; }
  sleep 1
done
echo "apps server did not come up:"; $M 'tail -20 ~/pinpin-apps-data/server.log'; exit 1
