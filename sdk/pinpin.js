// pinpin.js: the mini-app SDK. The server injects it into every app page (with pinpin.css), so apps never include it.
// Apps run sandboxed (an opaque-origin iframe, see host.js): storage, network and Claude go through the host page,
// which acts only for this app.
//
//   pinpin.app, pinpin.screen       ids of this app and of the wall screen showing it ("preview:<app>" in a preview)
//   await pinpin.ready              manifest and stored data loaded; then pinpin.manifest, pinpin.config
//   pinpin.onVoice(fn)              fn(text, info) for each dictated phrase sent to this screen (POST /api/screens/N/say)
//   pinpin.store.get(k, dflt)       per-app storage, kept on the Mini: survives reloads, new browser profiles, and is
//   pinpin.store.set(k, v)            shared by every screen showing this app (previews read it but never write it)
//   pinpin.fetch(url)               GET through the Mini proxy (hosts must be in manifest permissions.fetch); resolves
//                                   to a Response; response.headers 'X-Proxy-Cache' is 'stale' when the upstream is down
//   pinpin.fetchJSON(url)           the same, parsed; throws on HTTP errors
//   pinpin.ask(prompt, {system, json, model})  -> {text, json?, stub?, error?}: Claude via the assistant
//                                   (permissions.ask); model 'haiku' answers in seconds, the default takes 10-30 s
//   pinpin.send(app, kind, data)    -> {ok, error?}: a message to another app (manifest permissions.send lists it, and
//                                   the target's manifest "accepts" the kind); it waits on the Mini until that app is open
//   pinpin.onMessage(fn)            fn({from, kind, data, at}) for each message to this app; return true (or a promise
//                                   of true) once handled, and it is removed from the inbox
//   pinpin.toast(text, ms)          a big transient message;  pinpin.status(text)  the small corner line
//   pinpin.offline(on, text)        a small "offline" badge (e.g. when showing cached data)
//   pinpin.fit(el, {max, min})      shrink an element's font until it fits its box (long titles on small screens)
//   pinpin.escape(s)                text -> safe HTML
(function () {
  'use strict';
  const me = document.currentScript;
  const P = window.pinpin = { app: me.dataset.app, screen: me.dataset.screen, manifest: {}, config: {}, connected: false };
  const voiceFns = [], pending = {}, msgFns = [], msgQueue = [];
  let data = {}, seq = 0, initDone;
  const inited = new Promise((r) => { initDone = r; });

  function send(m) { parent.postMessage(m, '*'); }
  function call(m) { const id = ++seq; return new Promise((r) => { pending[id] = r; send(Object.assign(m, { id })); }); }
  window.addEventListener('message', (e) => {
    if (e.source !== parent || !e.data) return;
    const m = e.data;
    if (m.t === 'init') { P.manifest = m.manifest || {}; data = m.data || {}; P.connected = !!m.connected; initDone(); }
    else if (m.t === 'voice') voice(m.text, m.info);
    else if (m.t === 'message') { msgFns.length ? message(m.msg) : msgQueue.push(m.msg); }
    else if (m.t === 'net') P.connected = m.connected;
    else if ((m.t === 'fetch' || m.t === 'ask' || m.t === 'send') && pending[m.id]) { pending[m.id](m); delete pending[m.id]; }
    else if (m.t === 'error') console.error('pinpin host: ' + m.error);
  });

  // ---- storage (a copy here; the host keeps it on the Mini)
  P.store = {
    get(k, dflt) { return Object.prototype.hasOwnProperty.call(data, k) ? JSON.parse(JSON.stringify(data[k])) : dflt; },
    set(k, v) { data[k] = JSON.parse(JSON.stringify(v)); send({ t: 'store', data }); },
    remove(k) { delete data[k]; send({ t: 'store', data }); },
    all() { return JSON.parse(JSON.stringify(data)); },
  };

  // ---- network
  P.fetch = async (url) => {
    const r = await call({ t: 'fetch', url: String(url) });
    if (r.status === 0) throw new TypeError('network: ' + (r.error || 'unreachable'));
    return new Response(r.body, { status: r.status, headers: { 'Content-Type': r.type || 'text/plain', 'X-Proxy-Cache': r.cache || '' } });
  };
  P.fetchJSON = async (url) => {
    const r = await P.fetch(url);
    if (!r.ok) throw new Error(String(url).split('?')[0] + ': HTTP ' + r.status);
    const j = await r.json();
    Object.defineProperty(j, '_stale', { value: r.headers.get('X-Proxy-Cache') === 'stale' });
    return j;
  };
  P.ask = async (prompt, o = {}) => {
    const d = await call({ t: 'ask', prompt, system: o.system, json: o.json, model: o.model });
    delete d.t; delete d.id;
    if (o.json && d.json === undefined && d.text) { try { d.json = JSON.parse(d.text.replace(/^```(json)?|```$/gm, '')); } catch (e) {} }
    return d;
  };

  // ---- app-to-app messages (through the host and the Mini, never directly)
  P.send = async (to, kind, data) => {
    const r = await call({ t: 'send', to: String(to), kind: String(kind), data: JSON.parse(JSON.stringify(data ?? null)) });
    return { ok: !!r.ok, error: r.error };
  };
  P.onMessage = (fn) => { msgFns.push(fn); msgQueue.splice(0).forEach(message); };
  async function message(msg) {
    let handled = false;
    for (const fn of msgFns) { try { handled = (await fn({ from: msg.from, kind: msg.kind, data: msg.data, at: msg.at })) === true || handled; } catch (e) { console.error(e); } }
    if (handled) send({ t: 'ack', ids: [msg.id] });
  }

  // ---- voice
  P.onVoice = (fn) => { voiceFns.push(fn); };
  function voice(text, info) {
    for (const fn of voiceFns) { try { fn(text, info || {}); } catch (e) { console.error(e); } }
  }
  P._voice = voice;

  // ---- ui helpers
  let toastEl, statusEl, offEl, toastTimer;
  function el(cls) { const e = document.createElement('div'); e.className = cls; document.body.appendChild(e); return e; }
  P.toast = (text, ms = 2500) => {
    toastEl = toastEl || el('pp-toast');
    toastEl.textContent = text; toastEl.classList.add('pp-show');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => toastEl.classList.remove('pp-show'), ms);
  };
  P.status = (text) => { statusEl = statusEl || el('pp-status'); statusEl.textContent = text || ''; };
  P.offline = (on, text) => {
    offEl = offEl || el('pp-offline');
    offEl.textContent = text || 'offline'; offEl.style.display = on ? '' : 'none';
  };
  P.fit = (e, o = {}) => {  // max/min: factors of the element's own CSS font size
    if (!e.dataset.ppBase) { e.style.fontSize = ''; e.dataset.ppBase = parseFloat(getComputedStyle(e).fontSize); }
    const base = +e.dataset.ppBase, max = o.max || 1, min = o.min || 0.4;
    let s = max; e.style.fontSize = base * s + 'px';
    while (s > min && (e.scrollWidth > e.clientWidth + 1 || e.scrollHeight > e.clientHeight + 1)) { s -= 0.05; e.style.fontSize = base * s + 'px'; }
  };
  P.escape = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ---- start
  P.ready = (async () => {
    send({ t: 'hello' });
    await inited;
    P.config = Object.assign({}, P.manifest.config || {}, P.store.get('config', {}));
    if (document.readyState === 'loading') await new Promise((r) => document.addEventListener('DOMContentLoaded', r));
    return P;
  })();
})();
