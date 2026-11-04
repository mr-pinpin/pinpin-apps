#!/usr/bin/env python3
"""Nightly end-to-end health suite (VPS root cron, 01:40 UTC, after work-42's assistant eval at 01:17). One /status
check line per area ("nightly <area>"), plus a summary line. Read-only on everything live:
  apps       the 6 apps rendered in headless Chrome on the Mini (no JS errors, expected text), plus the hostile-app
             sandbox probe (ALL BLOCKED)
  house      a round trip on the SCRATCH house (:57801): get, put, a stale put answered 409, tag, rollback to the tag
  kanban     add, move, delete a card on the SCRATCH board (?board=scratch)
  backups    the newest data snapshot is under 2 h old, and restoring it into a scratch dir on the Mini gives exactly
             the snapshot's files (sha256 per file)
  network    the laptop on RT-5WiFi-EDA0, Tailscale up with the exit node, colorsync under 40%, the headset's Wi-Fi,
             the Mini's Tailscale (the /status Network rows)
  reception  the receptionist's /health on the VPS (no message is sent)
    python3 tools/nightly_health.py [--only apps,house,...] [--dry]   (--dry prints instead of posting)
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'pinpin-mini']
BACKUP = os.environ.get('PINPIN_DATA_BACKUP', '/root/backups/pinpin-apps-data')
APPS = [('weather', ['--wait', '5'], ''), ('recipes', ['--wait', '2.5', '--fail', '*/api/ask*', '--say', 'eggs and tomatoes'], ''),
        ('shopping', ['--wait', '2', '--say', 'add milk'], 'milk'), ('clock', ['--wait', '3'], ''),
        ('notes', ['--wait', '2', '--say', 'note: nightly check'], 'Nightly check'), ('timer', ['--wait', '2', '--say', '2 minutes'], '')]


def mini(script, timeout=300):
    """Run a bash script on the Mini; -> (rc, stdout+stderr)."""
    try:
        p = subprocess.run(SSH + ['bash -s'], input='export LC_ALL=C LANG=C\n' + script, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, 'timed out'


def api(method, path, body=None, base='http://127.0.0.1:57830', headers=''):
    """Call a Mini HTTP API from the Mini itself (the VPS is not on the home LAN); -> (status, parsed or text)."""
    data = json.dumps(body) if body is not None else ''
    script = (f'curl -s -m20 -o /tmp/pp-nh.out -w "%{{http_code}}" -X {method} '
              f'-H "Authorization: Bearer $(cat ~/pinpin-apps-data/token)" -H "Content-Type: application/json" {headers} '
              + (f"--data-binary @- " if body is not None else '') + f"'{base}{path}'" + '; echo; cat /tmp/pp-nh.out')
    try:
        p = subprocess.run(SSH + ['LC_ALL=C ' + script], input=data, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return 0, 'timed out'
    code, _, out = p.stdout.partition('\n')
    try:
        return int(code or 0), json.loads(out)
    except ValueError:
        return int(code or 0) if code.isdigit() else 0, out


# ---- the checks: each -> (ok, one line)

def check_apps():
    lines, bad = [], []
    for app, args, want in APPS:
        cmd = ['timeout', '120', 'python3', 'tools/render_check.py', f'http://127.0.0.1:57830/app/{app}/', *args] + (['--expect', want] if want else [])
        rc, out = mini('cd ~/pinpin-apps && ' + ' '.join(f"'{c}'" for c in cmd))
        try:
            d = json.loads(out[out.index('{'):])
            ok = not d['errors'] and not d['missing']
            why = '; '.join([*(str(e)[:80] for e in d['errors'][:2]), *(f'missing "{m}"' for m in d['missing'])])
        except (ValueError, KeyError):
            ok, why = False, out[-120:]
        (lines if ok else bad).append(app if ok else f'{app} ({why})')
    rc, out = mini(open(os.path.join(HERE, 'sandbox_probe.sh')).read(), timeout=200)
    probe = rc == 0 and 'ALL BLOCKED' in out
    text = f'{len(lines)}/{len(APPS)} apps render clean' + (f'; FAILED: {", ".join(bad)}' if bad else '') + \
           ('; sandbox probe ALL BLOCKED' if probe else f'; sandbox probe FAILED: {out.splitlines()[-1][:120] if out else rc}')
    return not bad and probe, text


def check_house():
    base = 'http://127.0.0.1:57801'
    script = r'''
set -u; B=%s; cd /tmp
curl -s -m10 -D h0 -o d0.json $B/house || { echo "FAIL scratch house not answering"; exit 1; }
R=$(awk 'tolower($1)=="x-house-rev:"{print $2+0}' h0)
sha() { shasum -a 256 "$1" | cut -d" " -f1; }
put() { curl -s -m10 -o "$3" -w "%%{http_code}" -X PUT -H "If-Match: $1" -H "X-House-SHA256: $(sha $2)" -H "Content-Type: application/json" --data-binary @"$2" $B/house; }
python3 -c "import json; d=json.load(open('d0.json')); d['by']='test'; d.setdefault('settings', {})['nightly']='$(date -u +%%FT%%TZ)'; json.dump(d, open('d1.json','w'))"
c1=$(put $R d1.json p1.out); [ "$c1" = 200 ] || { echo "FAIL put: $c1 $(tr -s ' \\n' ' ' < p1.out | head -c 200)"; exit 1; }
c2=$(put $R d1.json p2.out); [ "$c2" = 409 ] || { echo "FAIL stale put answered $c2, not 409"; exit 1; }
tc=$(curl -s -m10 -o t.out -w "%%{http_code}" -X POST "$B/house/tag?rev=$R&name=nightly-$(date -u +%%Y%%m%%d%%H%%M)")
R1=$((R+1))
rc=$(curl -s -m10 -o r.out -w "%%{http_code}" -X POST -H "If-Match: $R1" "$B/house/rollback?rev=$R")
curl -s -m10 -o d2.json $B/house
same=$(python3 -c "import json; a,b=json.load(open('d0.json')),json.load(open('d2.json')); [x.pop(k,None) for x in (a,b) for k in ('rev','updated','by')]; print('yes' if a==b else 'no')")
echo "rev $R -> put $c1 -> stale put $c2 -> tag $tc -> rollback $rc -> content restored: $same"
[ "$rc" = 200 ] && [ "$same" = yes ] || exit 1
[ "$tc" = 200 ] || exit 2
''' % base
    rc, out = mini(script, timeout=90)
    line = out.splitlines()[-1] if out else f'rc {rc}'
    if rc == 2:
        return False, line + ' (the scratch house server has no /house/tag: it runs older code than the live :57800)'
    return rc == 0, line


def check_kanban():
    st, c = api('POST', '/api/kanban?board=scratch', {'title': 'nightly test card', 'by': 'nightly'})
    if st != 201:
        return False, f'add answered {st}: {c}'
    k = c['id']
    st2, m = api('POST', f'/api/kanban/{k}/move?board=scratch', {'column': 'done', 'by': 'nightly'})
    st3, d = api('POST', f'/api/kanban/{k}/delete?board=scratch', {'by': 'nightly'})
    st4, _ = api('GET', f'/api/kanban/{k}?board=scratch')
    st5, live = api('GET', '/api/kanban')
    ok = st2 == 200 and isinstance(m, dict) and m.get('column') == 'done' and st3 == 200 and st4 == 404 and st5 == 200
    return ok, (f'scratch board: add {k} 201, move {st2}, delete {st3}, gone {st4 == 404}; live board readable '
                f'({len(live.get("cards", [])) if isinstance(live, dict) else "?"} cards, untouched)')


def check_backups():
    try:
        when = int(subprocess.run(['git', '-C', BACKUP, 'log', '-1', '--format=%ct'], capture_output=True, text=True, timeout=20).stdout)
    except ValueError:
        return False, f'no snapshot in {BACKUP}'
    age = (time.time() - when) / 3600
    tar = subprocess.run(['git', '-C', BACKUP, 'archive', '--format=tar', 'HEAD'], capture_output=True, timeout=60).stdout
    want = {}
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        for m in t.getmembers():
            if m.isfile():
                want[m.name] = hashlib.sha256(t.extractfile(m).read()).hexdigest()
    into = f'/tmp/pp-nightly-restore-{os.getpid()}'
    p = subprocess.run(['bash', os.path.join(HERE, 'data_restore.sh'), 'HEAD', '--into', into], capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        return False, f'snapshot {age:.1f} h old; restore into a scratch dir failed: {(p.stderr or p.stdout).strip()[-150:]}'
    rc, out = mini(f'cd {into} && find . -type f | sed "s|^./||" | while read -r f; do printf "%s %s\\n" "$(shasum -a 256 "$f" | cut -d" " -f1)" "$f"; done; rm -rf {into}', timeout=120)
    got = {}
    for line in out.splitlines():
        h, _, f = line.partition(' ')
        if len(h) == 64:
            got[f] = h
    diff = sorted(f for f in set(want) | set(got) if want.get(f) != got.get(f))
    ok = age < 2 and not diff
    return ok, (f'newest snapshot {age:.1f} h old' + ('' if age < 2 else ' (OVER 2 h)') +
                f'; restored {len(got)} files into a scratch dir: ' + ('identical to the snapshot' if not diff else f'{len(diff)} differ: {", ".join(diff[:3])}'))


def check_network():
    import status_collect
    a = status_collect.air_network()
    st, s = api('GET', '/api/status')
    n = s.get('network', {}) if isinstance(s, dict) else {}
    ex = n.get('expect') or {'air_ssid': ['RT-5WiFi-EDA0'], 'headset_ssid': []}
    probs, facts = [], []
    if not a.get('reachable'):
        probs.append(f'laptop not reachable over Tailscale ({a.get("error", "")[:60]})')
    else:
        facts.append(f'laptop {a.get("ip")} on {a.get("ssid")}')
        if a.get('ssid') not in ex['air_ssid']:
            probs.append(f'laptop Wi-Fi {a.get("ssid")} (should be {"/".join(ex["air_ssid"])})')
        t = a.get('tailscale') or {}
        if not t.get('up'):
            probs.append('laptop Tailscale down')
        if not (t.get('exit_node') and t.get('exit_online')):
            probs.append('laptop exit node off')
        cs = a.get('colorsync') or {}
        facts.append(f'colorsync {cs.get("cpu", "?")}% ({cs.get("req_per_min", "?")}/min)')
        if (cs.get('cpu') or 0) > 40:
            probs.append(f'colorsync {cs.get("cpu")}% of a core (K24)')
    hs = n.get('headset_ssid')
    facts.append(f'headset on {hs or "?"}')
    if ex.get('headset_ssid') and hs and hs not in ex['headset_ssid']:
        probs.append(f'headset Wi-Fi {hs}')
    if not (n.get('mini_tailscale') or {}).get('up'):
        probs.append('Mini Tailscale down')
    return not probs, ('; '.join(probs) + ' | ' if probs else 'all good: ') + ', '.join(facts)


def check_reception():
    try:
        with urllib.request.urlopen('http://127.0.0.1:57850/health', timeout=10) as r:
            d = json.loads(r.read())
        return bool(d.get('ok')), f'receptionist up ({d.get("sessions", "?")} sessions, default {d.get("default", "?")})'
    except Exception as e:
        return False, f'receptionist /health: {e}'


CHECKS = {'apps': check_apps, 'house': check_house, 'kanban': check_kanban, 'backups': check_backups,
          'network': check_network, 'reception': check_reception}


def post(component, ok, text, dry):
    if dry:
        print(f'{"PASS" if ok else "FAIL"} {component}: {text}')
        return
    st, d = api('POST', '/api/status/component', {'component': component, 'ok': ok, 'text': text})
    print(f'{"PASS" if ok else "FAIL"} {component}: {text}' + ('' if st == 200 else f'  (status post: {st})'))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--only', default='')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    names = [x for x in a.only.split(',') if x] or list(CHECKS)
    t0, results = time.time(), {}
    for name in names:
        try:
            ok, text = CHECKS[name]()
        except Exception as e:
            ok, text = False, f'the check itself crashed: {type(e).__name__}: {e}'[:200]
        results[name] = ok
        post(f'nightly {name}', ok, text, a.dry)
    if not a.only:
        failed = [n for n, ok in results.items() if not ok]
        post('nightly', not failed, f'{len(results) - len(failed)}/{len(results)} areas pass at {time.strftime("%H:%M", time.gmtime())} UTC'
             + (f'; failing: {", ".join(failed)}' if failed else '') + f' ({time.time() - t0:.0f} s)', a.dry)
    sys.exit(0 if all(results.values()) else 1)


if __name__ == '__main__':
    main()
