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

  const HDR = { 'X-PinPin': '1' };  // the server only accepts proxy/data/ask calls that carry it (frames can't)
  function post(msg) { if (frame.contentWindow) frame.contentWindow.postMessage(msg, '*'); }
  function voice(text, info) { const m = { t: 'voice', text, info: info || {} }; ready ? post(m) : queue.push(m); }

  function save() {
    if (preview) return;  // previews and render checks never overwrite what the real screens keep
    if (!loaded) return;  // see above: merged and written once the load succeeds
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      fetch('/api/apps/' + app + '/data', { method: 'PUT', body: JSON.stringify(data), headers: Object.assign({ 'Content-Type': 'application/json' }, HDR) })
        .catch(() => {});
    }, 400);
  }

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

  const delivered = new Set();
  async function inbox() {
    try {
      const r = await fetch('/api/apps/' + app + '/inbox', { cache: 'no-store', headers: HDR });
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
    es.addEventListener('reload', () => location.reload());
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
