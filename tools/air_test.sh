#!/bin/bash
# End-to-end on the laptop (run from the VPS, as root): a scratch virtual display (screen 47, through pinpin-displays),
# the weather app assigned to it, the runner (installed/updated as the LaunchAgent) opens its window there; we check the page connected (SSE client on the
# Mini), speak to it, capture the display region, then undo everything and run air_smoke.sh.
# Bench lock (quest-wall-screen bench/benchlock.py, AGENTS.md): the test holds it SHARED (LOCK=exclusive while an
# old-style benchmark campaign runs), air_smoke runs afterwards under EXCLUSIVE. START/END go to bench/activity.log.
#   [LOCK=shared|exclusive] [QWS_TOOLS=/root/work/qws-ad] bash tools/air_test.sh [APP]
set -u
APP=${1:-weather}; ID=47
QWS=/root/work/quest-wall-screen; L=$QWS/bench/activity.log   # the shared checkout: runtime files only
T=${QWS_TOOLS:-/root/work/qws-ad}; LOCK=${LOCK:-shared}           # a checkout with bench/benchlock.py (bdc73dd+)
BL="python3 $T/bench/benchlock.py"
MINI=http://192.168.0.50:57830
A="ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-air"
M="ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-mini"
api() { $M "curl -s -m10 -H \"Authorization: Bearer \$(cat ~/pinpin-apps-data/token 2>/dev/null)\" $*"; }  # the Mini API, from the Mini itself (the VPS is not on the home LAN)
cd "$(dirname "$0")/.."
log() { echo "$(date -u +%FT%TZ) apps air_test.sh[$$] $*" >> $L; }
tok=$($BL take $LOCK --who apps --max 600) || { echo "no bench lock"; exit 2; }
log "START runner test: scratch display $ID, app $APP, Chrome --app window (bench lock $LOCK)"
cleanup() {
  api "-X POST -H Content-Type:application/json -d '{\"app\": null}' 127.0.0.1:57830/api/screens/$ID/app" >/dev/null
  sleep 5  # the runner closes the window itself once the screen has no app
  $A "pkill -f 'pinpin-apps/profiles/screen-$ID' ; sleep 1; curl -s -m40 -X POST '127.0.0.1:57710/remove?id=$ID' >/dev/null; kill \$(cat /tmp/pp-caf.pid 2>/dev/null) 2>/dev/null; tail -4 ~/.local/share/pinpin-apps/runner.log"
  $BL release "$tok"
  (cd $T && $BL exclusive --who apps --max 300 -- bash bench/air_smoke.sh); smoke=$?
  log "END runner test: result=$result smoke=$([ $smoke = 0 ] && echo pass || echo FAIL) (bench lock released)"
}
result=fail; trap cleanup EXIT
$A "caffeinate -u -t 120 >/dev/null 2>&1 & echo \$! > /tmp/pp-caf.pid; curl -s -m40 -X POST '127.0.0.1:57710/displays?id=$ID&w=1920&h=1200&scale=1' >/dev/null; sleep 3; curl -s 127.0.0.1:57710/displays"; echo
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
W=$($A "cd ~/.local/share/pinpin-apps && /opt/homebrew/bin/python3 -c 'import apps_runner as r; print(\",\".join(map(str, r.window_bounds($ID))))'")
echo "display $B bounds $R; window $W"
python3 - "$R" "$W" <<'PY' || exit 1
import sys
dx, dy, dw, dh = map(float, sys.argv[1].split(',')); wx, wy, ww, wh = map(float, sys.argv[2].split(','))
inside = dx - 1 <= wx and wx + ww <= dx + dw + 1 and dy - 1 <= wy and wy + wh <= dy + dh + 1
big = ww * wh >= 0.8 * dw * dh
print('window on its display:', inside, '- covers >= 80%:', big); sys.exit(0 if inside and big else 1)
PY
# the assistant's open_url path: navigate the same window, then back to the app
seq=$(api "-X POST -H Content-Type:application/json -d '{\"url\": \"https://example.com/\"}' 127.0.0.1:57830/api/screens/$ID/open" | python3 -c "import json,sys; print(json.load(sys.stdin)['seq'])")
ok=
for i in $(seq 1 15); do
  sh=$(api 127.0.0.1:57830/api/screens | python3 -c "import json,sys; print(next((r['shown_seq'] for r in json.load(sys.stdin) if r['screen']==$ID), 0))" 2>/dev/null)
  [ "$sh" = "$seq" ] && { ok=1; break; }; sleep 1
done
[ -z "$ok" ] && { echo "open url: never shown"; exit 1; }
sleep 2
href=$($A "cd ~/.local/share/pinpin-apps && /opt/homebrew/bin/python3 -c 'import apps_runner as r; c=r.CDP(r.cdp_port($ID)); print(c.call(\"Runtime.evaluate\", {\"expression\": \"location.href\", \"returnByValue\": True})[\"result\"][\"value\"])'")
echo "open url shown after ${i}s: $href"; [ "$href" = "https://example.com/" ] || exit 1
api "-X POST -H Content-Type:application/json -d '{\"url\": null}' 127.0.0.1:57830/api/screens/$ID/open" >/dev/null; sleep 5
c=$(api 127.0.0.1:57830/api/screens | python3 -c "import json,sys; print(next((r['clients'] for r in json.load(sys.stdin) if r['screen']==$ID), 0))")
echo "back to the app: clients=$c"; [ "${c:-0}" -ge 1 ] || exit 1
result=pass
