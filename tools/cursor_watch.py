#!/usr/bin/env python3
"""Read-only cursor watcher for the Mini (the MINI-screen click bug, 2026-11-04): samples the system cursor once a
second and writes a line only when it LEAVES the display the live Sunshine streams (or comes back), with the
frontmost app at that moment, so the next user session shows who moves the cursor. It never moves anything.

The streamed display is the output_name of the config the running Sunshine was started with (re-read every minute:
display ids change when the resident recreates its virtual displays). Log: ~/pinpin-apps-data/cursor-watch.log,
rotated at 256 KB (one .1 kept).   python3 tools/cursor_watch.py [--period 1]
"""
import argparse
import ctypes
import ctypes.util
import os
import re
import subprocess
import time

CG = ctypes.cdll.LoadLibrary(ctypes.util.find_library('CoreGraphics') or
                             '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
CF = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')


class Point(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]


class Rect(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double), ('w', ctypes.c_double), ('h', ctypes.c_double)]


CG.CGEventCreate.restype = ctypes.c_void_p
CG.CGEventCreate.argtypes = [ctypes.c_void_p]
CG.CGEventGetLocation.restype = Point
CG.CGEventGetLocation.argtypes = [ctypes.c_void_p]
CG.CGDisplayBounds.restype = Rect
CG.CGDisplayBounds.argtypes = [ctypes.c_uint32]
CG.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
CG.CGMainDisplayID.restype = ctypes.c_uint32
CF.CFRelease.argtypes = [ctypes.c_void_p]

LOG = os.path.expanduser('~/pinpin-apps-data/cursor-watch.log')


def cursor():
    ev = CG.CGEventCreate(None)
    p = CG.CGEventGetLocation(ev)
    CF.CFRelease(ev)
    return p.x, p.y


def displays():
    ids, n = (ctypes.c_uint32 * 16)(), ctypes.c_uint32()
    CG.CGGetActiveDisplayList(16, ids, ctypes.byref(n))
    return {ids[i]: CG.CGDisplayBounds(ids[i]) for i in range(n.value)}


def streamed_display():
    """output_name of the running Sunshine's config (its first argument), or None."""
    try:
        ps = subprocess.run(['ps', '-axo', 'args='], capture_output=True, text=True, timeout=5).stdout
        for line in ps.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith('/sunshine') and parts[1].endswith('.conf'):
                with open(parts[1]) as f:
                    m = re.search(r'^\s*output_name\s*=\s*(\d+)', f.read(), re.M)
                return int(m.group(1)) if m else None
    except Exception:
        return None
    return None


def front_app():
    try:
        asn = subprocess.run(['lsappinfo', 'front'], capture_output=True, text=True, timeout=3).stdout.strip()
        out = subprocess.run(['lsappinfo', 'info', '-only', 'name', '-only', 'pid', asn], capture_output=True, text=True, timeout=3).stdout
        name = re.search(r'"(?:LSDisplayName|name)"="([^"]*)"', out)
        pid = re.search(r'"pid"=(\d+)', out)
        return f'{name.group(1) if name else "?"} (pid {pid.group(1) if pid else "?"})'
    except Exception as e:
        return f'? ({e})'


def write(line):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 256 << 10:
            os.replace(LOG, LOG + '.1')
        with open(LOG, 'a') as f:
            f.write(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + ' ' + line + '\n')
    except OSError:
        pass


def where(x, y, ds):
    for i, b in ds.items():
        if b.x <= x < b.x + b.w and b.y <= y < b.y + b.h:
            return i
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--period', type=float, default=1.0)
    a = ap.parse_args()
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    target, ds, refreshed, inside, last, outside_since = None, {}, 0, None, None, 0
    write('START cursor watch (read-only)')
    while True:
        now = time.time()
        if now - refreshed > 60:
            target, ds, refreshed = streamed_display(), displays(), now
        x, y = cursor()
        if target is not None:
            on = where(x, y, ds)
            ok = on == target
            if inside is not None and ok != inside:
                if not ok:
                    write(f'LEFT streamed display {target}: cursor {x:.0f},{y:.0f} now on display {on}'
                          f'{" (main)" if on == CG.CGMainDisplayID() else ""}, was {last[0]:.0f},{last[1]:.0f}; front app {front_app()}')
                    outside_since = now
                else:
                    write(f'BACK on display {target} at {x:.0f},{y:.0f} after {now - outside_since:.0f} s; front app {front_app()}')
            elif not ok and inside is False and now - outside_since > 300:
                write(f'still outside display {target}: cursor {x:.0f},{y:.0f}; front app {front_app()}')
                outside_since = now
            inside = ok
        last = (x, y)
        time.sleep(a.period)


if __name__ == '__main__':
    main()
