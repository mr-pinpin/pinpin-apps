#!/bin/bash
# The hostile-app test (run on the Mini, or from the VPS: ssh pinpin-mini 'bash -s' < tools/sandbox_probe.sh):
# apps/zz-probe tries localStorage, cookies, the parent page, the API, the house server (loopback and LAN), beacons,
# WebSockets, the proxy to LAN/unlisted hosts, ask without permission and top navigation. Every probe must be
# BLOCKED. It renders in the same sandbox as a real screen (host page + iframe) but with the network open
# (--no-sandbox-net), so only the app sandbox itself stops it.
cd ~/pinpin-apps || exit 1
# (render_check's own exit code counts the probes' refused requests and CSP reports as errors: expected here, so the
# verdict is the page's "ALL BLOCKED" line alone)
out=$(timeout 120 python3 tools/render_check.py http://127.0.0.1:57830/app/zz-probe/ --wait 25 --no-sandbox-net --expect "ALL BLOCKED")
echo "$out" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['text'])"
echo "$out" | python3 -c "import json,sys; sys.exit(1 if json.load(sys.stdin)['missing'] else 0)"; rc=$?
[ $rc = 0 ] && echo "SANDBOX PROBE PASS" || echo "SANDBOX PROBE FAIL"
exit $rc
