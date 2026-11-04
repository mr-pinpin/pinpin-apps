// pinpin.js: the mini-app SDK. The server injects it into every app page (with pinpin.css), so apps never include it.
//
//   pinpin.app, pinpin.screen       ids of this app and of the wall screen showing it ("preview:<app>" in a preview)
//   await pinpin.ready              manifest loaded and stored data restored; then pinpin.manifest, pinpin.config
//   pinpin.onVoice(fn)              fn(text, info) for each dictated phrase sent to this screen (POST /api/screens/N/say)
//   pinpin.store.get(k, dflt)       per-app storage: localStorage now, mirrored to the Mini so a new browser profile
//   pinpin.store.set(k, v)            or another screen showing the same app gets it back
//   pinpin.fetch(url, opts)         fetch through the Mini proxy (hosts must be in manifest permissions.fetch)
//   pinpin.fetchJSON(url)           the same, parsed; throws on HTTP errors
//   pinpin.ask(prompt, {system, json})  -> {text, json?, stub?}: Claude via the assistant (permissions.ask)
//   pinpin.toast(text, ms)          a big transient message;  pinpin.status(text)  the small corner line
//   pinpin.fit(el, {max, min})      shrink an element's font until it fits its box (long titles on small screens)
// Dev: ?dev=1 adds a text box that feeds onVoice (try an app without the headset); ?say=a|b|c scripts phrases.
(function () {
  'use strict';
  const me = document.currentScript;
  const app = me.dataset.app, screen = me.dataset.screen;
  const P = window.pinpin = { app, screen, manifest: null, config: {}, connected: false };
  const voiceFns = [];
  const ns = 'pinpin:' + app + ':';

  // ---- storage
  const mem = {};
  let syncTimer = null;
  function lsGet(k) { try { return localStorage.getItem(ns + k); } catch (e) { return mem[k] ?? null; } }
  function lsSet(k, v) { try { localStorage.setItem(ns + k, v); } catch (e) { mem[k] = v; } }
  function all() {
    const out = {};
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k.startsWith(ns)) out[k.slice(ns.length)] = JSON.parse(localStorage.getItem(k));
      }
    } catch (e) { for (const k in mem) out[k] = JSON.parse(mem[k]); }
    return out;
  }
  function sync() {
    clearTimeout(syncTimer);
    syncTimer = setTimeout(() => {
      fetch('/api/apps/' + app + '/data', { method: 'PUT', body: JSON.stringify(all()), headers: { 'Content-Type': 'application/json' } })
        .catch(() => {});
    }, 500);
  }
  P.store = {
    get(k, dflt) { const v = lsGet(k); if (v === null) return dflt; try { return JSON.parse(v); } catch (e) { return dflt; } },
    set(k, v) { lsSet(k, JSON.stringify(v)); sync(); },
    remove(k) { try { localStorage.removeItem(ns + k); } catch (e) { delete mem[k]; } sync(); },
    all,
  };

  // ---- network
  P.fetch = (url, opts) => fetch('/proxy?app=' + encodeURIComponent(app) + '&url=' + encodeURIComponent(url), opts);
  P.fetchJSON = async (url) => {
    const r = await P.fetch(url);
    if (!r.ok) throw new Error(url.split('?')[0] + ': HTTP ' + r.status);
    return r.json();
  };
  P.ask = async (prompt, o = {}) => {
    const r = await fetch('/api/ask', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ app, screen, prompt, system: o.system, json: o.json }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) return { text: '', stub: true, error: d.error || ('HTTP ' + r.status) };
    if (o.json && d.json === undefined && d.text) { try { d.json = JSON.parse(d.text.replace(/^```(json)?|```$/gm, '')); } catch (e) {} }
    return d;
  };

  // ---- voice and server events
  P.onVoice = (fn) => { voiceFns.push(fn); };
  function voice(text, info) {
    for (const fn of voiceFns) { try { fn(text, info || {}); } catch (e) { console.error(e); } }
  }
  P._voice = voice;
  function connect() {
    const es = new EventSource('/events?screen=' + encodeURIComponent(screen));
    es.addEventListener('hello', () => { P.connected = true; dot(); });
    es.addEventListener('reload', () => { location.reload(); });
    es.addEventListener('voice', (e) => { const d = JSON.parse(e.data); voice(d.text, d); });
    es.onerror = () => { P.connected = false; dot(); };  // EventSource retries by itself
  }

  // ---- ui helpers
  let toastEl, statusEl, dotEl, toastTimer;
  function el(cls, parent) { const e = document.createElement('div'); e.className = cls; (parent || document.body).appendChild(e); return e; }
  P.toast = (text, ms = 2500) => {
    toastEl = toastEl || el('pp-toast');
    toastEl.textContent = text; toastEl.classList.add('pp-show');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => toastEl.classList.remove('pp-show'), ms);
  };
  P.status = (text) => { statusEl = statusEl || el('pp-status'); statusEl.textContent = text || ''; };
  function dot() { if (dotEl) dotEl.classList.toggle('pp-off', !P.connected); }
  P.fit = (e, o = {}) => {
    const max = o.max || 1, min = o.min || 0.4;
    let s = max; e.style.fontSize = s + 'em';
    while (s > min && (e.scrollWidth > e.clientWidth + 1 || e.scrollHeight > e.clientHeight + 1)) { s -= 0.05; e.style.fontSize = s + 'em'; }
  };
  P.escape = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function devBox() {
    const f = document.createElement('form'); f.className = 'pp-dev';
    f.innerHTML = '<input placeholder="say something to this app" autofocus>';
    f.onsubmit = (e) => { e.preventDefault(); const i = f.firstChild; if (i.value.trim()) voice(i.value.trim(), { dev: true }); i.value = ''; };
    document.body.appendChild(f);
  }

  // ---- start
  P.ready = (async () => {
    try { P.manifest = await (await fetch('/apps/' + app + '/manifest.json', { cache: 'no-store' })).json(); } catch (e) { P.manifest = {}; }
    try {  // restore server-side data a fresh profile does not have yet
      const d = await (await fetch('/api/apps/' + app + '/data', { cache: 'no-store' })).json();
      for (const k in d) if (lsGet(k) === null) lsSet(k, JSON.stringify(d[k]));
    } catch (e) {}
    P.config = Object.assign({}, P.manifest.config || {}, P.store.get('config', {}));
    if (document.readyState === 'loading') await new Promise((r) => document.addEventListener('DOMContentLoaded', r));
    dotEl = el('pp-dot pp-off');
    const qs = new URLSearchParams(location.search);
    if (qs.has('dev')) devBox();
    connect();
    if (qs.has('say')) {  // scripted voice for render checks: ?say=eggs, tomatoes|recipe 2|next (one phrase per 1.5 s)
      qs.get('say').split('|').forEach((t, i) => setTimeout(() => voice(t, { scripted: true }), 800 + i * 1500));
    }
    return P;
  })();
})();
