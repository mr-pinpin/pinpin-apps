# Writing a mini-app (for the assistant)

Give this to Claude when it creates or changes an app. Every write is one git commit, and the screens showing the
app reload by themselves.

## The call

```
PUT http://192.168.0.50:57830/api/apps/<id>
{"files": {"index.html": "...", "manifest.json": "..."}, "message": "what changed and why", "by": "assistant",
 "check": true, "say": ["a phrase to try"]}
```
- `id`: lowercase letters, digits, `-` and `_`, up to 48 characters. The folder name and `manifest.id` must match.
- `files` patches the folder; `null` deletes a file. Add `"replace": true` to make `files` the whole app.
- `check: true` renders the app in headless Chrome (1920x1200) after the commit. The reply then includes
  `check.errors` (JS exceptions and console errors) and `check.text` (what is on screen). `say` speaks those phrases
  to the app first. **If `check.errors` is not empty, fix the app and PUT again.** If the text is not what the user
  asked for, change the app.
- Put it on a screen: `POST /api/screens/<screen id>/app {"app": "<id>"}`.
- Undo: `GET /api/apps/<id>/history`, then `POST /api/apps/<id>/rollback {"commit": "<sha>"}`.
- Read the current files before changing them: `GET /api/apps/<id>` returns them all as text.

## manifest.json

```json
{"id": "timer", "name": "Kitchen timer", "description": "One line for the app list",
 "size": {"aspect": "16:10", "preferred_px": [1920, 1200], "distance_m": 1.5},
 "permissions": {"fetch": [], "voice": true, "ask": false, "storage": true},
 "config": {}}
```
- `permissions.fetch` lists every host the app reaches (through `pinpin.fetch`). Pick APIs that need no key, such as
  open-meteo; keys live on the Mini, never in the app.
- `ask: true` is needed for `pinpin.ask` (Claude).

## index.html rules

- Plain HTML, CSS and JS in one file (more files only if they help). There is no build step and no frameworks from
  CDNs. The screen may be offline, so anything external must fail gracefully.
- Don't include the SDK: `pinpin.css` and `pinpin.js` are injected. Start your logic with `pinpin.ready.then(...)`.
- The screen is small, 1 to 3 m away in the headset: **few words, huge type**.
  - Use the SDK's rem scale (1rem ≈ 1/30 of the width). Body text is `1rem`, nothing below `0.6rem`, and a key number
    is `pp-huge`.
  - At most about 12 lines of text on screen.
- There is no mouse and no keyboard, so no hover, no buttons that must be clicked, and no forms. Input is voice:
  `pinpin.onVoice(text => ...)`.
  - Phrases arrive as dictated, lower or mixed case, sometimes with filler words. Match loosely.
  - Always show a one-line hint (`.pp-hint`) of what can be said.
- Keep state with `pinpin.store.set(k, v)` / `pinpin.store.get(k, default)`. It survives reloads and new browser
  profiles.
- Dark background, and colours from the CSS variables (`--pp-accent`, `--pp-good`, `--pp-bad`, `--pp-muted`).
- Nothing should scroll; fit the content into the window (`pinpin.fit(el)` shrinks long text).
- `pinpin.ask` takes 10 to 30 s. Show something useful at once (a local fallback or a "thinking" line), then update.
- Escape any text you put into HTML (`pinpin.escape`).

## Example

```html
<!doctype html><html><head><meta charset="utf-8"><title>Timer</title>
<style> #t { font-size: 7rem; font-weight: 800; text-align: center; } </style></head>
<body class="pp-center">
<div id="t">0:00</div><div class="pp-hint">Say <b>5 minutes</b> · <b>stop</b></div>
<script>
let end = 0;
pinpin.onVoice(t => {
  const m = t.match(/(\d+)\s*(min|minute)/i);
  if (m) { end = Date.now() + m[1] * 60000; pinpin.store.set('end', end); }
  else if (/stop|cancel/i.test(t)) { end = 0; pinpin.store.set('end', 0); }
});
pinpin.ready.then(() => {
  end = pinpin.store.get('end', 0);
  setInterval(() => {
    const s = Math.max(0, Math.round((end - Date.now()) / 1000));
    document.getElementById('t').textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }, 250);
});
</script></body></html>
```
