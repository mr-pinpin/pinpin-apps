#!/usr/bin/env python3
"""The VPS half of the morning status page: roadmap, benchmark verdicts, the activity log and the headset owner, from
the quest-wall-screen checkout, as one JSON document on stdout. tools/status_push.sh sends it to the Mini, where the
apps server adds the headset (adb), screens and the house plan and serves /status.   python3 tools/status_collect.py"""
import datetime
import glob
import json
import os
import re
import subprocess
import sys

QWS = os.environ.get('QWS', '/root/work/quest-wall-screen')         # the shared checkout: runtime files (activity.log)
DOCS = os.environ.get('QWS_DOCS', '/root/work/qws-ad')                # my worktree, fetched fresh: roadmap, log, flags
APPS = os.environ.get('APPS_REPO', '/root/work/pinpin-apps')
SINCE = os.environ.get('OVERNIGHT_SINCE', '2026-11-04T01:30:00Z')
VERDICT = re.compile(r'\b(KEEP|DROP|INCONCLUSIVE|PASS|FAIL|verdict|winner|better|worse)\b', re.I)


def read(path, default=''):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return default


def bench():
    """Finished campaigns first (bench/campaign.py writes campaign-<block>-<date>.md), then other recent verdict notes."""
    base = DOCS if os.path.isdir(DOCS) else QWS
    files = sorted(glob.glob(os.path.join(base, 'bench/results/*.md')), key=os.path.getmtime, reverse=True)
    files = [f for f in files if os.path.basename(f).startswith('campaign-')] + \
            [f for f in files if not os.path.basename(f).startswith('campaign-') and os.path.basename(f) != 'stress.md'][:5]
    out = []
    for f in files[:8]:
        text = read(f)
        title = next((l.lstrip('# ').strip() for l in text.splitlines() if l.startswith('#')), os.path.basename(f))
        lines = [l.strip(' -*|') for l in text.splitlines() if VERDICT.search(l) and not l.startswith('#')][:6]
        out.append({'file': os.path.basename(f), 'title': title, 'lines': lines,
                    'when': datetime.datetime.fromtimestamp(os.path.getmtime(f), datetime.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')})
    return out


def campaign_progress(activity):
    """The running campaign's last START line and how many of its runs ended, from activity.log."""
    starts = [l for l in activity if ' campaign[' in l and ' START ' in l]
    if not starts:
        return None
    last = starts[-1]
    since = activity[activity.index(last):]
    runs = sum(1 for l in since if 'stress.py' in l and ' END ' in l)
    done = any(' campaign[' in l and ' END ' in l for l in since)
    return {'started': last.split(' ', 1)[0], 'what': last.split(' START ', 1)[-1][:200], 'runs_done': runs, 'finished': done}


def flags():
    """room_lab/flags.json as a list (the user guide's flag table is generated from it)."""
    try:
        d = json.loads(read(os.path.join(DOCS if os.path.isdir(DOCS) else QWS, 'room_lab/flags.json')) or '{}')
    except ValueError:
        return []
    items = d.get('flags', []) if isinstance(d, dict) else d
    return [{k: f.get(k) for k in ('name', 'where', 'state', 'default', 'category', 'reason', 'menu')} for f in items if isinstance(f, dict)]


def git(repo, *a):
    return subprocess.run(['git', '-C', repo, *a], capture_output=True, text=True, timeout=60).stdout


def fresh_docs():
    """Fetch origin/main into the docs worktree (never the shared checkout), so the Morning list is the latest."""
    if os.path.isdir(DOCS):
        git(DOCS, 'fetch', '-q', 'origin')
        git(DOCS, 'checkout', '-q', '--detach', 'origin/main')
    return DOCS if os.path.isdir(DOCS) else QWS


SKIP = re.compile(r'^(captain: (log|ticks|kanban mirror)|house: mirror rev|Merge |(test|e2e|probe|zz)-[\w-]*: )', re.I)  # routine + test apps


def area(repo_name, subject, files):
    s = subject.lower()
    if repo_name == 'apps':
        return 'apps'
    if s.startswith('v0.') or any(f.startswith('room_lab/') for f in files):
        return 'headset'
    if s.startswith('assistant') or any(f.startswith('assistant/') for f in files):
        return 'assistant'
    if any(f.startswith('house/') for f in files) or s.startswith('house'):
        return 'house'
    if any(f.startswith('bench/') for f in files) or re.match(r'^(bench|stress|campaign|verdict|gate)', s):
        return 'benchmarks'
    return 'coordination'


def overnight():
    """Every agent's commits since SINCE, one line each, grouped by area (routine log/mirror commits skipped)."""
    out = []
    for name, repo in (('qws', DOCS if os.path.isdir(DOCS) else QWS), ('apps', APPS)):
        log = git(repo, 'log', f'--since={SINCE}', '--format=@@%h\t%cI\t%s', '--name-only')
        for block in log.split('@@')[1:]:
            lines = block.strip().splitlines()
            sha, when, subj = lines[0].split('\t', 2)
            if SKIP.search(subj):
                continue
            utc = datetime.datetime.fromisoformat(when).astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            out.append({'sha': sha, 'when': utc, 'area': area(name, subj, lines[1:]), 'repo': name, 'subject': subj[:140]})
    out.sort(key=lambda c: c['when'], reverse=True)
    return out


AIR_NET = r"""
echo "ip=$(ipconfig getifaddr en0)"
system_profiler SPAirPortDataType 2>/dev/null | awk '/Current Network Information:/{getline; sub(/^ */, ""); sub(/:$/, ""); print "ssid=" $0; exit}'
/usr/local/bin/tailscale status --json 2>/dev/null | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); e=d.get("ExitNodeStatus") or {}; n=[p.get("HostName") for p in (d.get("Peer") or {}).values() if p.get("ExitNode")]; print("ts=" + json.dumps({"up": d.get("BackendState") == "Running", "state": d.get("BackendState"), "ip": ((d.get("Self") or {}).get("TailscaleIPs") or [None])[0], "exit_node": n[0] if n else None, "exit_online": bool(e.get("Online"))}))'
"""


def air_network():
    """The laptop's Wi-Fi network and Tailscale, read over ssh (Tailscale): read-only, a few seconds. Unreachable is
    itself the signal (Tailscale off, or the laptop asleep)."""
    out = {'checked': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
    try:
        p = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', 'pinpin-air', 'bash -s'], input=AIR_NET,
                           capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired:
        return dict(out, reachable=False, error='ssh timed out')
    if p.returncode != 0 and 'ip=' not in p.stdout:
        return dict(out, reachable=False, error=(p.stderr.strip().splitlines() or ['ssh failed'])[-1][:160])
    out['reachable'] = True
    for line in p.stdout.splitlines():
        k, _, v = line.partition('=')
        if k == 'ts':
            try:
                out['tailscale'] = json.loads(v)
            except ValueError:
                pass
        elif k in ('ip', 'ssid'):
            out[k] = v.strip() or None
    return out


def main():
    docs = fresh_docs()
    activity = read(os.path.join(QWS, 'bench/activity.log')).splitlines()
    head = subprocess.run(['git', '-C', QWS, 'log', '-1', '--format=%h %cI %s'], capture_output=True, text=True).stdout.strip()
    doc = {
        'generated': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'roadmap': read(os.path.join(docs, 'docs/captain/ROADMAP.md')),
        'captain_log': read(os.path.join(docs, 'docs/captain/LOG.md')),
        'overnight': overnight(),
        'overnight_since': SINCE,
        'bench': bench(),
        'campaign': campaign_progress(activity[-3000:]),
        'activity': activity[-20:],
        'owner': read(os.path.join(QWS, 'bench/headset_owner')).strip(),
        'qws_head': head,
        'flags': flags(),
        'air_network': air_network(),
    }
    json.dump(doc, sys.stdout, ensure_ascii=False)


if __name__ == '__main__':
    main()
