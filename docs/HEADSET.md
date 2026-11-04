# Apps on the headset: the picker protocol (for room_lab)

Covers the SCREENS → <screen> → APP… menu (shoulder and wrist) and app dictation. Everything is plain HTTP+JSON
on the Mini, `http://192.168.0.50:57830`, LAN only, no auth. All calls are idempotent and cheap (under 50 ms,
except the first icon render).

## 1. Catalog: `GET /api/apps`

```json
[{"id": "recipes", "name": "Kitchen recipes",
  "description": "Say the ingredients you have; it suggests recipes ...",
  "icon": "data:image/svg+xml;base64,...", "icon_png": "/api/apps/recipes/icon.png", "commit": "fa909d9c1b2e",
  "size": {"aspect": "16:10", "preferred_px": [1920, 1200], "distance_m": 1.5},
  "permissions": {"voice": true, "ask": true, "fetch": [], "storage": true}, "screens": [2]}, ...]
```
- Sorted by id. Show `name` big, and the first sentence of `description` as the one-line hint.
- Icon: `GET <icon_png>?size=128`, or 256 / 512, gives a square RGBA PNG with transparent corners. Every app has one;
  apps without their own get a generated letter tile. Cache it by `(id, commit, size)`. It only changes when
  `commit` changes. The first fetch after a change renders in about 0.6 s, then it's a file read.
- `icon` is the same image as an SVG data URI, for clients that can draw SVG.
- `screens`: the screen ids showing this app now, so you can tick the current choice.
- Starter apps (2026-11-04): `clock`, `notes`, `recipes`, `shopping`, `timer`, `weather`. The assistant can add more
  at any time, so re-fetch the catalog each time the APP… menu opens.

## 2. Screens: `GET /api/screens`

```json
[{"screen": 2, "app": "weather", "display": 2, "role": "extra", "on": 1, "url": null, "seq": 0, "shown_seq": 0, "clients": 1}]
```
- `app`: the app on that screen, or `null`.
- `url`: a web page the assistant opened there (it takes precedence over the app).
- `clients` ≥ 1: the page is live in the runner's window on the laptop.

## 3. Choose: `POST /api/screens/<screen id>/app`

- Body `{"app": "recipes"}`, or `{"app": null}` for the **None** item.
- Reply: `{"screen": 2, "app": "recipes", "house_rev": 12}`.
  - The mapping is stored in house.json's top-level `apps` section (the headset never writes it; read-only for you).
  - The window on that screen's laptop display shows the app within about 3 s; nothing to do on the headset.
  - It clears any opened `url` on that screen.
- Errors:
  - 400 `{"error": "no app 'x'", "apps": [...]}`;
  - 502 if the house server is down. Show "Couldn't save, try again".
- **Only for screens whose `source.kind` is `mac-display`** (the extra screens). The main screen (id 1) streams the
  game/Atlas: don't offer APP… there.
- A screen with an app should keep `source = {kind: mac-display, screen: N}`: the app is drawn on laptop display N.

## 4. App dictation: `POST /api/screens/<screen id>/say`

- Body `{"text": "eggs, tomatoes and an onion"}`.
- Reply `{"delivered": 1}`, or 404 with `{"delivered": 0}` when nothing is showing on that screen.
- One phrase per call, as the ASR segments them. Every app shows its own "Say …" hint line.
- The Claude button is separate: it talks to work-42's per-screen session, not to the app.

## Menu layout suggestion

- Root: APP… (only on extra screens).
- Grid of tiles: icon 128 px, name under it, one-line hint on focus; a **None** tile first; the current app ticked.
- After a choice, close the submenu and show a toast "Recipes on screen 2". The screen itself changes ~3 s later.

## Test without the headset

```
curl -s 192.168.0.50:57830/api/apps | python3 -m json.tool | head
curl -s -X POST -H 'Content-Type: application/json' -d '{"app":"clock"}' 192.168.0.50:57830/api/screens/47/app
```
Use screen 47 (a scratch id) for tests, not 2/3; real screens get apps only when the user asks.
