// host.js: the trusted page around an app. The app runs in <iframe sandbox="allow-scripts">: an opaque origin with no
// cookies, no localStorage and no same-origin access to this server. Everything privileged goes through postMessage
// to here, and this page only ever acts for its own app id:
//   store   the app's data (GET/PUT /api/apps/<app>/data; previews read it but never write it back)
//   fetch   through /proxy?app=<app> (the server enforces the manifest's permissions.fetch)
//   ask     POST /api/ask with app=<app> (the server enforces permissions.ask)
//   voice   server-sent events for this screen, forwarded into the frame; reload on app changes
//   send    app-to-app messages: posted to the target's inbox on the Mini stamped with THIS app's id (the app can't
//           forge a sender); the server checks the sender's permissions.send and the target's accepts
//   inbox   this app's waiting messages, delivered into the frame; acknowledged ones are removed (not by previews)
// Dev: ?dev=1 adds a text box that feeds the app's onVoice; ?say=a|b|c scripts phrases (render checks).
(function () {
  'use strict';
  const me = document.currentScript;
  const app = me.dataset.app, screen = me.dataset.screen;
  const preview = screen.startsWith('preview:');
  const frame = document.getElementById('app');
  const H = window.pinpinHost = { app, screen, connected: false, voice };
  let manifest = {}, data = {}, saveTimer = null, ready = false, queue = [];
  // never write before the real data has loaded: a write from a page that started while the Mini was unreachable would
  // replace the app's saved data with a partial copy. Until then changes are kept here and merged on top.
  let loaded = false, pendingKeys = new Set();
  // versioned saves: `synced` is the copy the Mini confirmed (version `etag`); the keys where `data` differs from it are
  // ours to save. A save carries the version it was based on; if another screen saved first the Mini answers 409 with
  // its copy, and only our changed keys are merged onto it. Failed saves are retried until they land.
  let synced = {}, etag = null, saving = null, retryMs = 0, retryTimer = null;
  const dirtyKeys = () => [...new Set([...Object.keys(data), ...Object.keys(synced)])].filter((k) => JSON.stringify(data[k]) !== JSON.stringify(synced[k]));

  const HDR = { 'X-PinPin': '1' };  // the server only accepts proxy/data/ask calls that carry it (frames can't)
  function post(msg) { if (frame.contentWindow) frame.contentWindow.postMessage(msg, '*'); }
  function voice(text, info) { const m = { t: 'voice', text, info: info || {} }; ready ? post(m) : queue.push(m); }

  function save() {
    if (preview) return;  // previews and render checks never overwrite what the real screens keep
    if (!loaded) return;  // see above: merged and written once the load succeeds
    clearTimeout(saveTimer);
    saveTimer = setTimeout(sync, 400);
  }
  async function sync() {  // -> true once everything we changed is on the Mini
    if (preview || !loaded) return true;
    if (saving) { await saving; return dirtyKeys().length ? sync() : true; }
    if (!dirtyKeys().length) return true;
    clearTimeout(saveTimer); clearTimeout(retryTimer);
    saving = (async () => {
      for (let round = 0; round < 5; round++) {
        const sent = JSON.parse(JSON.stringify(data));
        let r;
        try {
          r = await fetch('/api/apps/' + app + '/data', { method: 'PUT', body: JSON.stringify(sent),
            headers: Object.assign({ 'Content-Type': 'application/json' }, etag ? { 'If-Match': etag } : {}, HDR) });
        } catch (e) { r = null; }
        if (r && r.ok) { synced = sent; etag = r.headers.get('ETag') || etag; retryMs = 0; return true; }
        if (r && r.status === 409) {  // another screen saved first: our changed keys onto its copy, then again
          const theirs = await r.json().catch(() => ({})), mine = dirtyKeys();
          const merged = JSON.parse(JSON.stringify(theirs));
          for (const k of mine) { if (k in data) merged[k] = data[k]; else delete merged[k]; }
          synced = theirs; etag = r.headers.get('ETag'); data = merged;
          needRestart = true;  // the app should show what the other screen added
          continue;
        }
        break;  // offline, or refused: keep the change and retry later
      }
      retryMs = Math.min(30000, (retryMs || 1000) * 2);
      retryTimer = setTimeout(sync, retryMs);
      return false;
    })();
    const ok = await saving; saving = null;
    if (ok && needRestart) restartApp();
    return ok;
  }
  let needRestart = false;
  function restartApp() { needRestart = false; if (ready) { ready = false; frame.src = frame.src; } }
  async function flushThen(fn) {  // never drop a pending change: save (up to 2 s) before reloading
    if (dirtyKeys().length) await Promise.race([sync(), new Promise((r) => setTimeout(r, 2000))]);
    fn();
  }
  addEventListener('pagehide', () => {  // last chance (the window closes): a fire-and-forget save
    if (!preview && loaded && dirtyKeys().length) fetch('/api/apps/' + app + '/data', { method: 'PUT', keepalive: true,
      body: JSON.stringify(data), headers: Object.assign({ 'Content-Type': 'application/json' }, etag ? { 'If-Match': etag } : {}, HDR) }).catch(() => {});
  });

  async function handle(m) {
    if (m.t === 'hello') {
      post({ t: 'init', app, screen, manifest, data, connected: H.connected });
      ready = true; queue.splice(0).forEach(post);
      inbox();
    } else if (m.t === 'store' && m.data && typeof m.data === 'object') {
      const s = JSON.stringify(m.data);
      if (s.length > 200000) return post({ t: 'error', error: 'app data over 200 KB' });
      const next = JSON.parse(s);
      if (!loaded) for (const k of new Set([...Object.keys(data), ...Object.keys(next)])) {
        if (JSON.stringify(data[k]) !== JSON.stringify(next[k])) pendingKeys.add(k);
      }
      data = next; save();
    } else if (m.t === 'fetch') {
      let out;
      try {
        const r = await fetch('/proxy?app=' + encodeURIComponent(app) + '&url=' + encodeURIComponent(String(m.url)), { headers: HDR });
        out = { ok: r.ok, status: r.status, body: await r.text(), type: r.headers.get('Content-Type') || '', cache: r.headers.get('X-Proxy-Cache') || '' };
      } catch (e) { out = { ok: false, status: 0, body: '', error: String(e) }; }
      post(Object.assign({ t: 'fetch', id: m.id }, out));
    } else if (m.t === 'send') {
      let out;
      try {
        const r = await fetch('/api/apps/' + encodeURIComponent(String(m.to)) + '/inbox', { method: 'POST',
          headers: Object.assign({ 'Content-Type': 'application/json' }, HDR), body: JSON.stringify({ from: app, kind: String(m.kind), data: m.data, dry: preview }) });  // previews: checked, not delivered
        const d = await r.json().catch(() => ({}));
        out = r.ok ? { ok: true } : { ok: false, error: d.error || ('HTTP ' + r.status) };
      } catch (e) { out = { ok: false, error: 'the Mini is not answering' }; }
      post(Object.assign({ t: 'send', id: m.id }, out));
    } else if (m.t === 'ack' && Array.isArray(m.ids) && !preview) {
      fetch('/api/apps/' + app + '/inbox/ack', { method: 'POST', headers: Object.assign({ 'Content-Type': 'application/json' }, HDR),
        body: JSON.stringify({ ids: m.ids.map(String) }) }).catch(() => {});
    } else if (m.t === 'ask') {
      let out;
      try {
        const r = await fetch('/api/ask', { method: 'POST', headers: Object.assign({ 'Content-Type': 'application/json' }, HDR),
          body: JSON.stringify({ app, screen, prompt: String(m.prompt || ''), system: m.system, json: m.json, model: m.model }) });
        out = await r.json().catch(() => ({}));
        if (!r.ok) out = { text: '', stub: true, error: out.error || ('HTTP ' + r.status) };
      } catch (e) { out = { text: '', stub: true, error: 'assistant unreachable' }; }
      post(Object.assign({ t: 'ask', id: m.id }, out));
    }
  }
  window.addEventListener('message', (e) => {
    if (e.source !== frame.contentWindow || !e.data || typeof e.data !== 'object') return;
    handle(e.data);
  });

  const delivered = new Set(), PAGE = Math.random().toString(36).slice(2, 10);
  async function inbox() {
    try {
      if (preview) return;  // previews neither claim nor acknowledge: the real screens get the messages
      const r = await fetch('/api/apps/' + app + '/inbox?claim=' + encodeURIComponent(screen + '-' + PAGE), { cache: 'no-store', headers: HDR });
      if (!r.ok) return;
      for (const msg of await r.json()) {
        if (delivered.has(msg.id)) continue;
        delivered.add(msg.id); post({ t: 'message', msg });
      }
    } catch (e) {}
  }

  function connect() {
    const es = new EventSource('/events?screen=' + encodeURIComponent(screen));
    es.addEventListener('hello', () => { H.connected = true; dot(); post({ t: 'net', connected: true }); });
    es.addEventListener('reload', () => flushThen(() => location.reload()));
    es.addEventListener('store', async (e) => {  // another screen saved this app's data
      const d = JSON.parse(e.data);
      if (!loaded || d.etag === etag) return;
      if (dirtyKeys().length) return sync();  // ours is pending: the save will merge (409) and restart the app
      try {
        const r = await fetch('/api/apps/' + app + '/data', { cache: 'no-store', headers: HDR });
        if (r.ok) { data = synced = await r.json(); etag = r.headers.get('ETag'); restartApp(); }
      } catch (err) {}
    });
    es.addEventListener('hello', () => { if (dirtyKeys().length) sync(); });  // back online: save what waited
    es.addEventListener('voice', (e) => { const d = JSON.parse(e.data); voice(d.text, d); });
    es.addEventListener('message', () => { if (ready) inbox(); });
    es.onerror = () => { H.connected = false; dot(); post({ t: 'net', connected: false }); };
  }
  let dotEl;
  function dot() { if (dotEl) dotEl.style.background = H.connected ? '#5fd38d' : '#ff6b6b'; }

  (async () => {
    try { manifest = await (await fetch('/apps/' + app + '/manifest.json', { cache: 'no-store' })).json(); } catch (e) {}
    async function loadData() {
      try {
        const r = await fetch('/api/apps/' + app + '/data', { cache: 'no-store', headers: HDR });
        if (!r.ok) return false;
        const server = await r.json();
        etag = r.headers.get('ETag'); synced = JSON.parse(JSON.stringify(server));
        if (pendingKeys.size) {  // changes made while offline win over the loaded copy, key by key
          for (const k of pendingKeys) { if (k in data) server[k] = data[k]; else delete server[k]; }
          pendingKeys.clear();
          data = server; loaded = true; save();
        } else { data = server; loaded = true; }
        if (ready) { ready = false; frame.src = frame.src; }  // the app started without its data: restart it with the real copy
        return true;
      } catch (e) { return false; }
    }
    if (!(await loadData())) {  // the Mini is unreachable: retry, and don't write until it answers
      const retry = setInterval(async () => { if (await loadData()) clearInterval(retry); }, 5000);
    }
    dotEl = document.createElement('div'); dotEl.className = 'host-dot'; document.body.appendChild(dotEl); dot();
    connect();
    // the frame starts loading only now, so its hello always finds manifest and data here
    frame.src = '/frame/' + app + '/?screen=' + encodeURIComponent(screen);
    const qs = new URLSearchParams(location.search);
    if (qs.has('say')) qs.get('say').split('|').forEach((t, i) => setTimeout(() => voice(t, { scripted: true }), 1500 + i * 1500));
    if (qs.has('dev')) {
      const f = document.createElement('form'); f.className = 'host-dev';
      f.innerHTML = '<input placeholder="say something to this app" autofocus>';
      f.onsubmit = (e) => { e.preventDefault(); const i = f.firstChild; if (i.value.trim()) voice(i.value.trim(), { dev: true }); i.value = ''; };
      document.body.appendChild(f);
    }
  })();
})();
