#!/usr/bin/env python3
"""pinpin-apps runner (the laptop that owns the virtual displays): one chromeless Chrome window per screen that has an
app, on that screen's virtual display, so the atlas streams it to the headset.

Every few seconds it reads
  the Mini's  GET :57830/api/screens   which screens have an app, and the laptop screen id each one shows, and
  the local   GET :57710/displays      the virtual displays pinpin-displays has (screen id -> CGDirectDisplayID),
and makes the windows match: launch, relaunch when one dies or its display moves or resizes, close when the app is
unassigned or the display goes away. It never creates or removes displays (that is pinpin-displays' job, driven by the
headset) and never opens a window on the main display.

Each window is its own Chrome instance (--user-data-dir per screen), in --app mode, pointed at
http://<mini>:57830/screen/<id>. The page itself follows app changes and reloads (server-sent events), so remapping
a screen to another app needs no relaunch here.

Stdlib only.   python3 apps_runner.py [--mini http://192.168.0.50:57830] [--once] [--dry-run]
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
HOME = os.path.expanduser('~/.local/share/pinpin-apps')
MENU_BAR = 25  # points macOS keeps at the top of every display ("Displays have separate Spaces")
FLAGS = ['--no-first-run', '--no-default-browser-check', '--disable-session-crashed-bubble', '--hide-crash-restore-bubble',
         '--disable-features=Translate,MediaRouter,DialMediaRouteProvider', '--disable-background-timer-throttling',
         '--disable-backgrounding-occluded-windows', '--disable-renderer-backgrounding', '--autoplay-policy=no-user-gesture-required',
         '--disable-sync', '--password-store=basic', '--use-mock-keychain', '--noerrdialogs', '--disable-infobars']


def log(*a):
    print(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), *a, file=sys.stderr, flush=True)


def get_json(url, timeout=4):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def display_bounds(ids):
    """{CGDirectDisplayID: (x, y, w, h)} in global points, plus the main display's id, through JXA (no compiler)."""
    js = ('ObjC.import("CoreGraphics"); var o = {main: $.CGMainDisplayID(), b: {}}; ' + json.dumps(list(ids)) +
          '.forEach(function (d) { var r = $.CGDisplayBounds(d); o.b[d] = [r.origin.x, r.origin.y, r.size.width, r.size.height]; });'
          ' JSON.stringify(o)')
    out = json.loads(subprocess.run(['osascript', '-l', 'JavaScript', '-e', js], capture_output=True, text=True, timeout=10).stdout)
    return {int(k): tuple(int(v) for v in b) for k, b in out['b'].items() if b[2] > 0}, int(out['main'])


def plan(screens, displays, bounds, main_id):
    """What should be running: {screen: (url_path, (x, y, w, h))}. Pure, for the tests."""
    want = {}
    by_screen = {d['screen']: d['display'] for d in displays}
    for s in screens:
        if not s.get('app') or s.get('display') is None:
            continue
        did = by_screen.get(s['display'])
        if did is None or did == main_id or did not in bounds:
            continue
        x, y, w, h = bounds[did]
        want[s['screen']] = (f'/screen/{s["screen"]}', (x, y + MENU_BAR, w, h - MENU_BAR))
    return want


def chrome_args(mini, screen, path, box, mode='app'):
    x, y, w, h = box
    prof = os.path.join(HOME, 'profiles', f'screen-{screen}')
    a = [CHROME, f'--user-data-dir={prof}', f'--window-position={x},{y}', f'--window-size={w},{h}', *FLAGS]
    a += ['--kiosk', mini + path] if mode == 'kiosk' else [f'--app={mini}{path}']
    return a


class Runner:
    def __init__(self, a):
        self.a, self.procs = a, {}  # screen -> (Popen, path, box)
        self.backoff = {}           # screen -> (next try time, delay)

    def step(self):
        try:
            screens = get_json(self.a.mini + '/api/screens')
            displays = get_json(self.a.displays + '/displays')
        except Exception as e:
            log('not syncing:', e)  # keep what runs; a Mini blip must not blank the screens
            return
        dids = {d['display'] for d in displays}
        bounds, main_id = display_bounds(dids) if dids else ({}, 0)
        want = plan(screens, displays, bounds, main_id)
        for s in list(self.procs):
            p, path, box = self.procs[s]
            if s not in want or want[s] != (path, box) or p.poll() is not None:
                why = 'unassigned or display gone' if s not in want else ('moved' if p.poll() is None else f'exited {p.returncode}')
                self.stop(s, why)
        for s, (path, box) in want.items():
            if s in self.procs:
                continue
            t, delay = self.backoff.get(s, (0, 2))
            if time.time() < t:
                continue
            args = chrome_args(self.a.mini, s, path, box, self.a.mode)
            log(f'screen {s}: open {path} at {box}')
            if self.a.dry_run:
                print(' '.join(args)); continue
            os.makedirs(os.path.join(HOME, 'profiles'), exist_ok=True)
            with open(os.path.join(HOME, f'screen-{s}.log'), 'ab') as lf:
                p = subprocess.Popen(args, stdout=lf, stderr=lf, start_new_session=True)
            self.procs[s] = (p, path, box)
            self.backoff[s] = (time.time() + delay, min(delay * 2, 120))  # if it dies at once, wait longer each time

    def stop(self, s, why):
        p = self.procs.pop(s)[0]
        log(f'screen {s}: close ({why})')
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(5)
            except Exception:
                try: os.killpg(p.pid, signal.SIGKILL)
                except Exception: pass
        if why != 'moved' and not why.startswith('exited'):
            self.backoff.pop(s, None)

    def stop_all(self, *_):
        for s in list(self.procs):
            self.stop(s, 'runner stopping')
        sys.exit(0)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--mini', default=os.environ.get('PINPIN_APPS', 'http://192.168.0.50:57830'))
    ap.add_argument('--displays', default='http://127.0.0.1:57710')
    ap.add_argument('--mode', choices=['app', 'kiosk'], default='app')
    ap.add_argument('--period', type=float, default=3.0)
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    r = Runner(a)
    signal.signal(signal.SIGTERM, r.stop_all)
    signal.signal(signal.SIGINT, r.stop_all)
    log(f'apps runner: {a.mini}, displays {a.displays}, mode {a.mode}')
    while True:
        r.step()
        if a.once:
            break
        time.sleep(a.period)


if __name__ == '__main__':
    main()
