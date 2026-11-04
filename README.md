# pinpin-apps: small HTML apps on the walls

Mini-apps for the Mr. PinPin house: small HTML apps that live on the wall screens in the Quest (and later on other
surfaces). Two examples: a recipe app above the oven that suggests dishes from the ingredients you say out loud, and
a weather board. Claude, the per-screen assistant, creates and evolves them on request. Every change is a git
commit in this repo.

```
 assistant (work-42) ──PUT /api/apps/recipes──▶ ┌───────────── Mini :57830 ─────────────┐
 headset dictation ──POST /api/screens/2/say──▶ │ apps_server.py  (git commit per change) │──GET/PUT──▶ house server :57800
                                                │  /screen/2 = the app house.json maps    │             house.json "apps"
                                                └───────▲───────────────────────────────┘
                     Chrome --app window, per screen    │ page + SSE (reload, voice) + /proxy + /api/ask
 laptop: apps_runner.py ── opens it on virtual display N (pinpin-displays :57710) ── atlas ──▶ headset screen N
```

## Layout

| path | what |
|---|---|
| `apps/<id>/` | one app: `index.html`, `manifest.json` and any `.js/.css/.svg/.png/...`. |
| `sdk/pinpin.js`, `sdk/pinpin.css` | the SDK. The server injects it into every app page. |
| `server/apps_server.py` | runs on the Mini: serves apps to screens, the create/update API, the house mapping, events, the proxy and ask. |
| `server/manifest.py` | validation of app folders and manifests. |
| `runner/apps_runner.py` | runs on the laptop: one chromeless Chrome window per app screen, on that screen's virtual display. |
| `docs/AUTHORING.md`, `docs/HEADSET.md` | how the assistant writes an app; the headset's picker protocol |
| `deploy/` | install scripts and LaunchAgents for the Mini and the laptop. |
| `tools/sync.sh` | syncs the VPS checkout ↔ the Mini repo (where the assistant's commits land) ↔ GitHub. |
| `ui/index.html` | the control page (`http://<mini>:57830/`) |
| `sdk/host.js`, `sdk/host.css` | the trusted page around each sandboxed app: storage, proxy, ask and voice for that app only |
| `tools/input_test.sh` | CDP input end to end on the laptop |
| `tools/render_check.py` | headless Chrome over the DevTools protocol, on the Mini: JS errors, visible text, screenshot, and scripted voice (`--say`). |
| `tools/air_test.sh` | end to end on the laptop, under the bench lock: a scratch display, an app, the runner's window, then teardown and air_smoke. |
| `run_tests.sh` | the offline tests. Run it before every deploy. |

## An app

`apps/weather/manifest.json`:
```json
{"id": "weather", "name": "Weather", "description": "...", "version": 1,
 "size": {"aspect": "16:10", "min_px": [800, 500], "preferred_px": [1920, 1200], "distance_m": 2.5},
 "permissions": {"fetch": ["api.open-meteo.com"], "voice": true, "ask": false, "storage": true},
 "config": {"place": "Moscow", "lat": 55.7558, "lon": 37.6173}}
```
- `permissions.fetch`: the only hosts the app can reach, through `/proxy`.
- `ask`: allows `pinpin.ask` (Claude).
- `config`: the app's defaults. `pinpin.store.set('config', {...})` overrides them per install, for example when you say "weather in Paris".

`index.html` is plain HTML with no build step. Don't include the SDK yourself: the page is served with a
`<base href="/apps/<id>/">`, `pinpin.css` and `pinpin.js` injected into its `<head>`.

### SDK (`window.pinpin`)

| call | |
|---|---|
| `await pinpin.ready` | manifest and stored data loaded; `pinpin.config` = manifest config + stored overrides |
| `pinpin.onVoice(fn)` | `fn(text)` for each phrase dictated to this screen |
| `pinpin.store.get(k, d)` / `.set(k, v)` | per-app storage: localStorage, mirrored to the Mini (survives a new browser profile) |
| `pinpin.fetch(url)` / `pinpin.fetchJSON(url)` | through the Mini proxy (allowlist, 60 s cache, stale copy when the upstream is down, API keys added server-side from `secrets.json`) |
| `pinpin.ask(prompt, {system, json})` | → `{text, json?, stub?}`: Claude through the assistant service. `stub: true` means no assistant is configured, so the app falls back to its own logic |
| `pinpin.toast(t)`, `pinpin.status(t)`, `pinpin.fit(el)`, `pinpin.escape(s)` | UI helpers |

Style: `1rem` = 1/30 of the window width, so a 1920x1200 window has 64 px body text. The screens are small and
metres away. Classes: `pp-huge pp-big pp-small pp-muted pp-accent pp-card pp-chip pp-row pp-grid pp-grow pp-center pp-hint`.
Dark, high contrast, no hover states (there is no mouse), and no cursor.

To try an app in a desktop browser, open `http://<mini>:57830/app/<id>/?dev=1`. A text box at the bottom feeds `onVoice`.
`?say=eggs, tomatoes|recipe 2` scripts phrases instead. On the Mini, `python3 tools/render_check.py <url> --say ... --shot x.png`
reports JS errors and the text on the page.

## API (Mini :57830)

| | |
|---|---|
| `GET /api/apps` | list (id, name, description, size, permissions, screens) |
| `GET /api/apps/<id>` | `{manifest, files: {path: text}, commit, screens}` |
| `PUT /api/apps/<id>` | `{files: {path: text \| null}, message, by?, replace?}`: validated, written, **one commit**, screens showing it reload. → `{commit, changed, screens, preview}`. `null` deletes a file. `replace: true` makes `files` the whole folder. |
| `DELETE /api/apps/<id>?message=` | removes it (one commit) and unassigns its screens |
| `GET /api/apps/<id>/history`, `POST /api/apps/<id>/rollback {commit}` | git history; a rollback is a new commit |
| `GET /api/screens` | `[{screen, app, display, role, on, w_m, h_m, clients}]` |
| `POST /api/screens/<id>/app {app \| null}` | writes house.json `apps` (If-Match, retried on 409), and the screen reloads |
| `POST /api/screens/<id>/say {text}` | dictation to the app on that screen |
| `POST /api/screens/<id>/reload` | |
| `POST /api/screens/<id>/open {url \| null}` | the runner navigates that screen's window to the URL (and creates the window if needed). `null` goes back to the app. Poll `GET /api/screens` until `shown_seq == seq` |
| `POST /api/ask {app, screen, prompt, system?, json?}` | → the assistant (`--ask-url`, work-42's `/api/ask`) or the stub |
| `GET /proxy?app=&url=` | GET only, to hosts in the app's `permissions.fetch` |
| `GET /screen/<id>`, `GET /app/<id>/`, `GET /events?screen=` | the page a screen shows, a preview, server-sent events |

### house.json

A top-level section maps screens to apps (schema in quest-wall-screen `house/schema.py`):
`"apps": [{"screen": 2, "app": "recipes"}]`. Only this server writes it, with `by: "apps"`. The headset never
writes it, so a headset re-applying a screen edit after a conflict can't undo an assignment. The screen id is
the headset's screen id, and its `source.screen` is the laptop virtual display that the runner draws on.

## Run and deploy

```
bash run_tests.sh                      # offline: server, house mapping (the real house server), runner plan
bash deploy/install-mini.sh            # the Mini: repo sync, LaunchAgent io.mr-pinpin.apps, /health
bash deploy/install-air.sh             # the laptop: runner LaunchAgent io.mr-pinpin.apps-runner (takes the bench lock)
bash tools/sync.sh                     # VPS <-> Mini repo <-> GitHub (cron, every 10 min)
```
The assistant service is set in `~/pinpin-apps-data/assistant-url` on the Mini; empty means the stub. API keys
for the proxy go in `~/pinpin-apps-data/secrets.json` as `{"host": {"headers": {...}, "query": {...}}}` and are
never committed.

## Milestones

- [x] M0: house.json `apps` section agreed (work-c4), validated in house/schema.py, live on the Mini (quest-wall-screen 001233b)
- [x] M1: repo, app format and manifest validation; SDK (big-type layout, voice hook, storage, proxy, ask)
- [x] M2: server with the create/update/rollback API (a commit per change), screen mapping, events, proxy, ask stub; 16 offline tests against the real house server
- [x] M3: starter apps: weather (open-meteo, no key, "weather in <city>") and kitchen recipes (voice ingredients → Claude or the built-in list, one step at a time)
- [x] M4: the server on the Mini (LaunchAgent io.mr-pinpin.apps), with headless Chrome render checks of both apps (`tools/render_check.py`, and `check: true` on PUT)
- [x] M5: the runner on the laptop (LaunchAgent io.mr-pinpin.apps-runner). `tools/air_test.sh` passed twice (2026-11-04 02:28 and 02:35 UTC):
  - the window sits exactly on scratch display 47 (from Chrome's own bounds) and the page connects in 1 s;
  - voice reaches the app;
  - `/open` of a URL lands in 1 s, and the window goes back to the app afterwards;
  - the window closes when the app is unassigned;
  - air_smoke passes at 50 fps.
  Finding: Chrome started over ssh can't reach the GUI session, so the runner must be the Aqua LaunchAgent.
- [x] M5b: `POST /api/screens/<id>/open {url}` for the assistant's open_url (one runner for apps and web pages, navigated over CDP)
- [x] M6: /api/ask forwarded to work-42's assistant (now the Air instance, 192.168.0.102:57840; the Mini instance when it's up). Recipes answered by Claude in 15–30 s, with built-in ideas shown at once
- [ ] M7: headset dictation to an app screen (`/say`): work-c4 is adding an app-mic button beside the Claude button (ASSIST flag)
- [x] M10: catalog for the headset's app picker: icons (SVG data URI, plus a PNG rendered on the Mini), commit per app; protocol in docs/HEADSET.md (sent to work-c4)
- [x] M11: more starter apps: shopping list, clock (sunrise/sunset), notes board. Each passed the render check with scripted voice
- [x] M12: input over CDP (`/api/screens/<id>/click|move|scroll|type|key`), because synthetic CGEvent clicks never reach Chrome --app windows. `tools/input_test.sh` passed on the laptop at 03:2x UTC:
  - a click at display u,v opened example.com's link;
  - scroll moved 900 px;
  - click, type and Enter ran a Wikipedia search;
  - air_smoke passed.
- [x] M13: hardening.
  - Apps run in an opaque-origin sandboxed iframe, with host.js brokering store, fetch and ask for that one app.
  - API guards: Origin null is refused, JSON only, X-PinPin header.
  - The proxy follows no redirects and refuses the LAN, loopback and userinfo tricks.
  - Upload size and stall limits.
  - render_check's Chrome reaches only the apps server.
- [x] M14: control page at `/` (gallery with live thumbnails, screens with app dropdowns); offline badges in weather, clock and recipes
- [ ] M9: an app on a real screen: waits for the user to say which screen is the kitchen one
- [ ] M8: the assistant creates a new app end to end ("make me a timer app") and it appears on a screen. Rehearsed through the API: `apps/timer` was made from docs/AUTHORING.md with `check: true`, rendered with no errors, and its voice worked
