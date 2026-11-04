#!/usr/bin/env python3
"""Render an app in headless Chrome and report what a person would see: JS errors, console lines, the text on the
page, and a screenshot. It can also speak to the app (pinpin._voice). It runs on the Mini, which has Chrome; stdlib
only, with a minimal WebSocket client for the DevTools protocol.

  python3 tools/render_check.py http://127.0.0.1:57830/app/recipes/ --say "eggs, tomatoes" --say "recipe 1" \
      --wait 4 --shot /tmp/recipes.png [--size 1920x1200] [--expect "Shakshuka"]

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

    def send(self, method, params=None, wait=True):
        self.id += 1
        msg = json.dumps({'id': self.id, 'method': method, 'params': params or {}}).encode()
        mask = os.urandom(4)
        n = len(msg)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack('>H', n) if n < 65536
                               else bytes([0x80 | 127]) + struct.pack('>Q', n))
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(msg)))
        if not wait:
            return None
        while True:
            m = self.recv()
            if m.get('id') == self.id:
                return m.get('result', m.get('error'))
            self.events.append(m)

    def pump(self, seconds):
        end = time.time() + seconds
        self.s.settimeout(0.2)
        while time.time() < end:
            try:
                self.events.append(self.recv())
            except (socket.timeout, TimeoutError):
                pass
        self.s.settimeout(30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('url')
    ap.add_argument('--say', action='append', default=[])
    ap.add_argument('--wait', type=float, default=3.0, help='seconds after load and after each phrase')
    ap.add_argument('--shot', default='')
    ap.add_argument('--size', default='1920x1200')
    ap.add_argument('--expect', action='append', default=[])
    a = ap.parse_args()
    w, h = (int(x) for x in a.size.split('x'))
    prof = tempfile.mkdtemp(prefix='pp-render-')
    port = 9400 + os.getpid() % 500
    p = subprocess.Popen([CHROME, '--headless=new', f'--remote-debugging-port={port}', f'--user-data-dir={prof}',
                          f'--window-size={w},{h}', '--hide-scrollbars', '--no-first-run', '--use-mock-keychain', 'about:blank'],
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
        ws.send('Page.navigate', {'url': a.url})
        ws.pump(a.wait)
        for t in a.say:
            ws.send('Runtime.evaluate', {'expression': f'window.pinpin && pinpin._voice({json.dumps(t)}, {{check: true}})'})
            ws.pump(a.wait)
        text = ws.send('Runtime.evaluate', {'expression': 'document.body ? document.body.innerText : ""', 'returnByValue': True})
        text = (text or {}).get('result', {}).get('value', '')
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
        missing = [x for x in a.expect if x not in text]
        print(json.dumps({'url': a.url, 'errors': errors, 'console': console[-20:], 'missing': missing, 'text': text[:1500]}, indent=1, ensure_ascii=False))
        sys.exit(1 if errors or missing else 0)
    finally:
        p.terminate()
        try: p.wait(5)
        except Exception: p.kill()
        shutil.rmtree(prof, ignore_errors=True)


if __name__ == '__main__':
    main()
