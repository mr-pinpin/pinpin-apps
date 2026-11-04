// host.js: the trusted page around an app. The app runs in <iframe sandbox="allow-scripts">: an opaque origin with no
// cookies, no localStorage and no same-origin access to this server. Everything privileged goes through postMessage
// to here, and this page only ever acts for its own app id:
//   store   the app's data (GET/PUT /api/apps/<app>/data; previews read it but never write it back)
//   fetch   through /proxy?app=<app> (the server enforces the manifest's permissions.fetch)
//   ask     POST /api/ask with app=<app> (the server enforces permissions.ask)
//   voice   server-sent events for this screen, forwarded into the frame; reload on app changes
// Dev: ?dev=1 adds a text box that feeds the app's onVoice; ?say=a|b|c scripts phrases (render checks).
(function () {
  'use strict';
  const me = document.currentScript;
  const app = me.dataset.app, screen = me.dataset.screen;
  const preview = screen.startsWith('preview:');
  const frame = document.getElementById('app');
  const H = window.pinpinHost = { app, screen, connected: false, voice };
  let manifest = {}, data = {}, saveTimer = null, ready = false, queue = [];

  const HDR = { 'X-PinPin': '1' };  // the server only accepts proxy/data/ask calls that carry it (frames can't)
  function post(msg) { if (frame.contentWindow) frame.contentWindow.postMessage(msg, '*'); }
  function voice(text, info) { const m = { t: 'voice', text, info: info || {} }; ready ? post(m) : queue.push(m); }

  function save() {
    if (preview) return;  // previews and render checks never overwrite what the real screens keep
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
    } else if (m.t === 'store' && m.data && typeof m.data === 'object') {
      const s = JSON.stringify(m.data);
      if (s.length > 200000) return post({ t: 'error', error: 'app data over 200 KB' });
      data = JSON.parse(s); save();
    } else if (m.t === 'fetch') {
      let out;
      try {
        const r = await fetch('/proxy?app=' + encodeURIComponent(app) + '&url=' + encodeURIComponent(String(m.url)), { headers: HDR });
        out = { ok: r.ok, status: r.status, body: await r.text(), type: r.headers.get('Content-Type') || '', cache: r.headers.get('X-Proxy-Cache') || '' };
      } catch (e) { out = { ok: false, status: 0, body: '', error: String(e) }; }
      post(Object.assign({ t: 'fetch', id: m.id }, out));
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

  function connect() {
    const es = new EventSource('/events?screen=' + encodeURIComponent(screen));
    es.addEventListener('hello', () => { H.connected = true; dot(); post({ t: 'net', connected: true }); });
    es.addEventListener('reload', () => location.reload());
    es.addEventListener('voice', (e) => { const d = JSON.parse(e.data); voice(d.text, d); });
    es.onerror = () => { H.connected = false; dot(); post({ t: 'net', connected: false }); };
  }
  let dotEl;
  function dot() { if (dotEl) dotEl.style.background = H.connected ? '#5fd38d' : '#ff6b6b'; }

  (async () => {
    try { manifest = await (await fetch('/apps/' + app + '/manifest.json', { cache: 'no-store' })).json(); } catch (e) {}
    try { const r = await fetch('/api/apps/' + app + '/data', { cache: 'no-store', headers: HDR }); if (r.ok) data = await r.json(); } catch (e) {}
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
