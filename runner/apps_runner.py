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
a screen to another app needs no relaunch here. When the assistant opens a URL on a screen (POST /api/screens/<id>/open)
the runner navigates that window over the DevTools protocol (--remote-debugging-port, localhost only) and reports the
seq it shows back to the Mini; a null URL navigates back to the app page.

Stdlib only.   python3 apps_runner.py [--mini http://192.168.0.50:57830] [--once] [--dry-run]
"""
import argparse
import base64
import hmac
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
HOME = os.path.expanduser('~/.local/share/pinpin-apps')
MENU_BAR = 25  # points macOS keeps at the top of every display ("Displays have separate Spaces")
FLAGS = ['--no-first-run', '--no-default-browser-check', '--disable-session-crashed-bubble', '--hide-crash-restore-bubble',
         '--disable-features=Translate,MediaRouter,DialMediaRouteProvider', '--disable-background-timer-throttling',
         '--disable-backgrounding-occluded-windows', '--disable-renderer-backgrounding', '--autoplay-policy=no-user-gesture-required',
         '--disable-sync', '--password-store=basic', '--use-mock-keychain', '--noerrdialogs', '--disable-infobars']


def log(*a):
    print(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), *a, file=sys.stderr, flush=True)


def get_json(url, timeout=4, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
        return json.loads(r.read())


def cdp_port(screen):
    return 9300 + int(screen) % 600


class CDP:
    """Just enough of the DevTools protocol (a WebSocket client in the stdlib) to navigate and read window bounds."""
    def __init__(self, port):
        tabs = get_json(f'http://127.0.0.1:{port}/json', timeout=2)
        url = next(t for t in tabs if t.get('type') == 'page')['webSocketDebuggerUrl']
        host, rest = url[len('ws://'):].split('/', 1)
        h, port_ = host.rsplit(':', 1)
        self.s = socket.create_connection((h, int(port_)), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f'GET /{rest} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                        f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            buf += self.s.recv(4096)
        self.buf, self.id = buf.split(b'\r\n\r\n', 1)[1], 0

    def _read(self, n):
        while len(self.buf) < n:
            d = self.s.recv(65536)
            if not d:
                raise EOFError
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def call(self, method, params=None):
        self.id += 1
        msg = json.dumps({'id': self.id, 'method': method, 'params': params or {}}).encode()
        mask, n = os.urandom(4), len(msg)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack('>H', n))
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(msg)))
        while True:
            data = b''
            while True:
                b1, b2 = self._read(2)
                k = b2 & 0x7f
                if k == 126: k = struct.unpack('>H', self._read(2))[0]
                elif k == 127: k = struct.unpack('>Q', self._read(8))[0]
                data += self._read(k)
                if b1 & 0x80:
                    break
            m = json.loads(data)
            if m.get('id') == self.id:
                if 'error' in m:
                    raise RuntimeError(m['error'])
                return m.get('result', {})

    def close(self):
        self.s.close()


def navigate(screen, url):
    c = CDP(cdp_port(screen))
    try:
        c.call('Page.navigate', {'url': url})
    finally:
        c.close()


def window_bounds(screen):
    """(x, y, w, h) of the screen's Chrome window in global points, from Chrome itself (no Screen Recording needed)."""
    c = CDP(cdp_port(screen))
    try:
        b = c.call('Browser.getWindowForTarget')['bounds']
        return b['left'], b['top'], b['width'], b['height']
    finally:
        c.close()


def display_bounds(ids):
    """{CGDirectDisplayID: (x, y, w, h)} in global points, plus the main display's id, through JXA (no compiler)."""
    js = ('ObjC.import("CoreGraphics"); var o = {main: $.CGMainDisplayID(), b: {}}; ' + json.dumps(list(ids)) +
          '.forEach(function (d) { var r = $.CGDisplayBounds(d); o.b[d] = [r.origin.x, r.origin.y, r.size.width, r.size.height]; });'
          ' JSON.stringify(o)')
    out = json.loads(subprocess.run(['osascript', '-l', 'JavaScript', '-e', js], capture_output=True, text=True, timeout=10).stdout)
    return {int(k): tuple(int(v) for v in b) for k, b in out['b'].items() if b[2] > 0}, int(out['main'])


def plan(screens, displays, bounds, main_id):
    """What should be running: {screen: (target, seq, (x, y, w, h))}. target: an opened URL, or the screen's app page
    path; seq: the open's sequence number (0 for the app page). Pure, for the tests."""
    want = {}
    by_screen = {d['screen']: d['display'] for d in displays}
    for s in screens:
        if not (s.get('app') or s.get('url')) or s.get('display') is None:
            continue
        did = by_screen.get(s['display'])
        if did is None or did == main_id or did not in bounds:
            continue
        x, y, w, h = bounds[did]
        target = s.get('url') or f'/screen/{s["screen"]}'
        want[s['screen']] = (target, s.get('seq', 0) if s.get('url') else 0, (x, y + MENU_BAR, w, h - MENU_BAR))
    return want


def full(mini, target):
    return target if target.startswith(('http://', 'https://')) else mini + target


def chrome_args(mini, screen, target, box, mode='app'):
    x, y, w, h = box
    prof = os.path.join(HOME, 'profiles', f'screen-{screen}')
    a = [CHROME, f'--user-data-dir={prof}', f'--window-position={x},{y}', f'--window-size={w},{h}',
         f'--remote-debugging-port={cdp_port(screen)}', '--remote-debugging-address=127.0.0.1', *FLAGS]
    a += ['--kiosk', full(mini, target)] if mode == 'kiosk' else [f'--app={full(mini, target)}']
    return a


# ---- input over CDP: synthetic CGEvent clicks never reach Chrome --app windows (2026-11-04, work-42), so the headset's
# pinch-clicks and the assistant's clicks come here (the Mini forwards /api/screens/<id>/{click,move,scroll,type,key})

MODS = {'alt': 1, 'option': 1, 'ctrl': 2, 'control': 2, 'cmd': 4, 'meta': 4, 'command': 4, 'shift': 8}
KEYS = {  # key -> (code, windowsVirtualKeyCode, text typed on keyDown)
    'enter': ('Enter', 13, '\r'), 'return': ('Enter', 13, '\r'), 'tab': ('Tab', 9, '\t'), 'escape': ('Escape', 27, ''),
    'esc': ('Escape', 27, ''), 'backspace': ('Backspace', 8, ''), 'delete': ('Delete', 46, ''), 'space': ('Space', 32, ' '),
    'arrowup': ('ArrowUp', 38, ''), 'arrowdown': ('ArrowDown', 40, ''), 'arrowleft': ('ArrowLeft', 37, ''),
    'arrowright': ('ArrowRight', 39, ''), 'up': ('ArrowUp', 38, ''), 'down': ('ArrowDown', 40, ''), 'left': ('ArrowLeft', 37, ''),
    'right': ('ArrowRight', 39, ''), 'pageup': ('PageUp', 33, ''), 'pagedown': ('PageDown', 34, ''), 'home': ('Home', 36, ''),
    'end': ('End', 35, ''),
}
KEY_NAMES = {'enter': 'Enter', 'return': 'Enter', 'esc': 'Escape', 'up': 'ArrowUp', 'down': 'ArrowDown', 'left': 'ArrowLeft',
             'right': 'ArrowRight', 'space': ' '}


def key_events(combo):
    """'Cmd+L' -> the keyDown/keyUp params for Input.dispatchKeyEvent."""
    parts = [p.strip() for p in str(combo).split('+') if p.strip()]
    if not parts:
        raise ValueError('empty key')
    mods = 0
    for p in parts[:-1]:
        if p.lower() not in MODS:
            raise ValueError(f'unknown modifier {p}')
        mods |= MODS[p.lower()]
    k = parts[-1]
    if k.lower() in KEYS:
        code, vk, text = KEYS[k.lower()]
        key = KEY_NAMES.get(k.lower(), code)
    elif len(k) == 1:
        key, code, vk, text = k, ('Key' + k.upper()) if k.isalpha() else ('Digit' + k if k.isdigit() else ''), ord(k.upper()), k
    elif k.lower().startswith('f') and k[1:].isdigit():
        key, code, vk, text = k.upper(), k.upper(), 111 + int(k[1:]), ''
    else:
        raise ValueError(f'unknown key {k}')
    if mods & (2 | 4):  # with Ctrl/Cmd it is a shortcut, not typing
        text = ''
    down = {'type': 'keyDown' if text else 'rawKeyDown', 'key': key, 'code': code, 'windowsVirtualKeyCode': vk, 'modifiers': mods}
    if text:
        down['text'] = down['unmodifiedText'] = text
    if mods & 4 and len(k) == 1:  # macOS: Chrome maps Cmd shortcuts through editing commands
        cmd = {'a': 'selectAll', 'c': 'copy', 'v': 'paste', 'x': 'cut', 'z': 'undo'}.get(k.lower())
        if cmd:
            down['commands'] = [cmd]
    return [down, {'type': 'keyUp', 'key': key, 'code': code, 'windowsVirtualKeyCode': vk, 'modifiers': mods}]


class Input:
    """One persistent CDP connection per screen window (scroll comes at up to 20/s), page geometry cached briefly."""
    def __init__(self, runner):
        self.r, self.conns, self.locks, self.geo = runner, {}, {}, {}

    def _conn(self, s):
        c = self.conns.get(s)
        if c is None:
            c = self.conns[s] = CDP(cdp_port(s))
        return c

    def call(self, s, method, params=None):
        with self.locks.setdefault(s, threading.Lock()):
            for attempt in (0, 1):  # a stale socket (page navigated, Chrome restarted): reconnect once
                try:
                    return self._conn(s).call(method, params)
                except Exception:
                    c = self.conns.pop(s, None)
                    if c:
                        try: c.close()
                        except Exception: pass
                    self.geo.pop(s, None)
                    if attempt:
                        raise

    def point(self, s, u, v, space):
        """u, v in 0..10000 -> CSS px in the page. space 'display' (default): of the screen's whole virtual display, like
        pinpin-displays; 'viewport': of the page's visible area."""
        g = self.geo.get(s)
        if not g or time.time() - g[0] > 2:
            js = '[screenX, screenY, outerWidth, outerHeight, innerWidth, innerHeight]'
            r = self.call(s, 'Runtime.evaluate', {'expression': js, 'returnByValue': True})
            g = self.geo[s] = (time.time(), r['result']['value'])
        sx, sy, ow, oh, iw, ih = g[1]
        u, v = max(0.0, min(10000.0, float(u))) / 10000, max(0.0, min(10000.0, float(v))) / 10000
        if space == 'viewport':
            return u * iw, v * ih
        disp = self.r.display_box.get(s)
        if not disp:
            raise LookupError('no window')
        dx, dy, dw, dh = disp
        gx, gy = dx + u * dw, dy + v * dh                # global point on the display
        cx, cy = sx + (ow - iw), sy + (oh - ih)          # the page's top-left on screen (the window chrome is on top)
        return min(max(gx - cx, 0), iw - 1), min(max(gy - cy, 0), ih - 1)

    def what(self, s, x, y):
        try:
            js = (f'(function(){{var e=document.elementFromPoint({x:.0f},{y:.0f});if(!e)return "";'
                  f'var a=e.closest("a,button,input,textarea,select,[role=button]")||e;'
                  f'return a.tagName+": "+(a.innerText||a.value||a.getAttribute("aria-label")||"").trim().slice(0,60)}})()')
            return self.call(s, 'Runtime.evaluate', {'expression': js, 'returnByValue': True})['result'].get('value', '')
        except Exception:
            return ''

    def do(self, s, kind, d):
        if s not in self.r.procs or self.r.procs[s][0].poll() is not None:
            raise LookupError('no window')
        space = d.get('space', 'display')
        if kind in ('click', 'move', 'scroll'):
            x, y = self.point(s, d.get('u', 5000), d.get('v', 5000), space)
        if kind == 'move':
            self.call(s, 'Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
            return {'ok': True, 'x': round(x), 'y': round(y)}
        if kind == 'click':
            btn = d.get('button', 'left')
            if btn not in ('left', 'right', 'middle'):
                raise ValueError('button: left, right or middle')
            n = 2 if d.get('double') or int(d.get('count', 1)) >= 2 else 1
            el = self.what(s, x, y)
            self.call(s, 'Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
            for c in range(1, n + 1):
                for t in ('mousePressed', 'mouseReleased'):
                    self.call(s, 'Input.dispatchMouseEvent', {'type': t, 'x': x, 'y': y, 'button': btn, 'clickCount': c})
            return {'ok': True, 'x': round(x), 'y': round(y), 'element': el}
        if kind == 'scroll':
            self.call(s, 'Input.dispatchMouseEvent', {'type': 'mouseWheel', 'x': x, 'y': y,
                                                      'deltaX': float(d.get('dx', 0)), 'deltaY': float(d.get('dy', 0))})
            return {'ok': True, 'x': round(x), 'y': round(y)}
        if kind == 'type':
            t = d.get('text')
            if not isinstance(t, str) or not t:
                raise ValueError('text required')
            self.call(s, 'Input.insertText', {'text': t[:5000]})
            return {'ok': True}
        if kind == 'key':
            for ev in key_events(d.get('combo') or d.get('key') or ''):
                self.call(s, 'Input.dispatchKeyEvent', ev)
            return {'ok': True}
        if kind == 'eval':  # read the page (DOM checks, the live URL); the result must be JSON-serialisable
            js = d.get('js')
            if not isinstance(js, str) or not js.strip() or len(js) > 20000:
                raise ValueError('js: an expression, at most 20000 characters')
            r = self.call(s, 'Runtime.evaluate', {'expression': js, 'returnByValue': True, 'awaitPromise': True, 'timeout': 5000})
            if r.get('exceptionDetails'):
                ex = r['exceptionDetails']
                return {'ok': False, 'error': ((ex.get('exception') or {}).get('description') or ex.get('text') or 'exception')[:500]}
            v = (r.get('result') or {}).get('value')
            if len(json.dumps(v, default=str)) > 65536:
                raise ValueError('result over 64 KB: return less')
            return {'ok': True, 'value': v}
        raise ValueError(f'unknown input {kind}')


def read_token():
    """~/.local/share/pinpin-apps/token (the apps token, as on the Mini); absent: input stays open (old installs)."""
    try:
        with open(os.path.join(HOME, 'token')) as f:
            return f.read().strip()
    except OSError:
        return ''


def input_server(runner, port):
    inp = Input(runner)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def reply(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

        def do_POST(self):
            tok = read_token()
            got = (self.headers.get('Authorization') or '').removeprefix('Bearer ').strip()
            if tok and not hmac.compare_digest(got.encode(), tok.encode()):  # only the Mini (it holds the token) may drive us
                return self.reply(401, {'error': 'token required'})
            parts = [p for p in self.path.split('?')[0].split('/') if p]  # /input/<screen>/<kind>
            if len(parts) != 3 or parts[0] != 'input' or not parts[1].isdigit():
                return self.reply(404, {'error': 'not found'})
            try:
                d = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                return self.reply(200, inp.do(int(parts[1]), parts[2], d))
            except LookupError as e:
                return self.reply(404, {'error': str(e)})
            except (ValueError, TypeError) as e:
                return self.reply(400, {'error': str(e)})
            except Exception as e:
                return self.reply(502, {'error': f'cdp: {e}'})

    srv = ThreadingHTTPServer(('0.0.0.0', port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Runner:
    def __init__(self, a):
        self.a, self.procs = a, {}  # screen -> (Popen, target, seq, box)
        self.display_box = {}       # screen -> (x, y, w, h) of its whole virtual display, for input u/v
        self.backoff = {}           # screen -> (next try time, delay)

    def step(self):
        try:
            # the header tells the Mini where to forward input (it learns our address from the connection)
            screens = get_json(self.a.mini + '/api/screens', headers={'X-PinPin-Runner': str(self.a.input_port)} if self.a.input_port else None)
            displays = get_json(self.a.displays + '/displays')
        except Exception as e:
            log('not syncing:', e)  # keep what runs; a Mini blip must not blank the screens
            return
        dids = {d['display'] for d in displays}
        bounds, main_id = display_bounds(dids) if dids else ({}, 0)
        want = plan(screens, displays, bounds, main_id)
        self.display_box = {s: (b[0], b[1] - MENU_BAR, b[2], b[3] + MENU_BAR) for s, (_, _, b) in want.items()}
        for s in list(self.procs):
            p, target, seq, box = self.procs[s]
            if s not in want or want[s][2] != box or p.poll() is not None:
                why = 'unassigned or display gone' if s not in want else ('moved' if p.poll() is None else f'exited {p.returncode}')
                self.stop(s, why)
            elif want[s][:2] != (target, seq):  # same window, new page: navigate it
                t, q, _ = want[s]
                try:
                    navigate(s, full(self.a.mini, t))
                    log(f'screen {s}: navigate {t}')
                    self.procs[s] = (p, t, q, box)
                    self.shown(s, q)
                except Exception as e:
                    log(f'screen {s}: navigate failed ({e}), reopening')
                    self.stop(s, 'moved')
        for s, (target, seq, box) in want.items():
            if s in self.procs:
                continue
            t, delay = self.backoff.get(s, (0, 2))
            if time.time() < t:
                continue
            args = chrome_args(self.a.mini, s, target, box, self.a.mode)
            log(f'screen {s}: open {target} at {box}')
            if self.a.dry_run:
                print(' '.join(args)); continue
            os.makedirs(os.path.join(HOME, 'profiles'), exist_ok=True)
            with open(os.path.join(HOME, f'screen-{s}.log'), 'ab') as lf:
                p = subprocess.Popen(args, stdout=lf, stderr=lf, start_new_session=True)
            self.procs[s] = (p, target, seq, box)
            self.backoff[s] = (time.time() + delay, min(delay * 2, 120))  # if it dies at once, wait longer each time
            self.shown(s, seq)

    def shown(self, s, seq):
        if not seq:
            return
        try:
            r = urllib.request.Request(f'{self.a.mini}/api/screens/{s}/shown', data=json.dumps({'seq': seq}).encode(),
                                       method='POST', headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(r, timeout=4).read()
        except Exception as e:
            log(f'screen {s}: could not report seq {seq}: {e}')

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
    ap.add_argument('--input-port', type=int, default=57735, help='LAN port for CDP input (the Mini forwards to it); 0: off')
    a = ap.parse_args()
    r = Runner(a)
    signal.signal(signal.SIGTERM, r.stop_all)
    signal.signal(signal.SIGINT, r.stop_all)
    if a.input_port:
        input_server(r, a.input_port)
    log(f'apps runner: {a.mini}, displays {a.displays}, mode {a.mode}, input :{a.input_port}')
    while True:
        r.step()
        if a.once:
            break
        time.sleep(a.period)


if __name__ == '__main__':
    main()
