#!/usr/bin/env python3
"""pinpin-apps server (Mini, port 57830): serves the mini-apps to the wall screens and lets the assistant evolve them.

  GET  /help                        the user guide (ui/help.html; its switch table comes from room_lab/flags.json)
  GET  /status                      the morning status page (ui/status.html): roadmap, headset, benchmarks, activity,
                                    house plan, screens;  GET /api/status its data;  GET /house-plan.svg the floor plan
  GET  /                            the control page (ui/index.html): app gallery, screens and their apps
  GET  /screen/<id>                 the app house.json maps to screen <id>: host.js + the app in a sandboxed iframe
                                    (the runner's window URL);  GET /frame/<app>/  the app document itself
  GET  /app/<app>/                  preview of an app with the SDK, screen "preview"
  GET  /apps/<app>/<path>           raw app files;  GET /sdk/<file>  the SDK
  GET  /events?screen=<id>          server-sent events to the page: reload, voice {text}
  Powerful calls need the shared token (Authorization: Bearer <token>, <data>/token): input/eval/say/app/open/reload
  on screens, build, PUT/DELETE apps, rollback. GET /api/auth shows the rollout (grace hour, unauthenticated callers).
  POST /api/status/component        {component, text, ok?, link?} one check line on /status (token), or write the same
                                    JSON to <data>/status/components/<component>.json
  GET  /api/apps                    the catalog: [{id, name, description, size, permissions, screens, icon (data: URI),
                                    icon_png (URL of a PNG, ?size=N, for the headset)}]
  GET  /api/apps/<app>              {manifest, files: {path: text}, commit}
  GET  /api/authoring               docs/AUTHORING.md (how to write an app), for the assistant's context
  PUT  /api/apps/<app>              {files: {path: text|null}, message, replace?, by?, check?, say?, create?, base?}
                                    -> one git commit; create: 409 if the id exists; base: 409 unless it is the app's
                                    current commit (GET returns it), so concurrent edits are never overwritten;
                                    screens reload; check: render it in headless Chrome, report JS errors + text
  DELETE /api/apps/<app>            removes the folder (one commit) and unassigns its screens
  GET  /api/apps/<app>/history      [{commit, when, subject}]
  GET  /api/apps/<app>/thumb.png    a 960x600 render of the app (cached per commit, refreshed every 10 min)
  POST /api/apps/<app>/rollback     {commit} -> folder as at that commit, as a new commit
  GET|PUT /api/apps/<app>/data      the app's stored JSON (SDK pinpin.store), outside git
  POST /api/apps/<app>/inbox        {from, kind, data} app-to-app message (host pages only; sender permissions.send,
                                    receiver accepts);  GET .../inbox, POST .../inbox/ack {ids}: the receiver's host
  GET  /api/screens                 [{screen, app, display, clients, w, h}] from house.json
  POST /api/screens/<id>/app        {app: id|null} -> house.json "apps" section, that screen reloads
  POST /api/screens/<id>/say        {text} -> the page's pinpin.onVoice handlers
  POST /api/screens/<id>/reload
  POST /api/screens/<id>/open       {url|null} the runner navigates that screen's window there (null: back to its app);
                                    GET /api/screens shows url, seq and shown_seq (= seq once it is on screen)
  POST /api/screens/<id>/shown      {seq} (the runner)
  POST /api/screens/<id>/click|move|scroll|type|key   input into that screen's runner window over CDP (forwarded to
                                    the laptop runner): {u, v (0..10000 of the display, or space:"viewport"), button,
                                    count|double} | {u, v} | {u, v, dx, dy} | {text} | {key|combo: "Cmd+L"};
                                    POST .../eval {js} -> {ok, value}: an expression in the page (DOM checks, live URL)
  POST /api/build                   {prompt, app?, screen?} -> the assistant builds/changes an app with its tools (control page)
  POST /api/ask                     {app, screen, prompt, system?, json?, model?} -> {text, json?} (assistant, or the stub)
  GET  /proxy?app=<app>&url=<url>   GET through the Mini, only to hosts in the app's permissions.fetch

Stdlib only. The repo (--repo) is a git checkout of mr-pinpin/pinpin-apps; app data and secrets live in --data.
"""
import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
import manifest  # noqa: E402

SHA = re.compile(r'^[0-9a-f]{7,40}$')
MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
        '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif', '.woff2': 'font/woff2',
        '.txt': 'text/plain; charset=utf-8', '.md': 'text/plain; charset=utf-8'}
MAX_BODY = 12 << 20
MAX_STORE = 256 << 10
MAX_PROXY = 5 << 20
PROXY_TTL = 60
AUTHOR = ['-c', 'user.name=Mr. PinPin', '-c', 'user.email=miguelemosreverte@gmail.com']


def atomic_write(path, data):
    """Crash-safe replace: a temp file in the same directory, fsync, rename, fsync the directory. A crash at any point
    leaves either the old file or the new one, never a torn mix (the user's rule: what they set is never corrupted)."""
    if isinstance(data, str):
        data = data.encode()
    d = os.path.dirname(path) or '.'
    tmp = f'{path}.tmp-{os.getpid()}-{threading.get_ident()}'
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def log(*a):
    print(now(), *a, file=sys.stderr, flush=True)


# ---- the apps repo: every write is one commit

class Conflict(Exception):
    pass


class Repo:
    def __init__(self, root, push=False):
        self.root, self.push, self.lock = root, push, threading.Lock()
        self.apps = os.path.join(root, 'apps')
        os.makedirs(self.apps, exist_ok=True)
        if not os.path.isdir(os.path.join(root, '.git')):
            self.git('init', '-q', '-b', 'main')

    def git(self, *a, check=True):
        r = subprocess.run(['git', *AUTHOR, '-C', self.root, *a], capture_output=True, text=True, timeout=60)
        if check and r.returncode:
            raise RuntimeError(f'git {a[0]}: {r.stderr.strip()[:300]}')
        return r.stdout.strip()

    def ids(self):
        return sorted(d for d in os.listdir(self.apps)
                      if manifest.check_id(d) and os.path.isfile(os.path.join(self.apps, d, 'manifest.json')))

    def files(self, app_id):
        base, out = os.path.join(self.apps, app_id), {}
        for dp, dn, fn in os.walk(base):
            dn[:] = [d for d in dn if not d.startswith('.')]
            for f in fn:
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, base).replace(os.sep, '/')
                if not manifest.check_path(rel):
                    with open(full, 'rb') as fh:
                        out[rel] = fh.read()
        return out

    def manifest(self, app_id):
        try:
            with open(os.path.join(self.apps, app_id, 'manifest.json')) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def head(self, app_id=None):
        return self.git('log', '-1', '--format=%H', *(['--', f'apps/{app_id}'] if app_id else []), check=False)

    def write(self, app_id, files, message, replace=False, by='assistant', create=False, base=None):
        """Returns (problems, commit, changed). Raises Conflict (create: the id exists; base: not the app's current
        commit), checked under the lock so two writers with the same base can't both pass."""
        with self.lock:
            exists = app_id in self.ids()
            if create and exists:
                raise Conflict(f'app {app_id} exists; pick another id or drop "create"')
            if base and exists and not self.head(app_id).startswith(base):
                raise Conflict(f'{app_id} changed since {base}; GET it again and re-apply')
            existing = {} if replace else self.files(app_id)
            problems, result = manifest.validate_files(app_id, files, existing)
            if problems:
                return problems, None, False
            base = os.path.join(self.apps, app_id)
            old = self.files(app_id)
            try:
                for p in set(old) - set(result):
                    os.remove(os.path.join(base, p))
                for p, b in result.items():
                    if old.get(p) == b:
                        continue
                    full = os.path.join(base, p)
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    atomic_write(full, b)
                return [], *self._commit(app_id, f'{app_id}: {message or "update"} (by {by})')
            except Exception:
                self.restore_folder(app_id)  # never leave a half-written app on the screens or for the next commit
                raise

    def restore_folder(self, app_id):
        """The folder back to its last commit (or gone, if it was never committed)."""
        self.git('checkout', 'HEAD', '--', f'apps/{app_id}', check=False)
        self.git('clean', '-fdq', '--', f'apps/{app_id}', check=False)

    def remove(self, app_id, message, by='assistant'):
        with self.lock:
            self.git('rm', '-r', '-q', '--ignore-unmatch', f'apps/{app_id}')
            subprocess.run(['rm', '-rf', os.path.join(self.apps, app_id)], check=False)
            return self._commit(app_id, f'{app_id}: removed - {message or "no reason given"} (by {by})')

    def rollback(self, app_id, commit, by='assistant'):
        with self.lock:
            full = self.git('rev-parse', '--verify', '-q', f'{commit}^{{commit}}', check=False)
            if not full or not self.git('ls-tree', '-r', '--name-only', full, f'apps/{app_id}/manifest.json', check=False):
                return f'{commit}: no {app_id} at that commit', None, False
            try:
                self.git('rm', '-r', '-q', '--ignore-unmatch', f'apps/{app_id}')
                self.git('checkout', full, '--', f'apps/{app_id}')
                return '', *self._commit(app_id, f'{app_id}: rollback to {full[:10]} (by {by})')
            except Exception as e:
                self.git('reset', '-q', 'HEAD', '--', f'apps/{app_id}', check=False)
                self.restore_folder(app_id)
                return f'rollback failed, the app was left as it was: {e}', None, False

    def _commit(self, app_id, msg):
        if os.path.isdir(os.path.join(self.apps, app_id)):  # after a removal, git rm already staged it
            self.git('add', '-A', f'apps/{app_id}')
        if not self.git('status', '--porcelain', '--', f'apps/{app_id}'):
            return self.head(app_id), False
        self.git('commit', '-q', '-m', msg)
        c = self.head()
        if self.push:
            threading.Thread(target=self.git, args=('push', '-q', 'origin', 'HEAD:main'), kwargs={'check': False}, daemon=True).start()
        return c, True

    def history(self, app_id, limit=50):
        out = self.git('log', f'-{limit}', '--format=%H%x09%cI%x09%s', '--', f'apps/{app_id}', check=False)
        return [dict(zip(('commit', 'when', 'subject'), l.split('\t', 2))) for l in out.splitlines() if l]


# ---- house.json: the "apps" section maps screen id -> app id (house/README.md in quest-wall-screen)

class House:
    def __init__(self, url, cache_path, by='apps'):
        self.url, self.cache_path, self.by = url.rstrip('/'), cache_path, by
        self.scratch_path = os.path.join(os.path.dirname(cache_path), 'scratch-apps.json')
        self.doc = None
        try:
            with open(cache_path) as f:
                self.doc = json.load(f)
        except (OSError, ValueError):
            pass

    def fetch(self):
        with urllib.request.urlopen(self.url + '/house', timeout=5) as r:
            doc = json.loads(r.read())
        if doc != self.doc:
            self.doc = doc
            atomic_write(self.cache_path, json.dumps(doc))
        return doc

    # scratch screen ids (tests, the assistant's spare displays): their app lives in <data>/scratch-apps.json, never in
    # the real house.json, so no test can change the user's house (work-df, 2026-11-04)
    SCRATCH = range(40, 100)

    def scratch(self):
        try:
            with open(self.scratch_path) as f:
                return {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError):
            return {}

    def mapping(self, doc=None):
        doc = doc if doc is not None else self.doc
        m = {a['screen']: a['app'] for a in (doc or {}).get('apps', [])
             if isinstance(a, dict) and isinstance(a.get('screen'), int) and isinstance(a.get('app'), str)}
        m.update(self.scratch())
        return m

    def screens(self):
        return [s for s in (self.doc or {}).get('screens', []) if isinstance(s, dict)]

    def assign(self, screen, app_id, tries=5):
        """Sets (app_id) or clears (None) the screen's app. Read-modify-write with If-Match; retries on 409.
        Scratch screens (SCRATCH) are kept locally and never reach house.json."""
        if screen in self.SCRATCH:
            m = self.scratch()
            if app_id:
                m[screen] = app_id
            else:
                m.pop(screen, None)
            atomic_write(self.scratch_path, json.dumps({str(k): v for k, v in m.items()}))
            return (self.doc or {}).get('rev')
        for _ in range(tries):
            doc = self.fetch()
            apps = [a for a in doc.get('apps', []) if not (isinstance(a, dict) and a.get('screen') == screen)]
            if app_id:
                apps.append({'screen': screen, 'app': app_id})
            apps.sort(key=lambda a: a.get('screen', 0))
            if apps == doc.get('apps', []) or (not apps and 'apps' not in doc):
                return doc['rev']
            new = dict(doc, apps=apps, by=self.by)
            body = json.dumps(new, indent=1).encode()
            req = urllib.request.Request(self.url + '/house', data=body, method='PUT', headers={
                'If-Match': str(doc['rev']), 'X-House-SHA256': hashlib.sha256(body).hexdigest(),
                'Content-Type': 'application/json', 'X-House-Note': f'screen {screen} app {app_id or "none"}'})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    rev = json.loads(r.read())['rev']
                self.fetch()
                return rev
            except urllib.error.HTTPError as e:
                if e.code != 409:
                    raise RuntimeError(f'house refused: {e.code} {e.read()[:400].decode(errors="replace")}')
        raise RuntimeError('house: still conflicting after retries')


# ---- the assistant behind /api/ask: work-42's service when configured, a stub otherwise

class StubAssistant:
    name = 'stub'

    def ask(self, req):
        return {'text': '', 'stub': True, 'note': 'no assistant configured; the app should use its own fallback'}


class HttpAssistant:
    def __init__(self, url):
        self.url, self.name = url, url

    def ask(self, req):
        body = json.dumps(req).encode()
        r = urllib.request.Request(self.url, data=body, method='POST', headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(r, timeout=90) as resp:
            return json.loads(resp.read())


def make_assistant(url):
    return HttpAssistant(url) if url else StubAssistant()


# ---- the headset, for the status page: read-only adb on this machine (the Mini holds the headset's wireless adb)

class Headset:
    PKG = 'com.pinpin.wallscreen.roomlab'

    def __init__(self, adb, serial):
        self.adb, self.serial, self.last = os.path.expanduser(adb), serial, {'state': 'unknown', 'checked': None}

    def sh(self, cmd, t=20):
        r = subprocess.run([self.adb, '-s', self.serial, 'shell', cmd], capture_output=True, text=True, timeout=t)
        return r.stdout

    def poll(self):
        out = {'checked': now()}
        try:
            if not os.path.exists(self.adb):
                out['state'] = 'no adb here'
                return out
            info = self.sh(f"dumpsys package {self.PKG} | grep -m1 versionName; dumpsys vrpowermanager | grep -m1 '^State:'; "
                           "dumpsys battery | grep -E 'level:|powered: true'")
            if not info.strip():
                out['state'] = 'unreachable'
                return out
            m = re.search(r'versionName=(\S+)', info); out['version'] = m.group(1) if m else None
            m = re.search(r'State: (\w+)', info); out['vr'] = m.group(1) if m else None  # MOUNTED (worn) / STANDBY / ...
            m = re.search(r'level: (\d+)', info); out['battery'] = int(m.group(1)) if m else None
            out['charging'] = 'powered: true' in info
            try:
                st = json.loads(self.sh(f'run-as {self.PKG} cat files/status.json') or '{}')
            except ValueError:
                st = {}
            mv = st.get('moonlight') or {}
            out.update({'fps': st.get('fps'), 'uptime_s': st.get('uptime'), 'refresh_hz': st.get('refreshHz'),
                        'screens': len((st.get('placement') or {}).get('screens', [])),
                        'source': (st.get('video') or {}).get('source'), 'stream': mv.get('stage'), 'stream_mode': mv.get('mode'),
                        'latency_ms': (st.get('video') or {}).get('latencyMs'),
                        'anchors': '{located}/{stored}'.format(**st['anchors']) if isinstance(st.get('anchors'), dict) else None,
                        'errors': (st.get('errors') or [])[-3:]})
            # the live switches: settings.json toggles (SAVESET), overridden by files/<flag>-on markers installers drop
            try:
                toggles = {k.upper(): bool(v) for k, v in (json.loads(self.sh(f'run-as {self.PKG} cat files/settings.json') or '{}')
                                                           .get('toggles') or {}).items()}
            except ValueError:
                toggles = {}
            for f in self.sh(f'run-as {self.PKG} ls files').split():
                if f.endswith('-on'):
                    toggles[f[:-3].upper()] = True
            out['toggles'] = toggles
            out['flags_on'] = sorted(k for k, v in toggles.items() if v)
            out['state'] = {'MOUNTED': 'worn', 'STANDBY': 'standby', 'ASLEEP': 'asleep'}.get(out['vr'], (out['vr'] or '?').lower())
        except Exception as e:
            out['state'] = f'error: {str(e)[:120]}'
        return out

    def watch(self, period):
        while True:
            p = self.poll()
            good = bool(p.get('vr')) and bool(p.get('toggles'))
            if good or not self.last.get('vr'):
                self.last = p
            else:  # a partial answer (adb reconnecting, an install in progress): keep the last good reading
                self.last = dict(self.last, retry_at=p['checked'], retry_state=p.get('state'))
            time.sleep(period if good else 15)


# ---- events to the pages

class Hub:
    def __init__(self):
        self.lock, self.subs = threading.Lock(), {}

    def subscribe(self, screen):
        q = queue.Queue(64)
        with self.lock:
            self.subs.setdefault(screen, []).append(q)
        return q

    def unsubscribe(self, screen, q):
        with self.lock:
            if q in self.subs.get(screen, []):
                self.subs[screen].remove(q)

    def count(self, screen):
        with self.lock:
            return len(self.subs.get(screen, []))

    def send(self, screen, event, data=None):
        with self.lock:
            qs = list(self.subs.get(screen, []))
        for q in qs:
            try:
                q.put_nowait((event, data or {}))
            except queue.Full:
                pass
        return len(qs)


# ---- the server

class State:
    def __init__(self, a):
        os.makedirs(a.data, exist_ok=True)
        os.makedirs(os.path.join(a.data, 'store'), exist_ok=True)
        self.repo = Repo(a.repo, push=a.push)
        self.house = House(a.house, os.path.join(a.data, 'house-cache.json'), by=a.house_by)
        self.hub = Hub()
        self.sdk = os.path.join(a.repo, 'sdk')
        self.data = a.data
        self.ask_url = a.ask_url or os.environ.get('PINPIN_ASSISTANT_URL', '')
        self.headset = Headset(a.adb, a.headset_serial)
        self._assistant = None
        self.proxy_cache, self.proxy_lock = {}, threading.Lock()
        try:
            with open(os.path.join(a.data, 'secrets.json')) as f:
                self.secrets = json.load(f)  # {"host": {"headers": {...}, "query": {...}}}, never in git
        except (OSError, ValueError):
            self.secrets = {}
        # urls opened on screens by the assistant (POST /api/screens/<id>/open): {screen: {url, seq}}; the runner
        # navigates the screen's window there and reports the seq it shows (POST /api/screens/<id>/shown)
        self.opens_path = os.path.join(a.data, 'opens.json')
        try:
            with open(self.opens_path) as f:
                self.opens = {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError):
            self.opens = {}
        self.shown = {}
        self.unauth = {}
        self.opens_lock = threading.Lock()
        self.runner_url = a.runner or ''  # set by the runner's own polls (X-PinPin-Runner) unless given
        if not self.runner_url:  # or a fixed address in <data>/runner-url (until the runner sends the token)
            try:
                with open(os.path.join(a.data, 'runner-url')) as f:
                    self.runner_url = f.readline().strip()
            except OSError:
                pass
        try:
            with open(os.path.join(a.data, 'proxy-private.json')) as f:
                self.private_ok = set(json.load(f))
        except (OSError, ValueError):
            self.private_ok = set()

    # ---- app stores (pinpin.store): <data>/store/<app>.json, its previous version kept as <app>.json.1
    @staticmethod
    def etag(body):
        return hashlib.sha256(body).hexdigest()[:16]

    def write_store(self, app_id, raw, if_match=None):
        """Returns (True, new etag), or (False, (current body, its etag)) when if_match is not the current version:
        then another page saved meanwhile, and the writer must merge its own changes onto the current copy."""
        path = os.path.join(self.data, 'store', app_id + '.json')
        with self.opens_lock:
            if if_match is not None:
                cur_body, _ = self.read_store(app_id)
                if self.etag(cur_body) != if_match:
                    return False, (cur_body, self.etag(cur_body))
            if os.path.exists(path):
                try:
                    with open(path, 'rb') as f:
                        cur = f.read()
                    json.loads(cur)  # only a good version becomes the fallback
                    if cur != raw:
                        atomic_write(path + '.1', cur)
                except (OSError, ValueError):
                    pass
            atomic_write(path, raw)
            return True, self.etag(raw)

    def read_store(self, app_id):
        """(body, recovered): the store, or its previous version if the current one is unreadable, or {} if neither."""
        path = os.path.join(self.data, 'store', app_id + '.json')
        for i, p in enumerate((path, path + '.1')):
            try:
                with open(p, 'rb') as f:
                    b = f.read()
                json.loads(b)
                if i:
                    log(f'store {app_id}: current file unreadable, served the previous version')
                return b, bool(i)
            except FileNotFoundError:
                if i == 0 and not os.path.exists(path + '.1'):
                    return b'{}', False
            except (OSError, ValueError):
                continue
        return b'{}', True

    def public_host(self, hostname):
        """True when every address the name resolves to is on the internet (not loopback, the LAN or link-local), or
        the host is explicitly allowed in <data>/proxy-private.json (["host", ...]; the tests use it)."""
        if hostname in self.private_ok:
            return True
        try:
            addrs = {i[4][0] for i in socket.getaddrinfo(hostname, None)}
        except OSError:
            return True  # unresolvable: the fetch itself will fail
        return all(ipaddress.ip_address(a.split('%')[0]).is_global for a in addrs)

    def set_open(self, screen, url):
        with self.opens_lock:
            seq = int(time.time() * 1000)
            if url:
                self.opens[screen] = {'url': url, 'seq': seq}
            elif self.opens.pop(screen, None) is None:
                return None
            atomic_write(self.opens_path, json.dumps(self.opens))
            return seq

    @property
    def assistant(self):
        """--ask-url, else the first line of <data>/assistant-url (re-read on every call, so it can be switched live)."""
        if self._assistant is not None:  # set by the tests
            return self._assistant
        url = self.ask_url
        if not url:
            try:
                with open(os.path.join(self.data, 'assistant-url')) as f:
                    url = f.readline().strip()
            except OSError:
                url = ''
        return make_assistant(url)

    @assistant.setter
    def assistant(self, v):
        self._assistant = v

    # ---- app-to-app messages: a per-app inbox on the Mini. Only host pages post (they stamp their own app id); the
    # sender's manifest must list the target in permissions.send and the target must accept the kind. Messages wait
    # until the target app is open somewhere and its handler acknowledges them.
    MAX_INBOX, MAX_MSG = 100, 16 << 10

    def inbox_path(self, app_id):
        d = os.path.join(self.data, 'inbox'); os.makedirs(d, exist_ok=True)
        return os.path.join(d, app_id + '.json')

    def inbox(self, app_id):
        try:
            with open(self.inbox_path(app_id)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def inbox_write(self, app_id, msgs):
        atomic_write(self.inbox_path(app_id), json.dumps(msgs))

    def post_message(self, sender, target, kind, data, dry=False):
        """'' when queued, else why not."""
        sm, tm = self.repo.manifest(sender), self.repo.manifest(target)
        if sm is None or tm is None:
            return f'no app {sender if sm is None else target!r}'
        if target not in (sm.get('permissions') or {}).get('send', []):
            return f'{sender} may not message {target} (its manifest permissions.send)'
        if kind not in (tm.get('accepts') or []):
            return f'{target} does not accept "{kind}" messages (accepts {tm.get("accepts", [])})'
        if len(json.dumps(data)) > self.MAX_MSG:
            return 'message over 16 KB'
        if dry:  # a preview or render check: everything checked, nothing delivered
            return ''
        with self.opens_lock:
            msgs = self.inbox(target)
            if len(msgs) >= self.MAX_INBOX:  # never drop an unread message to make room
                return f'{target} inbox is full ({self.MAX_INBOX} unread): open {target} so it can catch up'
            import uuid
            msgs.append({'id': uuid.uuid4().hex, 'from': sender, 'kind': kind, 'data': data, 'at': now()})
            self.inbox_write(target, msgs)
        for s in self.screens_showing(target):
            self.hub.send(str(s), 'message', {'app': target})
        self.hub.send('preview:' + target, 'message', {'app': target})
        return ''

    def claim_messages(self, app_id, who, hold=30):
        """Unclaimed (or expired) messages, now claimed by `who` for `hold` seconds; a crashed claimer's come back."""
        with self.opens_lock:
            msgs, t, out = self.inbox(app_id), time.time(), []
            for m in msgs:
                c = m.get('claim') or {}
                if c.get('by') in (None, who) or c.get('until', 0) < t:
                    m['claim'] = {'by': who, 'until': t + hold}
                    out.append({k: v for k, v in m.items() if k != 'claim'})
            if out:
                self.inbox_write(app_id, msgs)
            return out

    def ack_messages(self, app_id, ids):
        with self.opens_lock:
            msgs = self.inbox(app_id)
            keep = [m for m in msgs if m['id'] not in set(ids)]
            self.inbox_write(app_id, keep)
            return len(msgs) - len(keep)

    def token(self):
        try:
            with open(os.path.join(self.data, 'token')) as f:
                return f.read().strip()
        except OSError:
            return ''

    def enforce_after(self):
        try:
            with open(os.path.join(self.data, 'auth.json')) as f:
                return float(json.load(f).get('enforce_after', 0))
        except (OSError, ValueError):
            return 0.0

    def note_unauth(self, method, path, ip, what):
        key = (method, re.sub(r'/\d+/', '/N/', path), ip)
        n = self.unauth.get(key, 0) + 1
        self.unauth[key] = n
        if n == 1 or n % 100 == 0:  # first sighting, then every 100th: small log
            try:
                with open(os.path.join(self.data, 'unauth.log'), 'a') as f:
                    f.write(f'{now()} {what}: {method} {key[1]} from {ip} (x{n})\n')
            except OSError:
                pass

    def build_url(self):
        """The assistant's app-building endpoint: <data>/build-url if set, else /api/build next to its /api/ask."""
        try:
            with open(os.path.join(self.data, 'build-url')) as f:
                u = f.readline().strip()
                if u:
                    return u
        except OSError:
            pass
        a = self.assistant
        return a.url.rsplit('/api/ask', 1)[0] + '/api/build' if isinstance(a, HttpAssistant) else ''

    def screens_showing(self, app_id):
        return [s for s, a in self.house.mapping().items() if a == app_id]

    def reload_app(self, app_id):
        for s in self.screens_showing(app_id):
            self.hub.send(str(s), 'reload', {'why': f'{app_id} updated'})
        self.hub.send('preview:' + app_id, 'reload', {'why': 'updated'})

    def watch_house(self, period=3.0):
        last = self.house.mapping()
        while True:
            try:
                cur = self.house.mapping(self.house.fetch())
                for s in set(last) | set(cur):
                    if last.get(s) != cur.get(s):
                        log(f'screen {s}: app {last.get(s)} -> {cur.get(s)}')
                        self.hub.send(str(s), 'reload', {'why': 'app changed'})
                last = cur
            except Exception as e:  # house down: keep the cached mapping
                log('house poll:', e)
            time.sleep(period)


IDLE = """<!doctype html><html><head><meta charset="utf-8"><title>Screen {screen}</title></head>
<body><main class="pp-center"><div class="pp-huge">Screen {screen}</div>
<p class="pp-muted">No app here yet. Ask Claude for one, e.g. &ldquo;a recipe app for the kitchen&rdquo;.</p></main></body></html>"""


def icon_data_uri(repo, app_id, m):
    """The app's icon as a data: URI (manifest "icon"), else a generated letter tile, so the catalog always has one."""
    import base64
    name = m.get('icon')
    if isinstance(name, str) and not manifest.check_path(name):
        try:
            with open(os.path.join(repo.apps, app_id, name), 'rb') as f:
                b = f.read()
            kind = 'image/svg+xml' if name.lower().endswith('.svg') else 'image/png'
            return f'data:{kind};base64,' + base64.b64encode(b).decode()
        except OSError:
            pass
    letter = (m.get('name') or app_id)[:1].upper()
    hue = int(hashlib.sha1(app_id.encode()).hexdigest()[:2], 16) * 360 // 256
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" '
           f'fill="hsl({hue},45%,32%)"/><text x="32" y="44" font-family="Helvetica,Arial" font-size="34" font-weight="700" '
           f'text-anchor="middle" fill="#fff">{letter}</text></svg>')
    return 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode()


def icon_png(st, app_id, size):
    """The icon as a PNG for clients that can't draw SVG (the headset), rendered once per app commit by headless Chrome
    on this machine and cached in <data>/icons."""
    import base64
    size = max(32, min(512, size))
    m = st.repo.manifest(app_id) or {}
    uri = icon_data_uri(st.repo, app_id, m)
    if uri.startswith('data:image/png'):
        return base64.b64decode(uri.split(',', 1)[1])
    d = os.path.join(st.data, 'icons'); os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f'{app_id}-{(st.repo.head(app_id) or "x")[:12]}-{size}.png')
    if not os.path.exists(path):
        import render_check as rc
        if not os.path.exists(rc.CHROME):
            return None
        html = os.path.join(d, f'{app_id}.html')
        with open(html, 'w') as f:
            f.write(f'<html><body style="margin:0;background:transparent"><img src="{uri}" width="{size}" height="{size}"></body></html>')
        prof = tempfile.mkdtemp(prefix='pp-icon-')
        try:
            subprocess.run([rc.CHROME, '--headless=new', '--hide-scrollbars', '--default-background-color=00000000',
                            f'--window-size={size},{size}', f'--user-data-dir={prof}', '--no-first-run', '--use-mock-keychain',
                            f'--screenshot={path}.tmp.png', 'file://' + html], capture_output=True, timeout=40)
            os.replace(path + '.tmp.png', path)
        except Exception as e:
            log(f'icon {app_id}: {e}')
            return None
        finally:
            shutil.rmtree(prof, ignore_errors=True)
    with open(path, 'rb') as f:
        return f.read()


THUMB_TTL = 600
_thumb_lock = threading.Lock()


def thumb_png(st, app_id, port):
    """A 960x600 screenshot of the app's preview (it shows the app's current data), by headless Chrome on this machine;
    cached per commit and redone after THUMB_TTL so lists and notes stay roughly current."""
    import render_check as rc
    if not os.path.exists(rc.CHROME):
        return None
    d = os.path.join(st.data, 'thumbs'); os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f'{app_id}-{(st.repo.head(app_id) or "x")[:12]}.png')
    with _thumb_lock:  # one Chrome at a time: the Mini also renders and streams
        if not (os.path.exists(path) and time.time() - os.path.getmtime(path) < THUMB_TTL):
            tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'render_check.py')
            try:
                run_group([sys.executable, tool, f'http://127.0.0.1:{port}/app/{app_id}/', '--wait', '3',
                           '--size', '960x600', '--shot', path + '.tmp'], timeout=60)
                os.replace(path + '.tmp', path)
            except Exception as e:
                log(f'thumb {app_id}: {e}')
                if not os.path.exists(path):
                    return None
    with open(path, 'rb') as f:
        return f.read()


def run_group(args, timeout):
    """subprocess.run whose whole process group dies on timeout (render_check starts Chrome; killing only the
    python child would leave that Chrome and its profile behind)."""
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, p.returncode, out, err)
    except subprocess.TimeoutExpired:
        try: os.killpg(p.pid, signal.SIGKILL)
        except OSError: pass
        p.communicate()
        raise


def render_check(port, app_id, say):
    """tools/render_check.py on this machine (needs Chrome): JS errors and the visible text of the app's preview."""
    tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'render_check.py')
    import render_check as rc  # noqa: E402  (tools/ is on the path, see below)
    if not os.path.exists(rc.CHROME):
        return {'skipped': 'no Chrome on this machine'}
    args = [sys.executable, tool, f'http://127.0.0.1:{port}/app/{app_id}/', '--wait', '2.5']
    for t in say:
        args += ['--say', t]
    try:
        r = run_group(args, timeout=90)
        return json.loads(r.stdout)
    except Exception as e:
        return {'error': f'render check failed: {e}'}


SHELL = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/sdk/host.css"></head>
<body><iframe id="app" sandbox="allow-scripts" allow="autoplay" title="{title}"></iframe>
<script src="/sdk/host.js" data-app="{app}" data-screen="{screen}"></script></body></html>"""

# the frame's own CSP: even opened directly (not in the shell), an app page is sandboxed to an opaque origin
FRAME_CSP = "sandbox allow-scripts; default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'none'"


def inject(html, app_id, screen):
    tag = (f'<base href="/apps/{app_id}/"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<link rel="stylesheet" href="/sdk/pinpin.css">'
           f'<script src="/sdk/pinpin.js" data-app="{app_id}" data-screen="{screen}"></script>')
    m = re.search(r'<head[^>]*>', html, re.I)
    return html[:m.end()] + tag + html[m.end():] if m else tag + html


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


def html_escape(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def handle_one_request(self):
        # an unexpected error answers 500 instead of dropping the connection silently (and is logged)
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as e:
            log(f'request {getattr(self, "command", "?")} {getattr(self, "path", "?")}: {type(e).__name__}: {e}')
            try:
                self.send(500, {'error': f'{type(e).__name__}'})
            except Exception:
                pass
    st = None  # State
    timeout = 30  # seconds per socket read/write: a slow or stalled upload is dropped, never holds a thread forever

    def log_message(self, fmt, *a):
        if os.environ.get('PINPIN_APPS_VERBOSE'):
            log(self.address_string(), fmt % a)

    def send(self, code, obj=None, body=None, ctype='application/json', headers=None):
        if body is None:
            body = json.dumps(obj, indent=1).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')  # JSON is never run as a script by a curious app
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def has_token(self):
        """True when the request carries the token, or no token is configured (the tests); never logs, never grace."""
        tok = self.st.token()
        if not tok:
            return True
        got = (self.headers.get('Authorization') or '').removeprefix('Bearer ').strip() or (self.headers.get('X-PinPin-Token') or '').strip()
        return bool(got) and hmac.compare_digest(got.encode(), tok.encode())

    def authed(self):
        """Powerful calls (input, eval, assign, open, build, app writes) need the shared token: <data>/token, sent as
        "Authorization: Bearer <token>" or "X-PinPin-Token". Until <data>/auth.json's enforce_after (the rollout grace
        hour) a call without it is still served and logged in <data>/unauth.log, so missing callers show up first."""
        st = self.st
        tok = st.token()
        if not tok:
            return True
        got = (self.headers.get('Authorization') or '').removeprefix('Bearer ').strip() or (self.headers.get('X-PinPin-Token') or '').strip()
        if got and hmac.compare_digest(got.encode(), tok.encode()):
            return True
        grace = time.time() < st.enforce_after()
        st.note_unauth(self.command, urllib.parse.urlparse(self.path).path, self.client_address[0], 'served (grace)' if grace else 'refused')
        if grace:
            return True
        self.send(401, {'error': 'token required (Authorization: Bearer <token>)'}, headers={'WWW-Authenticate': 'Bearer'})
        return False

    def from_host(self):
        """App frames (opaque origin) may not call the API themselves: they must ask their host page. A request from a
        sandboxed frame carries Origin: null; one without our X-PinPin header can't come from host.js."""
        if self.headers.get('Origin') == 'null':
            self.send(403, {'error': 'apps call the API through their host page (pinpin.*), not directly'}); return False
        if self.headers.get('X-PinPin') != '1':
            self.send(403, {'error': 'X-PinPin: 1 required'}); return False
        return True

    def writable(self):
        """Every write: JSON only (a cross-origin form or no-cors fetch can't send that without a preflight we never
        answer) and never from an app frame."""
        if self.headers.get('Origin') == 'null':
            self.send(403, {'error': 'apps can not call the API directly'}); return False
        if not (self.headers.get('Content-Type') or '').startswith('application/json'):
            self.send(415, {'error': 'Content-Type: application/json required'}); return False
        return True

    def json_body(self):
        if not self.writable():
            return None
        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_BODY:
            self.send(413, {'error': 'body too large'}); return None
        try:
            d = json.loads(self.rfile.read(n) or b'{}')
        except ValueError as e:
            self.send(400, {'error': f'not JSON: {e}'}); return None
        if not isinstance(d, dict):
            self.send(400, {'error': 'body must be an object'}); return None
        return d

    def file(self, path, sandbox=False):
        try:
            with open(path, 'rb') as f:
                b = f.read()
        except OSError:
            return self.send(404, {'error': 'not found'})
        ext = os.path.splitext(path)[1].lower()
        # an app's raw .html/.svg opened directly must not run with this origin (the control page's token lives here)
        hdr = {'Content-Security-Policy': FRAME_CSP} if sandbox and ext in ('.html', '.svg') else None
        self.send(200, body=b, ctype=MIME.get(ext, 'application/octet-stream'), headers=hdr)

    def page(self, app_id, screen):
        """The trusted shell: host.js plus the app in a sandboxed iframe (/frame/<app>/)."""
        st = self.st
        if app_id is None or not os.path.isfile(os.path.join(st.repo.apps, app_id, 'index.html')):
            app_id = '_idle'
        title = (st.repo.manifest(app_id) or {}).get('name', f'Screen {screen}') if app_id != '_idle' else f'Screen {screen}'
        html = SHELL.format(title=html_escape(str(title)), app=app_id, screen=html_escape(screen))
        self.send(200, body=html.encode(), ctype=MIME['.html'])

    def frame(self, app_id, screen):
        """The app document itself, SDK injected, served sandboxed (CSP) so it never runs with this server's origin."""
        st = self.st
        if app_id == '_idle':
            html = inject(IDLE.format(screen=html_escape(screen)), '_idle', html_escape(screen))
        else:
            try:
                with open(os.path.join(st.repo.apps, app_id, 'index.html'), encoding='utf-8', errors='replace') as f:
                    html = inject(f.read(), app_id, html_escape(screen))
            except OSError:
                return self.send(404, {'error': 'not found'})
        self.send(200, body=html.encode(), ctype=MIME['.html'], headers={'Content-Security-Policy': FRAME_CSP})

    # -- GET
    def do_GET(self):
        st = self.st
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        parts = [urllib.parse.unquote(p) for p in u.path.split('/') if p]
        try:
            if u.path == '/health':
                return self.send(200, {'ok': True, 'apps': len(st.repo.ids()), 'commit': st.repo.head(),
                                       'assistant': st.assistant.name, 'house_rev': (st.house.doc or {}).get('rev')})
            if u.path in ('/', '/index.html'):
                return self.file(os.path.join(st.repo.root, 'ui', 'index.html'))
            if u.path in ('/help', '/help/'):
                return self.file(os.path.join(st.repo.root, 'ui', 'help.html'))
            if u.path in ('/status', '/status/'):
                return self.file(os.path.join(st.repo.root, 'ui', 'status.html'))
            if u.path == '/api/status':
                return self.send(200, self.status())
            if u.path == '/house-plan.svg':  # the house server's floor plan, relayed (phones reach this port only)
                try:
                    with urllib.request.urlopen(st.house.url + '/house/plan.svg', timeout=5) as r:
                        return self.send(200, body=r.read(), ctype='image/svg+xml')
                except Exception as e:
                    return self.send(502, {'error': f'house plan: {e}'})
            if u.path == '/api/auth':  # rollout state, for the agents wiring the token
                ea = st.enforce_after()
                return self.send(200, {'token_set': bool(st.token()), 'enforce_after': ea,
                                       'enforced': bool(st.token()) and time.time() >= ea,
                                       'unauthenticated': [{'method': k[0], 'path': k[1], 'from': k[2], 'count': v} for k, v in st.unauth.items()]})
            if u.path == '/api/authoring':  # docs/AUTHORING.md, live, for the assistant's context
                return self.file(os.path.join(st.repo.root, 'docs', 'AUTHORING.md'))
            if u.path == '/favicon.ico':
                return self.send(204, body=b'')
            if parts[:1] == ['sdk'] and len(parts) == 2 and not manifest.check_path(parts[1]):
                return self.file(os.path.join(st.sdk, parts[1]))
            if parts[:1] == ['apps'] and len(parts) >= 3 and manifest.check_id(parts[1]):
                rel = '/'.join(parts[2:])
                if manifest.check_path(rel):
                    return self.send(404, {'error': 'not found'})
                return self.file(os.path.join(st.repo.apps, parts[1], *parts[2:]), sandbox=True)
            if parts[:1] == ['app'] and len(parts) == 2 and manifest.check_id(parts[1]):
                if not u.path.endswith('/'):
                    return self.send(302, {}, headers={'Location': f'/app/{parts[1]}/' + (f'?{u.query}' if u.query else '')})
                return self.page(parts[1], 'preview:' + parts[1])
            if parts[:1] == ['frame'] and len(parts) == 2 and (manifest.check_id(parts[1]) or parts[1] == '_idle'):
                return self.frame(parts[1], q.get('screen', '')[:80])
            if parts[:1] == ['screen'] and len(parts) == 2 and (parts[1].isascii() and parts[1].isdecimal()):
                return self.page(st.house.mapping().get(int(parts[1])), parts[1])
            if u.path == '/events':
                return self.events(q.get('screen', ''))
            if u.path == '/proxy':
                if not self.from_host():
                    return
                return self.proxy(q.get('app', ''), q.get('url', ''))
            if parts == ['api', 'apps']:
                out = []
                for i in st.repo.ids():
                    m = st.repo.manifest(i) or {}
                    if m.get('hidden') and 'all' not in q:  # test apps (zz-probe) stay out of pickers
                        continue
                    out.append({'id': i, 'name': m.get('name'), 'description': m.get('description'), 'size': m.get('size'),
                                'permissions': m.get('permissions'), 'screens': st.screens_showing(i),
                                'icon': icon_data_uri(st.repo, i, m), 'icon_png': f'/api/apps/{i}/icon.png',
                                'commit': st.repo.head(i)[:12], 'last': (st.repo.history(i, 1) or [None])[0]})
                return self.send(200, out)
            if parts[:2] == ['api', 'apps'] and len(parts) >= 3 and manifest.check_id(parts[2]):
                a = parts[2]
                if a not in st.repo.ids():
                    return self.send(404, {'error': f'no app {a}'})
                if len(parts) == 3:
                    files = {p: b.decode('utf-8', 'replace') for p, b in st.repo.files(a).items()
                             if os.path.splitext(p)[1] in ('.html', '.js', '.css', '.json', '.svg', '.txt', '.md')}
                    return self.send(200, {'id': a, 'manifest': st.repo.manifest(a), 'files': files,
                                           'commit': st.repo.head(a), 'screens': st.screens_showing(a)})
                if parts[3:] == ['history']:
                    return self.send(200, st.repo.history(a))
                if parts[3:] == ['inbox']:
                    if not self.from_host():
                        return
                    if q.get('claim'):  # a screen takes its messages for 30 s, so two screens don't both apply one
                        return self.send(200, st.claim_messages(a, q['claim'][:40]))
                    return self.send(200, st.inbox(a))
                if parts[3:] == ['thumb.png']:
                    png = thumb_png(st, a, self.server.server_address[1])
                    return self.send(200, body=png, ctype='image/png') if png else self.send(404, {'error': 'no thumbnail (no Chrome here)'})
                if parts[3:] == ['icon.png']:
                    sz = q.get('size', '256')
                    png = icon_png(st, a, int(sz) if sz.isascii() and sz.isdecimal() else 256)
                    return self.send(200, body=png, ctype='image/png') if png else self.send(404, {'error': 'no icon'})
                if parts[3:] == ['data']:
                    if not self.from_host():
                        return
                    body, recovered = st.read_store(a)
                    h = {'ETag': st.etag(body)}
                    if recovered:
                        h['X-Store-Recovered'] = '1'
                    return self.send(200, body=body, headers=h)
            if parts == ['api', 'screens']:
                rp = self.headers.get('X-PinPin-Runner', '')
                if rp.isascii() and rp.isdecimal() and 0 < int(rp) < 65536 and self.has_token():
                    # the laptop runner polls with its input port: that's where input (and the token) goes. Only an
                    # authenticated runner may set it, or any LAN device could redirect input and collect the token.
                    st.runner_url = f'http://{self.client_address[0]}:{int(rp)}'
                return self.send(200, self.screen_list())
            return self.send(404, {'error': 'not found'})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def status(self):
        """The morning status page's data: the VPS bundle (tools/status_push.sh), the headset, screens and apps."""
        st = self.st
        p = os.path.join(st.data, 'status', 'vps.json')
        try:
            with open(p) as f:
                vps = json.load(f)
            vps['age_s'] = round(time.time() - os.path.getmtime(p))
        except (OSError, ValueError):
            vps = None
        names = {i: (st.repo.manifest(i) or {}).get('name', i) for i in st.repo.ids()}
        try:  # tools/cursor_watch.py: when the Mini's cursor left the streamed display (the MINI click bug)
            with open(os.path.join(st.data, 'cursor-watch.log'), errors='replace') as f:
                cursor = [l.rstrip() for l in f.readlines()[-6:]]
        except OSError:
            cursor = []
        checks = []
        cd = os.path.join(st.data, 'status', 'components')
        for fn in sorted(os.listdir(cd)) if os.path.isdir(cd) else []:
            if fn.endswith('.json'):
                try:
                    with open(os.path.join(cd, fn)) as f:
                        checks.append(json.load(f))
                except (OSError, ValueError):
                    pass
        return {'now': now(), 'vps': vps, 'headset': st.headset.last, 'screens': self.screen_list(), 'apps': names, 'cursor': cursor,
                'checks': checks,
                'house_rev': (st.house.doc or {}).get('rev'), 'assistant': st.assistant.name}

    def screen_list(self):
        st = self.st
        mp, out, seen = st.house.mapping(), [], set()
        for s in st.house.screens():
            sid = s.get('id')
            src = s.get('source') or {}
            out.append({'screen': sid, 'app': mp.get(sid), 'role': s.get('role'), 'on': s.get('on', 1),
                        'display': src.get('screen') if src.get('kind') == 'mac-display' else None,
                        'w_m': s.get('w'), 'h_m': s.get('h'), 'clients': st.hub.count(str(sid))})
            seen.add(sid)
        for sid in sorted(set(mp) | set(st.opens)):  # assigned or opened before the headset placed it
            if sid not in seen:
                out.append({'screen': sid, 'app': mp.get(sid), 'role': None, 'on': 0, 'display': sid, 'clients': st.hub.count(str(sid))})
        for r in out:
            o = st.opens.get(r['screen'])
            r['url'], r['seq'] = (o['url'], o['seq']) if o else (None, 0)
            r['shown_seq'] = st.shown.get(r['screen'], 0)
        return sorted(out, key=lambda r: r['screen'] if isinstance(r['screen'], int) else 0)

    def events(self, screen):
        st = self.st
        if not screen:
            return self.send(400, {'error': 'screen= required'})
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        q = st.hub.subscribe(screen)
        try:
            self.wfile.write(b'retry: 2000\nevent: hello\ndata: {}\n\n'); self.wfile.flush()
            while True:
                try:
                    ev, data = q.get(timeout=15)
                    self.wfile.write(f'event: {ev}\ndata: {json.dumps(data)}\n\n'.encode())
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            st.hub.unsubscribe(screen, q)

    def proxy(self, app_id, url):
        st = self.st
        m = st.repo.manifest(app_id) if manifest.check_id(app_id) else None
        if m is None:
            return self.send(404, {'error': f'no app {app_id!r}'})
        p = urllib.parse.urlparse(url)
        try:
            p.port
        except ValueError:
            return self.send(400, {'error': 'bad port in url'})
        if p.scheme not in ('http', 'https') or not p.hostname:
            return self.send(400, {'error': 'url must be http(s)'})
        host = p.hostname + (f':{p.port}' if p.port else '')
        allowed = (m.get('permissions') or {}).get('fetch', [])
        if host not in allowed and p.hostname not in allowed:
            return self.send(403, {'error': f'{host} is not in {app_id} permissions.fetch {allowed}'})
        if p.username or p.password:
            return self.send(400, {'error': 'no user:password@ in proxied urls'})
        if not st.public_host(p.hostname):
            return self.send(403, {'error': f'{p.hostname} is on the home network or this machine; apps only reach the internet'})
        sec = st.secrets.get(p.hostname, {})
        if sec.get('query'):
            qs = urllib.parse.parse_qsl(p.query) + list(sec['query'].items())
            url = urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(qs)))
        key = url
        with st.proxy_lock:
            hit = st.proxy_cache.get(key)
        if hit and time.time() - hit[0] < PROXY_TTL:
            return self.send(hit[1], body=hit[2], ctype=hit[3], headers={'X-Proxy-Cache': 'hit'})
        req = urllib.request.Request(url, headers={'User-Agent': 'pinpin-apps/1', **sec.get('headers', {})})
        try:
            with NO_REDIRECT.open(req, timeout=10) as r:  # a redirect is returned, never followed (it could point anywhere)
                body, code, ctype = r.read(MAX_PROXY + 1), r.status, r.headers.get('Content-Type', 'application/octet-stream')
        except urllib.error.HTTPError as e:
            body, code, ctype = e.read(MAX_PROXY + 1), e.code, e.headers.get('Content-Type', 'text/plain')
        except Exception as e:
            if hit:  # upstream down: serve the stale copy rather than nothing
                return self.send(hit[1], body=hit[2], ctype=hit[3], headers={'X-Proxy-Cache': 'stale'})
            return self.send(502, {'error': f'upstream: {e}'})
        if len(body) > MAX_PROXY:
            return self.send(502, {'error': 'upstream response too large'})
        if code == 200:
            with st.proxy_lock:
                if len(st.proxy_cache) > 500:
                    st.proxy_cache.clear()
                st.proxy_cache[key] = (time.time(), code, body, ctype)
        self.send(code, body=body, ctype=ctype, headers={'X-Proxy-Cache': 'miss'})

    # -- writes
    def do_PUT(self):
        st = self.st
        parts = [p for p in urllib.parse.urlparse(self.path).path.split('/') if p]
        if parts[:2] != ['api', 'apps'] or len(parts) < 3 or not manifest.check_id(parts[2]):
            return self.send(404, {'error': 'not found'})
        a = parts[2]
        if parts[3:] != ['data'] and not self.authed():  # writing an app (the host's own data PUT is host-scoped)
            return
        if parts[3:] == ['data']:
            if not self.writable() or not self.from_host():
                return
            n = int(self.headers.get('Content-Length') or 0)
            if n > MAX_STORE:
                return self.send(413, {'error': f'app data over {MAX_STORE} bytes'})
            raw = self.rfile.read(n)
            try:
                json.loads(raw)
            except ValueError:
                return self.send(400, {'error': 'not JSON'})
            if a not in st.repo.ids():
                return self.send(404, {'error': f'no app {a}'})
            im = self.headers.get('If-Match')
            ok, info = st.write_store(a, raw, im.strip().strip('"') if im else None)
            if not ok:  # another page saved first: here is the current copy, merge and retry
                return self.send(409, body=info[0], headers={'ETag': info[1]})
            for sc in st.screens_showing(a):  # the other pages showing this app pick up the new copy
                st.hub.send(str(sc), 'store', {'app': a, 'etag': info})
            return self.send(200, {'ok': True, 'etag': info}, headers={'ETag': info})
        if parts[3:]:
            return self.send(404, {'error': 'not found'})
        d = self.json_body()
        if d is None:
            return
        try:
            problems, commit, changed = st.repo.write(a, d.get('files'), str(d.get('message', ''))[:200],
                                                     bool(d.get('replace')), str(d.get('by', 'assistant'))[:40],
                                                     create=bool(d.get('create')), base=str(d['base']) if d.get('base') else None)
        except Conflict as e:
            return self.send(409, {'error': str(e), 'commit': st.repo.head(a)})
        except Exception as e:
            return self.send(500, {'error': f'write failed, the app was left as it was: {e}'})
        if problems:
            return self.send(400, {'error': 'invalid app', 'problems': problems})
        if changed:
            log(f'{a}: commit {commit[:10]}')
            st.reload_app(a)
        out = {'app': a, 'commit': commit, 'changed': changed, 'screens': st.screens_showing(a), 'preview': f'/app/{a}/'}
        if d.get('check'):
            out['check'] = render_check(self.server.server_address[1], a, [str(x) for x in d.get('say', [])][:5])
        self.send(200, out)

    def do_DELETE(self):
        st = self.st
        if self.headers.get('Origin') == 'null':
            return self.send(403, {'error': 'apps can not call the API directly'})
        if not self.authed():
            return
        parts = [p for p in urllib.parse.urlparse(self.path).path.split('/') if p]
        if parts[:2] != ['api', 'apps'] or len(parts) != 3 or parts[2] not in st.repo.ids():
            return self.send(404, {'error': 'not found'})
        a = parts[2]
        try:
            for s in st.screens_showing(a):
                st.house.assign(s, None)
                st.hub.send(str(s), 'reload', {'why': f'{a} removed'})
            commit, _ = st.repo.remove(a, urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('message', [''])[0])
        except Exception as e:
            return self.send(500, {'error': str(e)})
        self.send(200, {'app': a, 'commit': commit, 'removed': True})

    def do_POST(self):
        st = self.st
        parts = [p for p in urllib.parse.urlparse(self.path).path.split('/') if p]
        d = self.json_body()
        if d is None:
            return
        if parts == ['api', 'ask']:
            if not self.from_host():
                return
            if not (isinstance(d.get('prompt'), str) and d['prompt'].strip()):
                return self.send(400, {'error': 'prompt required'})
            m = st.repo.manifest(str(d.get('app', ''))) if manifest.check_id(str(d.get('app', ''))) else None
            if m is not None and not (m.get('permissions') or {}).get('ask'):
                return self.send(403, {'error': f'{d["app"]} has no ask permission'})
            try:
                return self.send(200, st.assistant.ask({k: d[k] for k in ('app', 'screen', 'prompt', 'system', 'json', 'model') if k in d}))
            except Exception as e:
                return self.send(502, {'error': f'assistant: {e}', 'stub': True})
        if parts[:2] == ['api', 'apps'] and len(parts) in (4, 5) and parts[3] == 'inbox' and manifest.check_id(parts[2]):
            if not self.from_host():
                return
            if len(parts) == 5 and parts[4] == 'ack':  # the target's host: these were handled
                ids = d.get('ids')
                if not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
                    return self.send(400, {'error': 'ids: a list'})
                return self.send(200, {'acked': st.ack_messages(parts[2], ids)})
            sender, kind = d.get('from'), d.get('kind')
            if not (isinstance(sender, str) and manifest.check_id(sender) and isinstance(kind, str)):
                return self.send(400, {'error': 'from (the sending app, set by its host) and kind required'})
            dry = bool(d.get('dry'))
            err = st.post_message(sender, parts[2], kind, d.get('data'), dry)
            return self.send(403 if 'may not' in err or 'accept' in err else 404 if err.startswith('no app') else
                             429 if 'inbox is full' in err else 400 if err else 200,
                             {'error': err} if err else {'queued': not dry, 'dry': dry})
        protected = parts == ['api', 'status', 'component'] or parts == ['api', 'build'] or (parts[:2] == ['api', 'apps'] and parts[3:] == ['rollback']) or \
            (parts[:2] == ['api', 'screens'] and len(parts) == 4 and parts[3] != 'shown')
        if protected and not self.authed():
            return
        if parts == ['api', 'status', 'component']:  # a one-line check result for /status (e.g. the nightly assistant eval)
            name = d.get('component')
            if not (isinstance(name, str) and re.match(r'^[a-z0-9][a-z0-9-]{0,40}$', name)) or not isinstance(d.get('text'), str):
                return self.send(400, {'error': 'component (lowercase id) and text required'})
            rec = {'component': name, 'text': d['text'][:300], 'ok': d.get('ok') if isinstance(d.get('ok'), bool) else None,
                   'link': d['link'][:300] if isinstance(d.get('link'), str) else None, 'at': now()}
            cd = os.path.join(st.data, 'status', 'components'); os.makedirs(cd, exist_ok=True)
            atomic_write(os.path.join(cd, name + '.json'), json.dumps(rec))
            return self.send(200, rec)
        if parts == ['api', 'build']:  # the control page's "ask Claude to build an app": the assistant runs its app tools
            prompt = d.get('prompt')
            if not (isinstance(prompt, str) and prompt.strip()):
                return self.send(400, {'error': 'prompt required'})
            if d.get('app') is not None and d['app'] not in st.repo.ids():
                return self.send(400, {'error': f'no app {d["app"]!r}'})
            url = st.build_url()
            if not url:
                return self.send(502, {'error': 'no assistant configured'})
            body = {'prompt': prompt.strip()[:4000], 'app': d.get('app'), 'screen': d.get('screen'), 'from': 'control-page'}
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST', headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=240) as r:
                    return self.send(r.status, body=r.read())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return self.send(502, {'error': 'the assistant does not build apps yet (no /api/build there)'})
                return self.send(e.code, body=e.read())
            except Exception as e:
                return self.send(502, {'error': f'assistant: {e}'})
        if parts[:2] == ['api', 'apps'] and len(parts) == 4 and parts[3] == 'rollback' and parts[2] in st.repo.ids():
            c = str(d.get('commit', ''))
            if not SHA.match(c):
                return self.send(400, {'error': 'commit: a git sha'})
            err, commit, changed = st.repo.rollback(parts[2], c, str(d.get('by', 'assistant'))[:40])
            if err:
                return self.send(400, {'error': err})
            if changed:
                st.reload_app(parts[2])
            return self.send(200, {'app': parts[2], 'commit': commit, 'changed': changed})
        if parts[:2] == ['api', 'screens'] and len(parts) == 4 and (parts[2].isascii() and parts[2].isdecimal()):
            sid, what = int(parts[2]), parts[3]
            if what == 'app':
                a = d.get('app')
                if a is not None and a not in st.repo.ids():
                    return self.send(400, {'error': f'no app {a!r}', 'apps': st.repo.ids()})
                try:
                    rev = st.house.assign(sid, a)
                except Exception as e:
                    return self.send(502, {'error': str(e)})
                st.set_open(sid, None)  # the screen goes back to showing its app
                st.hub.send(str(sid), 'reload', {'why': 'app changed'})
                return self.send(200, {'screen': sid, 'app': a, 'house_rev': rev})
            if what == 'say':
                t = d.get('text')
                if not (isinstance(t, str) and t.strip()):
                    return self.send(400, {'error': 'text required'})
                n = st.hub.send(str(sid), 'voice', {'text': t.strip()[:2000], 'final': d.get('final', True)})
                return self.send(200 if n else 404, {'screen': sid, 'delivered': n, 'app': st.house.mapping().get(sid)})
            if what == 'reload':
                return self.send(200, {'screen': sid, 'delivered': st.hub.send(str(sid), 'reload', {'why': 'asked'})})
            if what == 'open':
                url = d.get('url')
                if url is not None and not (isinstance(url, str) and len(url) < 4000 and
                                            urllib.parse.urlparse(url).scheme in ('http', 'https') and urllib.parse.urlparse(url).netloc):
                    return self.send(400, {'error': 'url: an http(s) URL, or null to return to the app'})
                seq = st.set_open(sid, url)
                return self.send(200, {'screen': sid, 'url': url, 'seq': seq or 0})
            if what in ('click', 'move', 'scroll', 'type', 'key', 'eval'):
                if not st.runner_url:
                    return self.send(502, {'error': 'the laptop runner has not checked in'})
                req = urllib.request.Request(f'{st.runner_url}/input/{sid}/{what}', data=json.dumps(d).encode(), method='POST',
                                             headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + st.token()})
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return self.send(r.status, body=r.read())
                except urllib.error.HTTPError as e:
                    return self.send(e.code, body=e.read())
                except Exception as e:
                    return self.send(502, {'error': f'laptop runner: {e}'})
            if what == 'shown':
                if not isinstance(d.get('seq'), int):
                    return self.send(400, {'error': 'seq: int'})
                st.shown[sid] = d['seq']
                return self.send(200, {'screen': sid, 'shown_seq': d['seq']})
        return self.send(404, {'error': 'not found'})


def main_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', default=os.path.expanduser('~/pinpin-apps'))
    ap.add_argument('--data', default=os.path.expanduser('~/pinpin-apps-data'))
    ap.add_argument('--house', default='http://127.0.0.1:57800')
    ap.add_argument('--house-by', default='apps')
    ap.add_argument('--ask-url', default='', help="the assistant's /api/ask (work-42); empty: the stub")
    ap.add_argument('--bind', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=57830)
    ap.add_argument('--push', action='store_true', help='git push after every commit')
    ap.add_argument('--poll', type=float, default=3.0)
    ap.add_argument('--adb', default='~/.local/share/pinpin-quest/platform-tools/adb', help='for the status page (read-only)')
    ap.add_argument('--headset-serial', default='127.0.0.1:26351')
    ap.add_argument('--headset-poll', type=float, default=60.0, help='seconds; 0: never ask the headset')
    ap.add_argument('--runner', default='', help='the laptop runner input URL; default: learned from its polls')
    ap.add_argument('--io-timeout', type=float, default=30.0, help='seconds a client may stall a request')
    return ap.parse_args(argv)


def serve(a):
    """The server (not started) and its state; the house watcher runs from here on."""
    st = State(a)
    try:
        st.house.fetch()
    except Exception as e:
        log('house not reachable at start, using the cached copy:', e)
    handler = type('H', (Handler,), {'st': st, 'timeout': a.io_timeout})
    srv = ThreadingHTTPServer((a.bind, a.port), handler)
    srv.daemon_threads = True
    threading.Thread(target=st.watch_house, args=(a.poll,), daemon=True).start()
    if a.headset_poll > 0:
        threading.Thread(target=st.headset.watch, args=(a.headset_poll,), daemon=True).start()
    return srv, st


def main():
    a = main_args()
    srv, st = serve(a)
    log(f'pinpin-apps on {a.bind}:{srv.server_address[1]}, repo {a.repo}, {len(st.repo.ids())} apps, assistant {st.assistant.name}')
    srv.serve_forever()


if __name__ == '__main__':
    main()
