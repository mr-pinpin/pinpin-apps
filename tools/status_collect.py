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

QWS = os.environ.get('QWS', '/root/work/quest-wall-screen')
VERDICT = re.compile(r'\b(KEEP|DROP|INCONCLUSIVE|PASS|FAIL|verdict|winner|better|worse)\b', re.I)


def read(path, default=''):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return default


def bench():
    """Finished campaigns first (bench/campaign.py writes campaign-<block>-<date>.md), then other recent verdict notes."""
    files = sorted(glob.glob(os.path.join(QWS, 'bench/results/*.md')), key=os.path.getmtime, reverse=True)
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


def main():
    activity = read(os.path.join(QWS, 'bench/activity.log')).splitlines()
    head = subprocess.run(['git', '-C', QWS, 'log', '-1', '--format=%h %cI %s'], capture_output=True, text=True).stdout.strip()
    doc = {
        'generated': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'roadmap': read(os.path.join(QWS, 'docs/captain/ROADMAP.md')),
        'bench': bench(),
        'campaign': campaign_progress(activity[-3000:]),
        'activity': activity[-20:],
        'owner': read(os.path.join(QWS, 'bench/headset_owner')).strip(),
        'qws_head': head,
    }
    json.dump(doc, sys.stdout, ensure_ascii=False)


if __name__ == '__main__':
    main()
