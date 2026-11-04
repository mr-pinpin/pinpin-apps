#!/usr/bin/env python3
"""Offline tests for the apps server: a temp clone of this repo, the real house server from quest-wall-screen (its
validation covers the "apps" section) or, without that checkout, a minimal stand-in, and a local fake upstream for
the proxy. No network, no devices.   python3 server/test_apps.py"""
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import apps_server  # noqa: E402
import manifest  # noqa: E402

QWS_HOUSE = os.environ.get('QWS_HOUSE', '/root/work/quest-wall-screen/house')
REAL_HOUSE = os.path.isfile(os.path.join(QWS_HOUSE, 'server.py'))
if REAL_HOUSE:
    sys.path.insert(0, QWS_HOUSE)
    import server as house_server  # noqa: E402
    import schema as house_schema  # noqa: E402


def house_doc():
    p = os.path.join(QWS_HOUSE, 'house.json')
    if os.path.isfile(p):
        with open(p) as f:
            return json.load(f)
    return {'schema': 1, 'rev': 1, 'updated': '', 'by': 'migrate', 'nextScreenId': 4, 'rooms': [], 'links': [], 'walls': [],
            'shapes': [], 'screens': [{'id': 2, 'role': 'extra', 'source': {'kind': 'mac-display', 'screen': 2}}],
            'menu': {}, 'settings': {}}


class FakeHouse(BaseHTTPRequestHandler):
    """Only what the apps server uses: GET /house, PUT /house with If-Match (409 when stale)."""
    doc = None
    lock = threading.Lock()

    def log_message(self, *a): pass

    def do_GET(self):
        b = json.dumps(self.doc).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_PUT(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        with self.lock:
            if int(self.headers['If-Match']) != self.doc['rev'] or hashlib.sha256(body).hexdigest() != self.headers['X-House-SHA256']:
                code, out = 409, self.doc
            else:
                d = json.loads(body); d['rev'] = self.doc['rev'] + 1; type(self).doc = d; code, out = 200, {'rev': d['rev']}
        b = json.dumps(out).encode()
        self.send_response(code); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)


class Upstream(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *a): pass

    def do_GET(self):
        type(self).hits += 1
        if self.path == '/redirect':
            self.send_response(302); self.send_header('Location', 'http://10.9.9.9/secret'); self.send_header('Content-Length', '0')
            self.end_headers(); return
        b = json.dumps({'path': self.path, 'key': self.headers.get('X-Key')}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(b)))
        self.end_headers(); self.wfile.write(b)


def bg(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Apps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='apps-test-')
        repo = os.path.join(cls.tmp, 'repo')
        shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        g = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-C', repo]
        subprocess.run(g + ['init', '-q', '-b', 'main'], check=True)
        subprocess.run(g + ['add', '-A'], check=True)
        subprocess.run(g + ['commit', '-q', '-m', 'seed'], check=True)
        cls.repo = repo
        doc = house_doc()
        if REAL_HOUSE:
            hd = os.path.join(cls.tmp, 'house'); os.makedirs(hd)
            with open(os.path.join(hd, 'house.json'), 'w') as f:
                json.dump(doc, f)
            cls.house = house_server.serve(hd, 0, '127.0.0.1')
        else:
            FakeHouse.doc = doc
            cls.house = ThreadingHTTPServer(('127.0.0.1', 0), FakeHouse)
        bg(cls.house)
        cls.up = bg(ThreadingHTTPServer(('127.0.0.1', 0), Upstream))
        data = os.path.join(cls.tmp, 'data'); os.makedirs(data)
        cls.up_host = f'127.0.0.1:{cls.up.server_address[1]}'
        with open(os.path.join(data, 'proxy-private.json'), 'w') as f:
            json.dump(['127.0.0.1'], f)  # the fake upstream is local; everything else private stays refused
        with open(os.path.join(data, 'secrets.json'), 'w') as f:
            json.dump({'127.0.0.1': {'headers': {'X-Key': 'sekret'}}}, f)
        a = apps_server.main_args(['--repo', repo, '--data', data, '--port', '0', '--bind', '127.0.0.1', '--poll', '0.3',
                                   '--house', f'http://127.0.0.1:{cls.house.server_address[1]}', '--io-timeout', '1', '--headset-poll', '0'])
        cls.srv, cls.st = apps_server.serve(a)
        bg(cls.srv)
        cls.port = cls.srv.server_address[1]
        cls.screen = next(s['id'] for s in doc['screens'] if s.get('role') == 'extra')

    @classmethod
    def tearDownClass(cls):
        for s in (cls.srv, cls.house, cls.up):
            s.shutdown(); s.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        b = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        if headers is None:  # by default: what host.js sends
            headers = {'X-PinPin': '1', **({'Content-Type': 'application/json'} if b is not None else {})}
        c.request(method, path, body=b, headers=headers)
        r = c.getresponse(); data = r.read(); c.close()
        try:
            return r.status, json.loads(data), r
        except ValueError:
            return r.status, data.decode('utf-8', 'replace'), r

    def commits(self):
        return int(subprocess.run(['git', '-C', self.repo, 'rev-list', '--count', 'HEAD'], capture_output=True, text=True).stdout)

    def events(self, screen):
        """A live SSE connection; returns (events list, closer). Waits for the hello."""
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        c.request('GET', f'/events?screen={screen}')
        r = c.getresponse()
        got, ready = [], threading.Event()

        def read():
            ev = None
            try:
                for line in r:
                    line = line.decode().rstrip('\n')
                    if line.startswith('event: '): ev = line[7:]
                    elif line.startswith('data: '):
                        got.append((ev, json.loads(line[6:])))
                        if ev == 'hello': ready.set()
            except Exception:
                pass
        threading.Thread(target=read, daemon=True).start()
        self.assertTrue(ready.wait(5), 'no hello event')
        return got, c.close

    def wait(self, cond, t=5):
        end = time.time() + t
        while time.time() < end:
            if cond(): return True
            time.sleep(0.05)
        return False

    def app(self, app_id, extra=None, perms=None, html='<html><head></head><body>hi</body></html>'):
        m = {'id': app_id, 'name': app_id.title(), 'description': 'test app', 'permissions': perms or {}}
        files = {'index.html': html, 'manifest.json': json.dumps(m)}
        files.update(extra or {})
        return files

    # ---- tests
    def test_00_starter_apps_valid(self):
        for a in ('weather', 'recipes'):
            with open(os.path.join(ROOT, 'apps', a, 'manifest.json')) as f:
                self.assertEqual(manifest.validate(json.load(f), a), [], a)
        st, apps, _ = self.req('GET', '/api/apps')
        self.assertEqual(st, 200)
        self.assertTrue({'weather', 'recipes'} <= {a['id'] for a in apps})
        self.assertNotIn('zz-probe', {a['id'] for a in apps})  # hidden test app
        self.assertIn('zz-probe', {a['id'] for a in self.req('GET', '/api/apps?all=1')[1]})

    def test_preview_shell_and_sandboxed_frame(self):
        st, html, r = self.req('GET', '/app/weather/')
        self.assertEqual(st, 200)
        self.assertIn('<iframe id="app" sandbox="allow-scripts"', html)  # no allow-same-origin: an opaque origin
        self.assertIn('<script src="/sdk/host.js" data-app="weather" data-screen="preview:weather">', html)
        self.assertEqual(r.getheader('Cache-Control'), 'no-store')
        st, html, r = self.req('GET', '/frame/weather/?screen=preview:weather')
        self.assertIn('<script src="/sdk/pinpin.js" data-app="weather" data-screen="preview:weather">', html)
        self.assertIn('<base href="/apps/weather/">', html)
        self.assertIn('sandbox allow-scripts', r.getheader('Content-Security-Policy'))  # sandboxed even opened directly
        self.assertIn("connect-src 'none'", r.getheader('Content-Security-Policy'))
        st, html, r = self.req('GET', '/frame/weather/?screen=%22%3E%3Cscript%3Ealert(1)%3C/script%3E')
        self.assertNotIn('<script>alert(1)', html)
        for p in ('/sdk/pinpin.js', '/sdk/pinpin.css', '/sdk/host.js', '/sdk/host.css', '/apps/recipes/catalogue.js', '/apps/weather/manifest.json'):
            self.assertEqual(self.req('GET', p)[0], 200, p)

    def test_path_traversal_and_bad_ids(self):
        for p in ('/apps/weather/../../server/apps_server.py', '/apps/weather/%2e%2e/recipes/index.html', '/apps/..%2fserver/x.js',
                  '/sdk/../server/manifest.py', '/apps/weather/.git/config', '/app/../', '/api/apps/Weather'):
            self.assertIn(self.req('GET', p)[0], (404, 302, 400), p)
        st, d, _ = self.req('PUT', '/api/apps/evil', {'files': {'../x.html': 'a', 'index.html': 'x', 'manifest.json': '{}'}})
        self.assertEqual(st, 400)
        st, d, _ = self.req('PUT', '/api/apps/Bad_ID!', {'files': self.app('x')})
        self.assertEqual(st, 404)

    def test_create_update_commit_and_reload(self):
        n = self.commits()
        st, d, _ = self.req('PUT', '/api/apps/tclock', {'files': self.app('tclock'), 'message': 'a clock', 'by': 'test'})
        self.assertEqual(st, 200, d); self.assertTrue(d['changed'])
        self.assertEqual(self.commits(), n + 1)
        subj = subprocess.run(['git', '-C', self.repo, 'log', '-1', '--format=%s %an'], capture_output=True, text=True).stdout
        self.assertIn('tclock: a clock (by test) Mr. PinPin', subj)
        # same content again: no commit
        st, d, _ = self.req('PUT', '/api/apps/tclock', {'files': self.app('tclock')})
        self.assertFalse(d['changed']); self.assertEqual(self.commits(), n + 1)
        # an update reloads previews and screens showing it
        got, close = self.events('preview:tclock')
        st, d, _ = self.req('PUT', '/api/apps/tclock', {'files': {'style.css': 'body{color:red}'}, 'message': 'red'})
        self.assertEqual(st, 200, d)
        self.assertTrue(self.wait(lambda: ('reload', {'why': 'updated'}) in got))
        close()
        st, d, _ = self.req('GET', '/api/apps/tclock')
        self.assertEqual(set(d['files']), {'index.html', 'manifest.json', 'style.css'})
        # delete one file, then replace the whole folder
        self.req('PUT', '/api/apps/tclock', {'files': {'style.css': None}})
        self.assertNotIn('style.css', self.req('GET', '/api/apps/tclock')[1]['files'])
        self.req('PUT', '/api/apps/tclock', {'files': self.app('tclock', {'a.js': '1'}), 'replace': True})
        self.assertEqual(set(self.req('GET', '/api/apps/tclock')[1]['files']), {'index.html', 'manifest.json', 'a.js'})

    def test_invalid_apps_rejected_and_nothing_written(self):
        n = self.commits()
        bad = {
            'no index': {'manifest.json': json.dumps({'id': 'b1', 'name': 'n', 'description': 'd'})},
            'id mismatch': self.app('other'),
            'bad json': {'index.html': 'x', 'manifest.json': '{nope'},
            'bad perm': self.app('b1', perms={'camera': True}),
            'bad host': self.app('b1', perms={'fetch': ['http://x.com/']}),
            'bad ext': self.app('b1', {'run.sh': 'rm -rf /'}),
            'dotfile': self.app('b1', {'.htaccess.txt': 'x'}),
            'binary': {'index.html': 5, 'manifest.json': '{}'},
        }
        for name, files in bad.items():
            st, d, _ = self.req('PUT', '/api/apps/b1', {'files': files})
            self.assertEqual(st, 400, name)
        self.assertEqual(self.commits(), n)
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'apps', 'b1')))

    def test_history_and_rollback(self):
        self.req('PUT', '/api/apps/hist', {'files': self.app('hist', html='<head></head>v1'), 'message': 'v1'})
        self.req('PUT', '/api/apps/hist', {'files': {'index.html': '<head></head>v2', 'extra.js': '2'}, 'message': 'v2'})
        h = self.req('GET', '/api/apps/hist/history')[1]
        self.assertEqual([x['subject'].split(' (')[0] for x in h], ['hist: v2', 'hist: v1'])
        st, d, _ = self.req('POST', '/api/apps/hist/rollback', {'commit': h[1]['commit']})
        self.assertEqual(st, 200, d); self.assertTrue(d['changed'])
        files = self.req('GET', '/api/apps/hist')[1]['files']
        self.assertEqual(files['index.html'], '<head></head>v1'); self.assertNotIn('extra.js', files)
        self.assertEqual(self.req('POST', '/api/apps/hist/rollback', {'commit': '0000000'})[0], 400)
        self.assertEqual(self.req('POST', '/api/apps/hist/rollback', {'commit': 'HEAD; rm'})[0], 400)

    def test_screen_mapping_in_house(self):
        s = self.screen
        st, html, _ = self.req('GET', f'/screen/{s}')
        got, close = self.events(str(s))
        st, d, _ = self.req('POST', f'/api/screens/{s}/app', {'app': 'weather'})
        self.assertEqual(st, 200, d)
        self.assertTrue(self.wait(lambda: any(e == 'reload' for e, _ in got)))
        house = json.loads(urlget(f'http://127.0.0.1:{self.house.server_address[1]}/house'))
        self.assertIn({'screen': s, 'app': 'weather'}, house['apps'])
        self.assertEqual(house['by'], 'apps')
        if REAL_HOUSE:
            self.assertEqual(house_schema.validate(house), [])
        self.assertIn('data-app="weather"', self.req('GET', f'/screen/{s}')[1])
        rows = {r['screen']: r for r in self.req('GET', '/api/screens')[1]}
        self.assertEqual(rows[s]['app'], 'weather'); self.assertGreaterEqual(rows[s]['clients'], 1)
        self.assertEqual(self.req('POST', f'/api/screens/{s}/app', {'app': 'nope'})[0], 400)
        # a screen the headset has not placed yet can be assigned too (shows up once it exists)
        self.assertEqual(self.req('POST', '/api/screens/40/app', {'app': 'recipes'})[0], 200)
        self.assertEqual({r['screen']: r['app'] for r in self.req('GET', '/api/screens')[1]}[40], 'recipes')
        self.req('POST', '/api/screens/40/app', {'app': None})
        self.assertNotIn(40, {r['screen'] for r in self.req('GET', '/api/screens')[1]})
        close()

    def test_house_conflict_retried_and_other_writers_kept(self):
        """Someone else (the headset) writes between our GET and PUT: we retry on the new rev, keep their change."""
        hp = self.house.server_address[1]
        real_fetch = self.st.house.fetch
        raced = []

        def racing_fetch():
            doc = real_fetch()
            if not raced:
                raced.append(1)
                d = json.loads(urlget(f'http://127.0.0.1:{hp}/house'))
                d['screens'][0]['x'] = 0.123
                d['by'] = 'headset'
                body = json.dumps(d).encode()
                st = urlput(f'http://127.0.0.1:{hp}/house', body, d['rev'])
                self.assertEqual(st, 200)
            return doc
        self.st.house.fetch = racing_fetch
        try:
            st, d, _ = self.req('POST', f'/api/screens/{self.screen}/app', {'app': 'recipes'})
        finally:
            self.st.house.fetch = real_fetch
        self.assertEqual(st, 200, d)
        house = json.loads(urlget(f'http://127.0.0.1:{hp}/house'))
        self.assertEqual(house['screens'][0]['x'], 0.123)
        self.assertIn({'screen': self.screen, 'app': 'recipes'}, house['apps'])

    def test_house_change_by_others_reloads_screen(self):
        hp = self.house.server_address[1]
        got, close = self.events('31')
        d = json.loads(urlget(f'http://127.0.0.1:{hp}/house'))
        d['apps'] = [a for a in d.get('apps', []) if a['screen'] != 31] + [{'screen': 31, 'app': 'weather'}]
        d['by'] = 'apps'
        self.assertEqual(urlput(f'http://127.0.0.1:{hp}/house', json.dumps(d).encode(), d['rev']), 200)
        self.assertTrue(self.wait(lambda: any(e == 'reload' for e, _ in got)), 'watcher did not reload screen 31')
        close()

    def test_voice_say(self):
        got, close = self.events('77')
        st, d, _ = self.req('POST', '/api/screens/77/say', {'text': '  eggs, tomatoes  '})
        self.assertEqual(st, 200); self.assertEqual(d['delivered'], 1)
        self.assertTrue(self.wait(lambda: ('voice', {'text': 'eggs, tomatoes', 'final': True}) in got))
        close()
        self.assertEqual(self.req('POST', '/api/screens/78/say', {'text': 'x'})[0], 404)  # nobody listening
        self.assertEqual(self.req('POST', '/api/screens/77/say', {'text': ''})[0], 400)

    def test_ask_stub_and_permission(self):
        st, d, _ = self.req('POST', '/api/ask', {'app': 'recipes', 'screen': '2', 'prompt': 'eggs', 'json': True})
        self.assertEqual(st, 200); self.assertTrue(d['stub'])
        self.assertEqual(self.req('POST', '/api/ask', {'app': 'weather', 'prompt': 'hi'})[0], 403)
        self.assertEqual(self.req('POST', '/api/ask', {'app': 'recipes'})[0], 400)

    def test_ask_forwarded_to_assistant(self):
        class A(BaseHTTPRequestHandler):
            seen = []
            def log_message(self, *a): pass
            def do_POST(self):
                d = json.loads(self.rfile.read(int(self.headers['Content-Length']))); A.seen.append(d)
                b = json.dumps({'text': '{"recipes": []}', 'json': {'recipes': []}}).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
        srv = bg(ThreadingHTTPServer(('127.0.0.1', 0), A))
        self.st.assistant = apps_server.make_assistant(f'http://127.0.0.1:{srv.server_address[1]}/api/ask')
        try:
            st, d, _ = self.req('POST', '/api/ask', {'app': 'recipes', 'screen': '2', 'prompt': 'eggs', 'json': True, 'model': 'haiku', 'junk': 1})
        finally:
            self.st.assistant = None; srv.shutdown(); srv.server_close()
        self.assertEqual(st, 200); self.assertEqual(d['json'], {'recipes': []})
        self.assertEqual(A.seen, [{'app': 'recipes', 'screen': '2', 'prompt': 'eggs', 'json': True, 'model': 'haiku'}])

    def test_proxy_allowlist_secrets_cache(self):
        self.req('PUT', '/api/apps/net', {'files': self.app('net', perms={'fetch': [self.up_host]})})
        q = '/proxy?app=net&url=' + f'http://{self.up_host}/v1/x%3Fa%3D1'
        Upstream.hits = 0
        st, d, r = self.req('GET', q)
        self.assertEqual(st, 200, d); self.assertEqual(d, {'path': '/v1/x?a=1', 'key': 'sekret'})
        self.assertEqual(r.getheader('X-Proxy-Cache'), 'miss')
        st, d, r = self.req('GET', q)
        self.assertEqual(r.getheader('X-Proxy-Cache'), 'hit'); self.assertEqual(Upstream.hits, 1)
        self.assertEqual(self.req('GET', '/proxy?app=net&url=http://127.0.0.2:1/')[0], 403)
        self.assertEqual(self.req('GET', '/proxy?app=weather&url=' + f'http://{self.up_host}/')[0], 403)
        self.assertEqual(self.req('GET', '/proxy?app=net&url=file:///etc/passwd')[0], 400)
        self.assertEqual(self.req('GET', '/proxy?app=nope&url=http://x/')[0], 404)

    def test_app_data_store(self):
        self.assertEqual(self.req('GET', '/api/apps/recipes/data')[1], {})
        self.assertEqual(self.req('PUT', '/api/apps/recipes/data', b'{"state": {"have": ["egg"]}}')[0], 200)
        self.assertEqual(self.req('GET', '/api/apps/recipes/data')[1], {'state': {'have': ['egg']}})
        self.assertEqual(self.req('PUT', '/api/apps/recipes/data', b'nope')[0], 400)
        self.assertEqual(self.req('PUT', '/api/apps/recipes/data', b'"' + b'x' * (300 << 10) + b'"')[0], 413)
        self.assertEqual(self.req('PUT', '/api/apps/ghost/data', b'{}')[0], 404)
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'apps', 'recipes', 'data')))

    def test_idle_screen_and_delete(self):
        st, html, _ = self.req('GET', '/screen/55')
        self.assertIn('data-app="_idle"', html)
        self.assertIn('No app here yet', self.req('GET', '/frame/_idle/?screen=55')[1])
        self.req('PUT', '/api/apps/tmpapp', {'files': self.app('tmpapp')})
        self.req('POST', '/api/screens/56/app', {'app': 'tmpapp'})
        st, d, _ = self.req('DELETE', '/api/apps/tmpapp?message=test')
        self.assertEqual(st, 200, d)
        self.assertNotIn('tmpapp', [a['id'] for a in self.req('GET', '/api/apps')[1]])
        self.assertNotIn(56, {r['screen'] for r in self.req('GET', '/api/screens')[1]})
        self.assertIn('data-app="_idle"', self.req('GET', '/screen/56')[1])

    def test_assistant_url_file(self):
        with open(os.path.join(self.st.data, 'assistant-url'), 'w') as f:
            f.write('http://127.0.0.1:9/api/ask\n')
        try:
            self.assertEqual(self.req('GET', '/health')[1]['assistant'], 'http://127.0.0.1:9/api/ask')
            st, d, _ = self.req('POST', '/api/ask', {'app': 'recipes', 'prompt': 'x'})
            self.assertEqual(st, 502); self.assertTrue(d['stub'])  # down: the app falls back
        finally:
            os.remove(os.path.join(self.st.data, 'assistant-url'))
        self.assertEqual(self.req('GET', '/health')[1]['assistant'], 'stub')

    def test_open_url_on_screen(self):
        st, d, _ = self.req('POST', '/api/screens/61/open', {'url': 'https://example.com/'})
        self.assertEqual(st, 200, d); seq = d['seq']
        rows = {r['screen']: r for r in self.req('GET', '/api/screens')[1]}
        self.assertEqual((rows[61]['url'], rows[61]['seq'], rows[61]['shown_seq'], rows[61]['display']), ('https://example.com/', seq, 0, 61))
        self.assertEqual(self.req('POST', '/api/screens/61/shown', {'seq': seq})[0], 200)
        self.assertEqual({r['screen']: r for r in self.req('GET', '/api/screens')[1]}[61]['shown_seq'], seq)
        for bad in ('file:///etc/passwd', 'javascript:alert(1)', 'example.com', 5):
            self.assertEqual(self.req('POST', '/api/screens/61/open', {'url': bad})[0], 400, bad)
        self.req('POST', '/api/screens/61/app', {'app': 'weather'})  # assigning an app clears the url
        self.assertIsNone({r['screen']: r for r in self.req('GET', '/api/screens')[1]}[61]['url'])
        self.req('POST', '/api/screens/61/app', {'app': None})
        self.assertNotIn(61, {r['screen'] for r in self.req('GET', '/api/screens')[1]})
        self.req('POST', '/api/screens/62/open', {'url': 'https://example.com/'})
        self.assertEqual(json.load(open(os.path.join(self.st.data, 'opens.json')))['62']['url'], 'https://example.com/')
        self.req('POST', '/api/screens/62/open', {'url': None})
        self.assertNotIn(62, {r['screen'] for r in self.req('GET', '/api/screens')[1]})

    def test_create_and_base_guards(self):
        self.assertEqual(self.req('PUT', '/api/apps/guarded', {'files': self.app('guarded'), 'create': True})[0], 200)
        st, d, _ = self.req('PUT', '/api/apps/guarded', {'files': self.app('guarded'), 'create': True})
        self.assertEqual(st, 409); head = d['commit']
        self.assertEqual(self.req('PUT', '/api/apps/guarded', {'files': {'a.js': '1'}, 'base': head[:10]})[0], 200)
        st, d, _ = self.req('PUT', '/api/apps/guarded', {'files': {'a.js': '2'}, 'base': head})  # stale base
        self.assertEqual(st, 409)
        self.assertEqual(self.req('GET', '/api/apps/guarded')[1]['files']['a.js'], '1')

    def test_catalog_icons(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8" fill="#f00"/></svg>'
        m = {'id': 'iconic', 'name': 'Iconic', 'description': 'd', 'icon': 'icon.svg'}
        st, d, _ = self.req('PUT', '/api/apps/iconic', {'files': {'index.html': 'x', 'manifest.json': json.dumps(m), 'icon.svg': svg}})
        self.assertEqual(st, 200, d)
        cat = {a['id']: a for a in self.req('GET', '/api/apps')[1]}
        self.assertTrue(cat['iconic']['icon'].startswith('data:image/svg+xml;base64,'))
        self.assertEqual(cat['iconic']['icon_png'], '/api/apps/iconic/icon.png')
        self.assertEqual(len(cat['iconic']['commit']), 12)
        self.assertTrue(cat['clock' if 'clock' in cat else 'weather']['icon'].startswith('data:image/'))  # every app has one
        m['icon'] = 'missing.svg'
        self.assertEqual(self.req('PUT', '/api/apps/iconic', {'files': {'manifest.json': json.dumps(m)}})[0], 400)
        for a in cat:
            if a in ('weather', 'recipes', 'timer', 'shopping', 'clock', 'notes'):
                self.assertIn('icon.svg', os.listdir(os.path.join(ROOT, 'apps', a)), f'{a}: starter apps ship an icon')

    def test_apps_cannot_call_the_api_themselves(self):
        frame = {'Origin': 'null', 'Content-Type': 'application/json', 'X-PinPin': '1'}  # what a sandboxed frame sends
        for method, path, body in (('POST', '/api/screens/2/app', {'app': 'weather'}), ('PUT', '/api/apps/weather/data', {}),
                                   ('PUT', '/api/apps/weather', {'files': {}}), ('POST', '/api/ask', {'prompt': 'x'}),
                                   ('GET', '/api/apps/recipes/data', None), ('GET', '/proxy?app=weather&url=https://api.open-meteo.com/', None),
                                   ('DELETE', '/api/apps/weather', None)):
            self.assertEqual(self.req(method, path, body, headers=frame)[0], 403, path)
        # a no-cors request (no custom header) can't read another app's data or use the proxy or ask
        for path in ('/api/apps/recipes/data', '/proxy?app=weather&url=https://api.open-meteo.com/'):
            self.assertEqual(self.req('GET', path, headers={})[0], 403, path)
        # a blind form-style write (text/plain, no preflight) is refused
        st, d, _ = self.req('POST', '/api/screens/2/app', b'{"app": "weather"}', headers={'Content-Type': 'text/plain'})
        self.assertEqual(st, 415)
        self.assertEqual(self.req('GET', '/api/apps', headers={})[2].getheader('X-Content-Type-Options'), 'nosniff')

    def test_proxy_hardening(self):
        self.req('PUT', '/api/apps/net2', {'files': self.app('net2', perms={'fetch': ['api.open-meteo.com', self.up_host, 'localhost', '10.1.2.3']})})
        for url, code in (('http://api.open-meteo.com@evil.example/', 403),          # userinfo trick: the real host is not allowed
                          ('http://api.open-meteo.com.evil.example/', 403),           # suffix trick
                          ('http://localhost/', 403), ('http://10.1.2.3/', 403),     # listed, but LAN/loopback: never
                          ('http://user:pw@api.open-meteo.com/', 400)):
            self.assertEqual(self.req('GET', '/proxy?app=net2&url=' + urllib.parse.quote(url, safe=''))[0], code, url)
        # a redirect from an allowed host is passed back, not followed
        Upstream.hits = 0
        st, d, r = self.req('GET', '/proxy?app=net2&url=' + urllib.parse.quote(f'http://{self.up_host}/redirect', safe=''))
        self.assertEqual(st, 302); self.assertEqual(Upstream.hits, 1)

    def test_upload_limits(self):
        sk = socket.create_connection(('127.0.0.1', self.port), timeout=10)  # announced as 13 MB: refused before reading it
        sk.sendall(b'PUT /api/apps/big HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: 13631488\r\n\r\n{')
        self.assertIn(b' 413 ', sk.recv(200)); sk.close()
        # a client that stalls mid-upload is dropped after the io timeout (1 s in the tests), not held forever
        sk = socket.create_connection(('127.0.0.1', self.port), timeout=10)
        sk.sendall(b'PUT /api/apps/slow HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{"files":')
        t0 = time.time(); sk.settimeout(10)
        try:
            sk.recv(100)
        except OSError:
            pass
        self.assertLess(time.time() - t0, 5)
        sk.close()
        self.assertEqual(self.req('GET', '/health')[0], 200)  # still serving

    def test_control_page(self):
        st, html, r = self.req('GET', '/')
        self.assertEqual(st, 200); self.assertIn('<title>PinPin apps</title>', html)
        cat = self.req('GET', '/api/apps')[1]
        self.assertTrue(all('last' in a and 'commit' in a for a in cat))

    def test_input_forwarded_to_the_runner(self):
        class R(BaseHTTPRequestHandler):
            seen = []
            def log_message(self, *a): pass
            def do_POST(self):
                R.seen.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                code, out = (404, {'error': 'no window'}) if '/9/' in self.path else (200, {'ok': True, 'x': 1, 'y': 2})
                b = json.dumps(out).encode()
                self.send_response(code); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
        srv = bg(ThreadingHTTPServer(('127.0.0.1', 0), R))
        try:
            self.st.runner_url = ''
            self.assertEqual(self.req('POST', '/api/screens/47/click', {'u': 1, 'v': 2})[0], 502)  # not checked in yet
            # the runner checks in by polling /api/screens with its input port
            self.req('GET', '/api/screens', headers={'X-PinPin-Runner': str(srv.server_address[1])})
            self.assertEqual(self.st.runner_url, f'http://127.0.0.1:{srv.server_address[1]}')
            st, d, _ = self.req('POST', '/api/screens/47/click', {'u': 5000, 'v': 8450, 'count': 2})
            self.assertEqual((st, d), (200, {'ok': True, 'x': 1, 'y': 2}))
            self.assertEqual(R.seen[-1], ('/input/47/click', {'u': 5000, 'v': 8450, 'count': 2}))
            for kind in ('move', 'scroll', 'type', 'key'):
                self.assertEqual(self.req('POST', f'/api/screens/47/{kind}', {'u': 1})[0], 200, kind)
            self.assertEqual(self.req('POST', '/api/screens/9/click', {'u': 1, 'v': 1})[0], 404)  # no window there
            self.assertEqual(self.req('POST', '/api/screens/47/click', {'u': 1}, headers={'Origin': 'null', 'Content-Type': 'application/json'})[0], 403)
        finally:
            srv.shutdown(); srv.server_close(); self.st.runner_url = ''

    def test_build_forwarded_to_assistant(self):
        class B(BaseHTTPRequestHandler):
            seen = []
            def log_message(self, *a): pass
            def do_POST(self):
                B.seen.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                b = json.dumps({'text': 'Made **egg-timer**.', 'app': 'egg-timer'}).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
        srv = bg(ThreadingHTTPServer(('127.0.0.1', 0), B))
        try:
            self.assertEqual(self.req('POST', '/api/build', {'prompt': 'x'})[0], 502)  # stub assistant: nowhere to build
            self.st.assistant = apps_server.make_assistant(f'http://127.0.0.1:{srv.server_address[1]}/api/ask')
            st, d, _ = self.req('POST', '/api/build', {'prompt': ' an egg timer ', 'screen': 47})
            self.assertEqual((st, d['app']), (200, 'egg-timer'))
            self.assertEqual(B.seen[-1], ('/api/build', {'prompt': 'an egg timer', 'app': None, 'screen': 47, 'from': 'control-page'}))
            self.assertEqual(self.req('POST', '/api/build', {'prompt': 'x', 'app': 'nope'})[0], 400)
            self.assertEqual(self.req('POST', '/api/build', {'prompt': ''})[0], 400)
        finally:
            self.st.assistant = None; srv.shutdown(); srv.server_close()

    def test_status(self):
        st, d, _ = self.req('GET', '/api/status')
        self.assertEqual(st, 200)
        self.assertIsNone(d['vps'])  # nothing pushed yet
        os.makedirs(os.path.join(self.st.data, 'status'), exist_ok=True)
        with open(os.path.join(self.st.data, 'status', 'vps.json'), 'w') as f:
            json.dump({'generated': 'x', 'roadmap': '## Morning list\n- [ ] a', 'activity': ['l1']}, f)
        d = self.req('GET', '/api/status')[1]
        self.assertEqual(d['vps']['roadmap'], '## Morning list\n- [ ] a'); self.assertIn('age_s', d['vps'])
        self.assertIn('recipes', d['apps']); self.assertIsInstance(d['screens'], list)
        self.assertIn('<title>', self.req('GET', '/status')[1])
        self.assertIn('All the switches', self.req('GET', '/help')[1])
        # the floor plan is relayed from the house server (the stand-in has none: an error, not a crash)
        self.assertIn(self.req('GET', '/house-plan.svg')[0], (200, 404, 502))

    def test_app_to_app_messages(self):
        snd = {'id': 'msender', 'name': 'S', 'description': 'd', 'permissions': {'send': ['mrecv']}}
        rcv = {'id': 'mrecv', 'name': 'R', 'description': 'd', 'accepts': ['add-items']}
        self.req('PUT', '/api/apps/msender', {'files': {'index.html': 'x', 'manifest.json': json.dumps(snd)}})
        self.req('PUT', '/api/apps/mrecv', {'files': {'index.html': 'x', 'manifest.json': json.dumps(rcv)}})
        got, close = self.events('preview:mrecv')
        st, d, _ = self.req('POST', '/api/apps/mrecv/inbox', {'from': 'msender', 'kind': 'add-items', 'data': {'items': ['milk']}})
        self.assertEqual(st, 200, d)
        self.assertTrue(self.wait(lambda: any(e == 'message' for e, _ in got))); close()
        inbox = self.req('GET', '/api/apps/mrecv/inbox')[1]
        self.assertEqual([(m['from'], m['kind'], m['data']) for m in inbox], [('msender', 'add-items', {'items': ['milk']})])
        # not allowed: wrong kind, a sender without permission, a frame (Origin null), oversized
        self.assertEqual(self.req('POST', '/api/apps/mrecv/inbox', {'from': 'msender', 'kind': 'delete-all', 'data': {}})[0], 403)
        self.assertEqual(self.req('POST', '/api/apps/mrecv/inbox', {'from': 'weather', 'kind': 'add-items', 'data': {}})[0], 403)
        self.assertEqual(self.req('POST', '/api/apps/mrecv/inbox', {'from': 'msender', 'kind': 'add-items'},
                                  headers={'Origin': 'null', 'Content-Type': 'application/json', 'X-PinPin': '1'})[0], 403)
        self.assertEqual(self.req('POST', '/api/apps/mrecv/inbox', {'from': 'msender', 'kind': 'add-items', 'data': 'x' * 20000})[0], 400)
        self.assertEqual(self.req('GET', '/api/apps/mrecv/inbox', headers={})[0], 403)  # only host pages read inboxes
        self.assertEqual(self.req('POST', '/api/apps/mrecv/inbox/ack', {'ids': [inbox[0]['id']]})[1], {'acked': 1})
        self.assertEqual(self.req('GET', '/api/apps/mrecv/inbox')[1], [])
        # manifests: send and accepts are validated
        bad = dict(snd, permissions={'send': ['../x']})
        self.assertEqual(self.req('PUT', '/api/apps/msender', {'files': {'manifest.json': json.dumps(bad)}})[0], 400)

    def test_authoring_guide(self):
        st, text, r = self.req('GET', '/api/authoring')
        self.assertEqual(st, 200); self.assertIn('PUT http://', text)

    def test_health(self):
        st, d, _ = self.req('GET', '/health')
        self.assertTrue(d['ok']); self.assertEqual(d['assistant'], 'stub')


def urlget(u):
    import urllib.request
    with urllib.request.urlopen(u, timeout=5) as r:
        return r.read()


def urlput(u, body, rev):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(u, data=body, method='PUT', headers={'If-Match': str(rev), 'X-House-SHA256': hashlib.sha256(body).hexdigest()})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


if __name__ == '__main__':
    print('house server:', 'real (' + QWS_HOUSE + ')' if REAL_HOUSE else 'stand-in')
    unittest.main(verbosity=1)
