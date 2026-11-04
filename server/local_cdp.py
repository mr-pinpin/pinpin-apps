"""Screens whose page is a Chrome on the Mini itself (K20: the MINI screen's Atlas kiosk), driven over its DevTools port
with the laptop runner's own CDP client, so every screen has the same /api/screens/<id>/click|move|scroll|type|key|
eval|shot contract. Off unless <data>/local-cdp.json names the screen:

  {"1": {"port": 9612, "match": "atlas-webgpu.html", "allow": ["shot", "eval", "click", "move", "scroll", "type", "key"]}}

port: that Chrome's --remote-debugging-port (loopback); match: a substring of the page's URL (the first such page);
allow: the kinds this screen accepts (navigate/open are never routed here: the kiosk page IS the screen). u, v are of the
page's viewport. CDP input is dispatched into the page, so it never moves the system cursor.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'runner'))
import apps_runner  # noqa: E402

KINDS = ('click', 'move', 'scroll', 'type', 'key', 'eval', 'shot')


class LocalScreens(apps_runner.Input):
    def __init__(self, path):
        super().__init__(runner=None)
        self.path, self._cfg, self._mtime = path, {}, None

    def config(self):
        """The file, re-read when it changes (switching a screen on or off needs no restart)."""
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            self._cfg, self._mtime = {}, None
            return self._cfg
        if m != self._mtime:
            try:
                with open(self.path) as f:
                    d = json.load(f)
                self._cfg = {int(k): v for k, v in d.items() if isinstance(v, dict) and str(k).isdigit()}
            except (OSError, ValueError):
                self._cfg = {}
            self._mtime = m
            for s in list(self.conns):  # a changed port or match: reconnect
                try:
                    self.conns.pop(s).close()
                except Exception:
                    pass
        return self._cfg

    def handles(self, s):
        return int(s) in self.config()

    def endpoint(self, s):
        c = self.config()[s]
        return int(c['port']), c.get('match') or None

    def alive(self, s):
        return s in self.config()

    def do(self, s, kind, d):
        c = self.config().get(s)
        if c is None:
            raise LookupError('not enabled')
        if kind not in KINDS or kind not in c.get('allow', KINDS):
            raise PermissionError(f'{kind} is not allowed on screen {s} (local-cdp.json "allow")')
        d = dict(d, space='viewport')  # the page fills its display; there is no display box to map from
        return super().do(s, kind, d)
