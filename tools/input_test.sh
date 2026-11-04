#!/bin/bash
# CDP input end to end on the laptop (run from the VPS as root): scratch display 47, example.com opened on it, a
# click at the link's display u/v through the Mini API must navigate; then scroll, type and key on real pages.
# SHARED bench lock for the test, EXCLUSIVE for the air_smoke after the runner update.
#   [QWS_TOOLS=/root/work/qws-ad] bash tools/input_test.sh
set -u
ID=47; QWS=/root/work/quest-wall-screen; L=$QWS/bench/activity.log
T=${QWS_TOOLS:-/root/work/qws-ad}; BL="python3 $T/bench/benchlock.py"
A="ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-air"; M="ssh -o BatchMode=yes -o ConnectTimeout=10 pinpin-mini"
cd "$(dirname "$0")/.."
log() { echo "$(date -u +%FT%TZ) apps input_test.sh[$$] $*" >> $L; }
post() { $M "curl -s -m10 -X POST -H Content-Type:application/json -H \"Authorization: Bearer \$(cat ~/pinpin-apps-data/token 2>/dev/null)\" -d '$2' 127.0.0.1:57830/api/screens/$ID/$1"; }
ON_AIR='cd ~/.local/share/pinpin-apps && /opt/homebrew/bin/python3 -c'
href() { $A "$ON_AIR 'import apps_runner as r; c=r.CDP(r.cdp_port($ID)); print(c.call(\"Runtime.evaluate\", {\"expression\": \"location.href+\\\" \\\"+scrollY\", \"returnByValue\": True})[\"result\"][\"value\"])'"; }
wait_href() { for i in $(seq 1 20); do h=$(href 2>/dev/null); case "$h" in *"$1"*) echo "$h"; return 0;; esac; sleep 0.5; done; echo "timeout: $h"; return 1; }
open_url() { post open "{\"url\": \"$1\"}" >/dev/null; wait_href "$2"; }
tok=$($BL take shared --who apps --max 600) || { echo "no bench lock"; exit 2; }
log "START CDP input test on scratch display $ID (bench lock shared)"
result=fail
cleanup() {
  post open '{"url": null}' >/dev/null; post app '{"app": null}' >/dev/null; sleep 5
  $A "curl -s -m40 -X POST '127.0.0.1:57710/remove?id=$ID' >/dev/null; kill \$(cat /tmp/pp-caf.pid 2>/dev/null) 2>/dev/null"
  $BL release "$tok"
  (cd $T && $BL exclusive --who apps --max 300 -- bash bench/air_smoke.sh); smoke=$?
  log "END CDP input test: result=$result smoke=$([ $smoke = 0 ] && echo pass || echo FAIL) (bench lock released)"
}
trap cleanup EXIT
bash deploy/install-air.sh </dev/null
$A "caffeinate -u -t 180 >/dev/null 2>&1 & echo \$! > /tmp/pp-caf.pid; curl -s -m40 -X POST '127.0.0.1:57710/displays?id=$ID&w=1920&h=1200&scale=1' >/dev/null"
open_url https://example.com/ example.com || exit 1
sleep 2
# the link's centre as display u/v: page rect -> global point (window chrome on top) -> fraction of the display
B=$($A "curl -s 127.0.0.1:57710/displays" | python3 -c "import json,sys; print(next(d['display'] for d in json.load(sys.stdin) if d['screen']==$ID))")
R=$($A "osascript -l JavaScript -e 'ObjC.import(\"CoreGraphics\"); var r=\$.CGDisplayBounds($B); [r.origin.x,r.origin.y,r.size.width,r.size.height].join(\",\")'")
G=$($A "$ON_AIR 'import apps_runner as r, json; c=r.CDP(r.cdp_port($ID)); print(json.dumps(c.call(\"Runtime.evaluate\", {\"expression\": \"var b=document.querySelector(\\\"a\\\").getBoundingClientRect(); [b.left+b.width/2, b.top+b.height/2, screenX, screenY, outerWidth, outerHeight, innerWidth, innerHeight]\", \"returnByValue\": True})[\"result\"][\"value\"]))'")
UV=$(python3 -c "
dx,dy,dw,dh=map(float,'$R'.split(',')); cx,cy,sx,sy,ow,oh,iw,ih=$G
gx,gy=sx+(ow-iw)+cx, sy+(oh-ih)+cy; print(round((gx-dx)/dw*10000), round((gy-dy)/dh*10000))")
set -- $UV; echo "display $B $R; link at page $G -> display u,v $1,$2"
echo "click: $(post click "{\"u\": $1, \"v\": $2}")"
wait_href iana || exit 1
echo "PASS click navigates"
ev=$(post eval '{"js": "location.href + \" | \" + document.title"}'); echo "eval: $ev"
case "$ev" in *iana.org*) echo "PASS eval";; *) echo "eval failed"; exit 1;; esac
# scroll (display u/v = centre)
open_url https://en.wikipedia.org/wiki/Moscow wikipedia || exit 1
sleep 2; post scroll '{"u": 5000, "v": 5000, "dy": 900}' >/dev/null; sleep 1
y=$(href | awk '{print $2}'); echo "scrollY after dy=900: $y"; [ "${y%.*}" -gt 300 ] || exit 1
echo "PASS scroll"
# type + key: focus the search box by clicking it in viewport units, type, Enter
open_url "https://en.wikipedia.org/wiki/Special:Search" Special:Search || exit 1
sleep 2
V=$($A "$ON_AIR 'import apps_runner as r, json; c=r.CDP(r.cdp_port($ID)); print(json.dumps(c.call(\"Runtime.evaluate\", {\"expression\": \"var b=document.querySelector(\\\"input[name=search]\\\").getBoundingClientRect(); [Math.round((b.left+b.width/2)/innerWidth*10000), Math.round((b.top+b.height/2)/innerHeight*10000)]\", \"returnByValue\": True})[\"result\"][\"value\"]))'")
set -- $(echo "$V" | tr -d '[],'); echo "search box viewport u,v $1,$2"
echo "click: $(post click "{\"u\": $1, \"v\": $2, \"space\": \"viewport\"}")"
post type '{"text": "pinpin wall screens"}' >/dev/null
post key '{"combo": "Enter"}' >/dev/null
wait_href "search=pinpin" || exit 1
echo "PASS type + Enter"
result=pass
