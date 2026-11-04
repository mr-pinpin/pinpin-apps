#!/usr/bin/env python3
"""Render an app in headless Chrome and report what a person would see: JS errors, console lines, the text on the
page, and a screenshot. It can also speak to the app (pinpin._voice). It runs on the Mini, which has Chrome; stdlib
only, with a minimal WebSocket client for the DevTools protocol.

  python3 tools/render_check.py http://127.0.0.1:57830/app/recipes/ --say "eggs, tomatoes" --say "recipe 1" \
      --wait 4 --shot /tmp/recipes.png [--size 1920x1200] [--expect "Shakshuka"] [--fail '*/proxy*']

Sandboxed: this Chrome reaches only the apps server it was pointed at (every other host, the LAN and other local ports
go to a dead proxy), so an app under test can't touch the house. --fail PATTERN makes matching requests fail (e.g.
'*/proxy*' = the internet is down, '*/api/ask*' = the assistant is down) to check how an app degrades.

Exit 0 when there were no JS exceptions (and every --expect text is on the page), else 1. Prints JSON.
"""
import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'


class WS:
    def __init__(self, url):
        host, rest = url[len('ws://'):].split('/', 1)
        h, p = host.split(':')
        self.s = socket.create_connection((h, int(p)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f'GET /{rest} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                        f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            buf += self.s.recv(4096)
        self.buf = buf.split(b'\r\n\r\n', 1)[1]
        self.id = 0
        self.events = []
        self.sessions = []
        self.fail = []

    def _recv(self, n):
        while len(self.buf) < n:
            d = self.s.recv(1 << 20)
            if not d:
                raise EOFError
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        data = b''
        while True:
            b1, b2 = self._recv(2)
            n = b2 & 0x7f
            if n == 126: n = struct.unpack('>H', self._recv(2))[0]
            elif n == 127: n = struct.unpack('>Q', self._recv(8))[0]
            data += self._recv(n)
            if b1 & 0x80:
                return json.loads(data)

    def send(self, method, params=None, wait=True, session=None):
        self.id += 1
        mine = self.id  # event() may send while we wait, which moves self.id
        msg = {'id': mine, 'method': method, 'params': params or {}}
        if session:
            msg['sessionId'] = session  # a child target (the app's out-of-process iframe), flat protocol
        msg = json.dumps(msg).encode()
        mask = os.urandom(4)
        n = len(msg)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack('>H', n) if n < 65536
                               else bytes([0x80 | 127]) + struct.pack('>Q', n))
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(msg)))
        if not wait:
            return None
        while True:
            m = self.recv()
            if m.get('id') == mine:
                return m.get('result', m.get('error'))
            if 'id' not in m:
                self.event(m)

    def pump(self, seconds):
        end = time.time() + seconds
        self.s.settimeout(0.2)
        while time.time() < end:
            try:
                self.event(self.recv())
            except (socket.timeout, TimeoutError):
                pass
        self.s.settimeout(30)

    def event(self, m):
        self.events.append(m)
        if m.get('method') == 'Target.attachedToTarget':  # the sandboxed app frame runs in its own process
            sid = m['params']['sessionId']
            self.sessions.append(sid)
            for meth in ('Runtime.enable', 'Log.enable', 'Runtime.runIfWaitingForDebugger'):
                self.send(meth, wait=False, session=sid)
            if self.fail:
                self.send('Fetch.enable', {'patterns': [{'urlPattern': x} for x in self.fail]}, wait=False, session=sid)
        if m.get('method') == 'Fetch.requestPaused':  # --fail: this request "loses the network"
            self.send('Fetch.failRequest', {'requestId': m['params']['requestId'], 'errorReason': 'InternetDisconnected'},
                      wait=False, session=m.get('sessionId'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('url')
    ap.add_argument('--say', action='append', default=[])
    ap.add_argument('--wait', type=float, default=3.0, help='seconds after load and after each phrase')
    ap.add_argument('--shot', default='')
    ap.add_argument('--size', default='1920x1200')
    ap.add_argument('--expect', action='append', default=[])
    ap.add_argument('--fail', action='append', default=[], help='URL pattern (CDP Fetch syntax) to fail')
    ap.add_argument('--no-sandbox-net', action='store_true', help='let this Chrome reach the network freely')
    a = ap.parse_args()
    w, h = (int(x) for x in a.size.split('x'))
    prof = tempfile.mkdtemp(prefix='pp-render-')
    port = 9400 + os.getpid() % 500
    target = urllib.parse.urlparse(a.url)
    net = [] if a.no_sandbox_net else ['--proxy-server=http://127.0.0.1:9',  # nothing listens there: requests fail
                                       f'--proxy-bypass-list=<-loopback>;{target.hostname}:{target.port or 80}']  # order matters
    p = subprocess.Popen([CHROME, '--headless=new', f'--remote-debugging-port={port}', f'--user-data-dir={prof}',
                          f'--window-size={w},{h}', '--hide-scrollbars', '--no-first-run', '--use-mock-keychain', *net, 'about:blank'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=1).read())
                page = next(t for t in tabs if t.get('type') == 'page')
                break
            except Exception:
                time.sleep(0.2)
        ws = WS(page['webSocketDebuggerUrl'])
        ws.send('Runtime.enable'); ws.send('Page.enable'); ws.send('Log.enable')
        ws.send('Emulation.setDeviceMetricsOverride', {'width': w, 'height': h, 'deviceScaleFactor': 1, 'mobile': False})
        ws.fail = a.fail
        if a.fail:
            ws.send('Fetch.enable', {'patterns': [{'urlPattern': x} for x in a.fail]})
        ws.send('Target.setAutoAttach', {'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True})
        top = ws.send('Page.navigate', {'url': a.url}).get('frameId')
        ws.pump(a.wait)
        for t in a.say:
            ws.send('Runtime.evaluate', {'expression': f'(window.pinpinHost || window.pinpin) && (window.pinpinHost ? pinpinHost.voice : pinpin._voice)({json.dumps(t)}, {{check: true}})'})
            ws.pump(a.wait)
        # the visible text: the top page's plus every frame's (apps run in a sandboxed, out-of-process iframe)
        texts = []
        for sid in [None] + ws.sessions:
            r = ws.send('Runtime.evaluate', {'expression': 'document.body ? document.body.innerText : ""', 'returnByValue': True}, session=sid)
            v = ((r or {}).get('result') or {}).get('value')
            if v:
                texts.append(v)
        text = '\n'.join(texts)
        if a.shot:
            shot = ws.send('Page.captureScreenshot', {'format': 'png'})
            with open(a.shot, 'wb') as f:
                f.write(base64.b64decode(shot['data']))
        errors, console = [], []
        for e in ws.events:
            m, pr = e.get('method'), e.get('params', {})
            if m == 'Runtime.exceptionThrown':
                d = pr['exceptionDetails']
                errors.append((d.get('exception') or {}).get('description') or d.get('text'))
            elif m == 'Runtime.consoleAPICalled':
                line = ' '.join(str(x.get('value', x.get('description', ''))) for x in pr['args'])
                console.append(pr['type'] + ': ' + line)
                if pr['type'] == 'error':  # the SDK catches errors in voice handlers and logs them
                    errors.append(line)
            elif m == 'Log.entryAdded' and pr['entry']['level'] == 'error' and 'favicon.ico' not in pr['entry'].get('url', ''):
                errors.append(pr['entry']['text'] + ' ' + pr['entry'].get('url', ''))
        failed = [x for x in errors if 'ERR_INTERNET_DISCONNECTED' in x] if a.fail else []  # the ones we caused
        errors = [x for x in errors if x not in failed]
        missing = [x for x in a.expect if x not in text]
        print(json.dumps({'url': a.url, 'errors': errors, 'failed_on_purpose': len(failed), 'console': console[-20:], 'missing': missing, 'text': text[:1500]}, indent=1, ensure_ascii=False))
        sys.exit(1 if errors or missing else 0)
    finally:
        p.terminate()
        try: p.wait(5)
        except Exception: p.kill()
        shutil.rmtree(prof, ignore_errors=True)


if __name__ == '__main__':
    main()
