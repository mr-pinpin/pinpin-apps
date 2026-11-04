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
        self.assertEqual(r.plan(screens, DISPLAYS, BOUNDS, 1), {2: ('/screen/2', (3360, 25, 1920, 1175))})

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


if __name__ == '__main__':
    unittest.main(verbosity=1)
