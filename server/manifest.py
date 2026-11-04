"""App folders and manifests: validation of what the assistant writes, before anything touches the repo.

An app is apps/<id>/ with index.html and manifest.json. Stdlib only (runs on the Mini and in the VPS tests).
"""
import json
import posixpath
import re

ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,47}$')  # also a valid house.json ID (schema.ID)
HOST = re.compile(r'^[a-z0-9.-]+(:\d{1,5})?$')
PATH = re.compile(r'^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$')
EXT = {'.html', '.js', '.css', '.json', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.woff2', '.txt', '.md'}
MAX_FILE = 2 << 20
MAX_APP = 8 << 20
MAX_FILES = 64
PERMS = {'fetch', 'voice', 'ask', 'storage', 'send'}


def check_id(app_id):
    return isinstance(app_id, str) and bool(ID.match(app_id))


def check_path(p):
    """'' when p is an acceptable file path inside an app folder, else why not."""
    if not isinstance(p, str) or not PATH.match(p) or len(p) > 200:
        return f'{p!r}: use letters, digits, _ . - and / only'
    if any(part in ('.', '..') or part.startswith('.') for part in p.split('/')):
        return f'{p!r}: no dot files or dot segments'
    if posixpath.splitext(p)[1].lower() not in EXT:
        return f'{p!r}: extension not one of {sorted(EXT)}'
    return ''


def validate(m, app_id):
    """Problems with a parsed manifest.json for app app_id (empty list when valid)."""
    e = []
    if not isinstance(m, dict):
        return ['manifest: not an object']
    if m.get('id') != app_id:
        e.append(f'id: {m.get("id")!r}, must equal the folder name {app_id!r}')
    for k in ('name', 'description'):
        if not (isinstance(m.get(k), str) and 0 < len(m[k]) <= 300):
            e.append(f'{k}: needs a non-empty string')
    size = m.get('size', {})
    if not isinstance(size, dict):
        e.append('size: not an object')
    else:
        for k in ('min_px', 'preferred_px'):
            v = size.get(k)
            if v is not None and not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) and 0 < x <= 8192 for x in v)):
                e.append(f'size.{k}: needs [width, height] in pixels')
        if 'distance_m' in size and not (isinstance(size['distance_m'], (int, float)) and 0 < size['distance_m'] < 50):
            e.append('size.distance_m: needs metres')
    perms = m.get('permissions', {})
    if not isinstance(perms, dict):
        e.append('permissions: not an object')
    else:
        for k in perms:
            if k not in PERMS:
                e.append(f'permissions.{k}: unknown, use {sorted(PERMS)}')
        f = perms.get('fetch', [])
        if not (isinstance(f, list) and all(isinstance(h, str) and HOST.match(h) for h in f)):
            e.append('permissions.fetch: needs a list of host names (host or host:port)')
        snd = perms.get('send', [])
        if not (isinstance(snd, list) and all(isinstance(x, str) and ID.match(x) for x in snd)):
            e.append('permissions.send: needs a list of app ids this app may message')
        for k in ('voice', 'ask', 'storage'):
            if k in perms and not isinstance(perms[k], bool):
                e.append(f'permissions.{k}: not a boolean')
    if 'icon' in m and not (isinstance(m['icon'], str) and not check_path(m['icon']) and m['icon'].lower().endswith(('.svg', '.png'))):
        e.append('icon: a .svg or .png file in the app folder, e.g. "icon.svg" (square, readable at 64 px)')
    acc = m.get('accepts', [])
    if not (isinstance(acc, list) and all(isinstance(x, str) and re.match(r'^[a-z][a-z0-9-]{0,31}$', x) for x in acc)):
        e.append('accepts: a list of message kinds other apps may send this one (e.g. "add-items")')
    if 'hidden' in m and not isinstance(m['hidden'], bool):
        e.append('hidden: not a boolean')
    if 'config' in m and not isinstance(m['config'], dict):
        e.append('config: not an object')
    return e


def validate_files(app_id, files, existing):
    """files: {path: str|None} from a write (None deletes); existing: {path: bytes} now in the folder.

    Returns (problems, result) where result is the folder's {path: bytes} after the write."""
    e = []
    if not check_id(app_id):
        return [f'app id {app_id!r}: lowercase letters, digits, _ and -, at most 48'], None
    if not isinstance(files, dict) or not files:
        return ['files: needs an object {path: text}'], None
    if len(files) > MAX_FILES:
        return [f'files: more than {MAX_FILES}'], None
    out = dict(existing)
    for p, body in files.items():
        why = check_path(p)
        if why:
            e.append(why); continue
        if body is None:
            out.pop(p, None); continue
        if not isinstance(body, str):
            e.append(f'{p!r}: content must be text (null deletes)'); continue
        b = body.encode('utf-8')
        if len(b) > MAX_FILE:
            e.append(f'{p!r}: over {MAX_FILE} bytes')
        out[p] = b
    if e:
        return e, None
    if 'index.html' not in out:
        e.append('index.html: required')
    if 'manifest.json' not in out:
        e.append('manifest.json: required')
    else:
        try:
            e += validate(json.loads(out['manifest.json']), app_id)
        except ValueError as x:
            e.append(f'manifest.json: invalid JSON ({x})')
        else:
            icon = json.loads(out['manifest.json']).get('icon') if isinstance(json.loads(out['manifest.json']), dict) else None
            if isinstance(icon, str) and icon not in out:
                e.append(f'icon: {icon} is not among the app files')
    if sum(len(b) for b in out.values()) > MAX_APP:
        e.append(f'app: over {MAX_APP} bytes in all')
    return e, (out if not e else None)
