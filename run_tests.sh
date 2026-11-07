#!/bin/bash
# Offline tests (no devices, no network). Run before every deploy; deploy only on "all offline tests passed".
# Memory- and time-capped: the VPS is shared (7 GB).
cd "$(dirname "$0")" || exit 1
ulimit -v 2000000
fail=0
for t in server/test_apps.py runner/test_runner.py tools/test_status_collect.py; do
  if timeout 180 python3 "$t" > /tmp/pinpin-apps-test.$$ 2>&1; then echo "ok   $t"; else echo "FAIL $t"; grep -v '"\(GET\|PUT\) /house' /tmp/pinpin-apps-test.$$ | tail -30; fail=1; fi
done
rm -f /tmp/pinpin-apps-test.$$
for f in apps/*/manifest.json; do python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$f" || { echo "FAIL $f"; fail=1; }; done
[ $fail = 0 ] && echo "all offline tests passed"
exit $fail
