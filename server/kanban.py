"""The captain's kanban board: one JSON file (<data>/kanban.json, crash-safe writes, previous copy kept as .1), served by
apps_server.py at /kanban and /api/kanban, mirrored to quest-wall-screen docs/captain/kanban.json + KANBAN.md by
tools/kanban_mirror.sh (git history), and backed up hourly with the rest of <data>.

  board  {"rev": n, "next": n, "cards": [card]}
  card   {id: "K7", title, details, column, owner, area, links: [str], created, updated, by,
          history: [{at, by, what, ...}], comments: [{at, by, text}]}

Columns: backlog, next, doing, review, done. Links are URLs or "qws:<sha>" / "apps:<sha>" commits or a repo path.
"""
import copy
import json
import os
import re
import threading
import time

COLUMNS = ['backlog', 'next', 'doing', 'review', 'done']
TITLES = {'backlog': 'Backlog', 'next': 'Next', 'doing': 'Doing', 'review': 'Review', 'done': 'Done'}
LIMITS = {'title': 200, 'details': 4000, 'owner': 40, 'area': 40, 'by': 40, 'link': 500, 'links': 20, 'comment': 2000,
          'cards': 2000, 'history': 200, 'comments': 200}
CARD_ID = re.compile(r'^K\d{1,6}$')


class Invalid(Exception):
    pass


class NotFound(Exception):
    pass


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def column(c):
    c = str(c or '').strip().lower()
    if c not in COLUMNS:
        raise Invalid(f'column must be one of {", ".join(COLUMNS)}')
    return c


def text(d, k, required=False):
    v = d.get(k)
    if v is None:
        if required:
            raise Invalid(f'{k} required')
        return None
    if not isinstance(v, str):
        raise Invalid(f'{k} must be a string')
    v = v.strip() if k != 'details' else v.rstrip()
    if required and not v:
        raise Invalid(f'{k} required')
    if len(v) > LIMITS[k]:
        raise Invalid(f'{k} over {LIMITS[k]} characters')
    return v


def links(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = [v]
    if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
        raise Invalid('links must be a list of strings')
    v = [x.strip() for x in v if x.strip()]
    if len(v) > LIMITS['links'] or any(len(x) > LIMITS['link'] for x in v):
        raise Invalid(f'at most {LIMITS["links"]} links of {LIMITS["link"]} characters')
    return v


class Board:
    def __init__(self, path, write):
        """write(path, bytes): the server's atomic_write."""
        self.path, self.write, self.lock = path, write, threading.Lock()
        self.doc = self.load()

    def load(self):
        for p in (self.path, self.path + '.1'):  # a torn or hand-broken file falls back to the previous copy
            try:
                with open(p) as f:
                    d = json.load(f)
                if isinstance(d, dict) and isinstance(d.get('cards'), list):
                    return d
            except (OSError, ValueError):
                pass
        return {'rev': 0, 'next': 1, 'cards': []}

    def save(self):
        self.doc['rev'] += 1
        self.doc['updated'] = now()
        try:  # the previous copy, whole, before the new one replaces it
            with open(self.path, 'rb') as f:
                self.write(self.path + '.1', f.read())
        except OSError:
            pass
        self.write(self.path, json.dumps(self.doc, indent=1, ensure_ascii=False) + '\n')

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.doc)

    def find(self, cid):
        cid = str(cid).strip().upper()
        if not cid.startswith('K'):
            cid = 'K' + cid
        for c in self.doc['cards']:
            if c['id'] == cid:
                return c
        raise NotFound(f'no card {cid}')

    def get(self, cid):
        with self.lock:
            return copy.deepcopy(self.find(cid))

    @staticmethod
    def note(c, entry):
        c['history'].append(entry)
        del c['history'][:-LIMITS['history']]

    def add(self, d):
        title = text(d, 'title', True)
        card = {'title': title, 'details': text(d, 'details') or '', 'column': column(d.get('column') or 'backlog'),
                'owner': text(d, 'owner') or '', 'area': text(d, 'area') or '', 'links': links(d.get('links')) or []}
        by = text(d, 'by') or 'unknown'
        with self.lock:
            if len(self.doc['cards']) >= LIMITS['cards']:
                raise Invalid('board full')
            t = now()
            card.update(id=f'K{self.doc["next"]}', created=t, updated=t, by=by, comments=[],
                        history=[{'at': t, 'by': by, 'what': 'created', 'column': card['column']}])
            self.doc['next'] += 1
            self.doc['cards'].append(card)
            self.save()
            return copy.deepcopy(card)

    def move(self, cid, d):
        to, by, note = column(d.get('column')), text(d, 'by') or 'unknown', text({'comment': d.get('note')}, 'comment')
        with self.lock:
            c = self.find(cid)
            if c['column'] != to:
                t = now()
                e = {'at': t, 'by': by, 'what': 'moved', 'from': c['column'], 'to': to}
                if note:
                    e['note'] = note
                self.note(c, e)
                c['column'], c['updated'] = to, t
                self.save()
            return copy.deepcopy(c)

    def update(self, cid, d):
        by = text(d, 'by') or 'unknown'
        new = {k: text(d, k) for k in ('title', 'details', 'owner', 'area')}
        if new['title'] is not None and not new['title']:
            raise Invalid('title can not be empty')
        new['links'] = links(d.get('links'))
        add = links(d.get('add_links'))
        with self.lock:
            c = self.find(cid)
            if add:
                base = new['links'] if new['links'] is not None else c['links']
                new['links'] = base + [x for x in add if x not in base]
                if len(new['links']) > LIMITS['links']:
                    raise Invalid(f'at most {LIMITS["links"]} links')
            changed = [k for k, v in new.items() if v is not None and v != c[k]]
            if changed:
                t = now()
                self.note(c, {'at': t, 'by': by, 'what': 'updated', 'fields': changed,
                              **{f'old_{k}': c[k] for k in changed if k in ('owner', 'area', 'title')}})
                for k in changed:
                    c[k] = new[k]
                c['updated'] = t
                self.save()
            return copy.deepcopy(c)

    def comment(self, cid, d):
        body, by = text({'comment': d.get('text')}, 'comment', True), text(d, 'by') or 'unknown'
        with self.lock:
            c = self.find(cid)
            t = now()
            c['comments'].append({'at': t, 'by': by, 'text': body})
            del c['comments'][:-LIMITS['comments']]
            self.note(c, {'at': t, 'by': by, 'what': 'commented'})
            c['updated'] = t
            self.save()
            return copy.deepcopy(c)

    def summary(self):
        """For /status: counts per column, and the Doing and Review cards."""
        with self.lock:
            cards = self.doc['cards']
            pick = lambda col: [{k: c[k] for k in ('id', 'title', 'owner', 'area', 'updated')} for c in cards if c['column'] == col]
            return {'rev': self.doc['rev'], 'counts': {col: sum(c['column'] == col for c in cards) for col in COLUMNS},
                    'doing': pick('doing'), 'review': pick('review'), 'next': pick('next'),
                    'done': sorted(pick('done'), key=lambda c: c['updated'], reverse=True)[:5]}


def select(doc, col=None, owner=None, area=None):
    cards = doc['cards']
    if col:
        cards = [c for c in cards if c['column'] == column(col)]
    if owner:
        cards = [c for c in cards if c['owner'].lower() == owner.lower()]
    if area:
        cards = [c for c in cards if c['area'].lower() == area.lower()]
    return cards


def markdown(doc):
    """The read-only mirror, docs/captain/KANBAN.md."""
    out = ['# Kanban', '',
           f'Generated from the board (rev {doc.get("rev", 0)}, {doc.get("updated", "")}). Do not edit this file: '
           'use http://192.168.0.50:57830/kanban, the `kanban` CLI on the VPS, or the API (pinpin-apps docs/KANBAN.md).', '']
    for col in COLUMNS:
        cards = [c for c in doc['cards'] if c['column'] == col]
        if col == 'done':
            cards = sorted(cards, key=lambda c: c['updated'], reverse=True)
        out.append(f'## {TITLES[col]} ({len(cards)})')
        out.append('')
        for c in cards:
            who = ', '.join(x for x in (c['owner'], c['area']) if x)
            out.append(f'- [{"x" if col == "done" else " "}] **{c["id"]}** {c["title"]}' + (f' ({who})' if who else ''))
            if c['details']:
                out += ['  ' + l for l in c['details'].splitlines()[:6]]
            if c['links']:
                out.append('  links: ' + ', '.join(c['links']))
            if c['comments']:
                last = c['comments'][-1]
                out.append(f'  last comment ({last["by"]}, {last["at"]}): ' + last['text'].splitlines()[0][:200])
        if not cards:
            out.append('_(empty)_')
        out.append('')
    return '\n'.join(out)
