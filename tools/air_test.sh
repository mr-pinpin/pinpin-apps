#!/bin/bash
# End-to-end on the laptop (run from the VPS, as root): a scratch virtual display (screen 47, through pinpin-displays),
# the weather app assigned to it, the runner (installed/updated as the LaunchAgent) opens its window there; we check the page connected (SSE client on the
# Mini), speak to it, capture the display region, then undo everything and run air_smoke.sh.
# Takes the shared bench lock and logs START/END in bench/activity.log (PINPIN_WHO=apps).
#   bash tools/air_test.sh [APP]
set -u
APP=${1:-weather}; ID=47
QWS=/root/work/quest-wall-screen; L=$QWS/bench/activity.log
MINI=http://192.168.0.50:57830
A="ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-air"
M="ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-mini"
api() { $M "curl -s -m10 $*"; }  # the Mini API, from the Mini itself (the VPS is not on the home LAN)
cd "$(dirname "$0")/.."
log() { echo "$(date -u +%FT%TZ) apps air_test.sh[$$] $*" >> $L; }
holder=$(cd $QWS/bench && python3 -c "import stress,sys; print(stress.lock_take(sys.argv[1]))" "apps air_test.sh $APP since $(date -u +%T)")
[ -n "$holder" ] && { echo "bench lock held: $holder"; exit 2; }
log "START runner test: scratch display $ID, app $APP, Chrome --app window (bench lock taken)"
cleanup() {
  api "-X POST -H Content-Type:application/json -d '{\"app\": null}' 127.0.0.1:57830/api/screens/$ID/app" >/dev/null
  sleep 5  # the runner closes the window itself once the screen has no app
  $A "pkill -f 'pinpin-apps/profiles/screen-$ID' ; sleep 1; curl -s -m40 -X POST '127.0.0.1:57710/remove?id=$ID' >/dev/null; kill \$(cat /tmp/pp-caf.pid 2>/dev/null) 2>/dev/null; tail -4 ~/.local/share/pinpin-apps/runner.log"
  (cd $QWS && bash bench/air_smoke.sh); smoke=$?
  (cd $QWS/bench && python3 -c "import stress; stress.lock_release()")
  log "END runner test: result=$result smoke=$([ $smoke = 0 ] && echo pass || echo FAIL) (bench lock released)"
}
result=fail; trap cleanup EXIT
$A "caffeinate -u -t 120 & echo \$! > /tmp/pp-caf.pid; curl -s -m40 -X POST '127.0.0.1:57710/displays?id=$ID&w=1920&h=1200&scale=1' >/dev/null; sleep 3; curl -s 127.0.0.1:57710/displays"; echo
api "-X POST -H Content-Type:application/json -d '{\"app\": \"$APP\"}' 127.0.0.1:57830/api/screens/$ID/app" || { echo "assign failed"; exit 1; }; echo
bash deploy/install-air.sh  # the runner must live in the GUI session (LaunchAgent): Chrome started from ssh cannot reach it
ok=
for i in $(seq 1 20); do
  c=$(api 127.0.0.1:57830/api/screens | python3 -c "import json,sys; print(next((r['clients'] for r in json.load(sys.stdin) if r['screen']==$ID), 0))" 2>/dev/null)
  [ "${c:-0}" -ge 1 ] && { ok=1; break; }; sleep 1
done
[ -z "$ok" ] && { echo "the page never connected"; exit 1; }
echo "page connected after ${i}s"
[ "$APP" = recipes ] && api "-X POST -H Content-Type:application/json -d '{\"text\": \"eggs, tomatoes and an onion\"}' 127.0.0.1:57830/api/screens/$ID/say" && echo
sleep 6
B=$($A "curl -s 127.0.0.1:57710/displays" | python3 -c "import json,sys; print(next(d['display'] for d in json.load(sys.stdin) if d['screen']==$ID))")
R=$($A "osascript -l JavaScript -e 'ObjC.import(\"CoreGraphics\"); var r=\$.CGDisplayBounds($B); [r.origin.x,r.origin.y,r.size.width,r.size.height].join(\",\")'")
$A "screencapture -x -R $R /tmp/pp-air-$ID.png 2>&1; ls -la /tmp/pp-air-$ID.png 2>&1; ps -o pid,args -p \$(pgrep -f 'profiles/screen-$ID' | head -1) | cut -c1-200"
mkdir -p /tmp/pp-air && scp -q -o BatchMode=yes pinpin-air:/tmp/pp-air-$ID.png /tmp/pp-air/ 2>/dev/null && echo "capture: /tmp/pp-air/pp-air-$ID.png"
result=pass
