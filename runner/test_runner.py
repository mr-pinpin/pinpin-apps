#!/usr/bin/env python3
"""The runner's plan (which windows where) without a Mac."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apps_runner as r  # noqa: E402

DISPLAYS = [{'screen': 2, 'display': 366, 'w': 3840, 'h': 2400, 'scale': 2}, {'screen': 3, 'display': 367, 'w': 3840, 'h': 2400, 'scale': 2}]
BOUNDS = {366: (3360, 0, 1920, 1200), 367: (1440, 0, 1920, 1200), 1: (0, 0, 1440, 900)}


class Plan(unittest.TestCase):
    def test_windows_follow_apps_and_displays(self):
        screens = [{'screen': 1, 'app': 'weather', 'display': None},      # the main screen streams the game: never
                   {'screen': 2, 'app': 'recipes', 'display': 2},
                   {'screen': 3, 'app': None, 'display': 3},             # no app: no window
                   {'screen': 9, 'app': 'weather', 'display': 9}]        # no display yet: wait
        self.assertEqual(r.plan(screens, DISPLAYS, BOUNDS, 1), {2: ('/screen/2', 0, (3360, 25, 1920, 1175))})

    def test_opened_url_needs_no_app(self):
        screens = [{'screen': 3, 'app': None, 'display': 3, 'url': 'https://example.com/', 'seq': 7},
                   {'screen': 2, 'app': 'recipes', 'display': 2, 'url': None, 'seq': 0}]
        self.assertEqual(r.plan(screens, DISPLAYS, BOUNDS, 1),
                         {3: ('https://example.com/', 7, (1440, 25, 1920, 1175)), 2: ('/screen/2', 0, (3360, 25, 1920, 1175))})

    def test_never_main_display(self):
        d = [{'screen': 2, 'display': 1}]
        self.assertEqual(r.plan([{'screen': 2, 'app': 'x', 'display': 2}], d, BOUNDS, 1), {})

    def test_display_without_bounds_skipped(self):
        self.assertEqual(r.plan([{'screen': 2, 'app': 'x', 'display': 2}], DISPLAYS, {}, 1), {})

    def test_chrome_args(self):
        a = r.chrome_args('http://m:57830', 2, '/screen/2', (3360, 25, 1920, 1175))
        self.assertIn('--app=http://m:57830/screen/2', a)
        self.assertIn('--window-position=3360,25', a)
        self.assertTrue(any(x.endswith('profiles/screen-2') for x in a))
        self.assertIn('--kiosk', r.chrome_args('http://m', 2, '/screen/2', (0, 0, 1, 1), 'kiosk'))
        self.assertIn('--remote-debugging-port=9302', a)
        self.assertIn('--app=https://example.com/', r.chrome_args('http://m', 2, 'https://example.com/', (0, 0, 1, 1)))


class Keys(unittest.TestCase):
    def test_key_events(self):
        d, u = r.key_events('Enter')
        self.assertEqual((d['type'], d['key'], d['windowsVirtualKeyCode'], d['text']), ('keyDown', 'Enter', 13, '\r'))
        d, u = r.key_events('Cmd+L')
        self.assertEqual((d['type'], d['key'], d['modifiers'], d.get('text')), ('rawKeyDown', 'L', 4, None))
        d, u = r.key_events('Cmd+A'); self.assertEqual(d['commands'], ['selectAll'])
        self.assertEqual(r.key_events('Shift+Tab')[0]['modifiers'], 8)
        self.assertEqual(r.key_events('down')[0]['key'], 'ArrowDown')
        for bad in ('', 'Hyper+X', 'Florp'):
            self.assertRaises(ValueError, r.key_events, bad)


class Point(unittest.TestCase):
    def test_display_uv_to_page_px(self):
        class Fake:  # a 1920x1200 display at x=1440; the window under the menu bar with a 28 px title bar
            procs = {47: None}; display_box = {47: (1440, 0, 1920, 1200)}
        inp = r.Input(Fake())
        inp.call = lambda s, m, p=None: {'result': {'value': [1440, 25, 1920, 1175, 1920, 1147]}}
        x, y = inp.point(47, 5000, 5000, 'display')
        self.assertEqual((round(x), round(y)), (960, 600 - 53))  # 25 menu bar + 28 title bar above the page
        self.assertEqual(inp.point(47, 0, 0, 'display'), (0, 0))  # clicks on the menu bar clamp to the page's edge
        self.assertEqual(inp.point(47, 10000, 10000, 'viewport'), (1920, 1147))


if __name__ == '__main__':
    unittest.main(verbosity=1)
