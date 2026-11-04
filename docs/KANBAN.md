# Kanban: the captain's board

One board for the captain (work-df), the agents and the user: **http://192.168.0.50:57830/kanban** (desktop: five
columns, drag cards between them; phone: one column at a time, tabs on top). Columns: **Backlog, Next, Doing, Review,
Done**. A card has a title, details, owner (agent name, e.g. `work-c4`), area, links, created/updated, comments and a
history of every change.

## CLI (VPS, any user with sudo)

```
export PINPIN_WHO=work-42          # who you are (or --by work-42)
kanban list                        # by column; Done only with --all or -c done;  -o OWNER  -a AREA
kanban show K3                     # details, links, comments, history
kanban add "Room screenshot crash" -o work-42 -a assistant -c next -d "details…" -l qws:abc1234
kanban move K3 doing  [-m "note"]  # backlog next doing review done
kanban update K3 -o work-c4 -l apps:def5678     # -t TITLE -d DETAILS -a AREA; -l adds a link (--set-links replaces)
kanban comment K3 "shipped in v0.56, please review"
kanban md                          # the board as markdown
```

It reaches the Mini through `ssh pinpin-mini` (root's key; it re-runs itself with `sudo -n`), and the Mini supplies
its own token. Elsewhere: `PINPIN_KANBAN_URL=http://192.168.0.50:57830 PINPIN_TOKEN=… tools/kanban …`.

## API (Mini :57830; writes need the token: `Authorization: Bearer <token>`, JSON bodies)

| | |
|---|---|
| `GET /api/kanban[?column=&owner=&area=&since=REV]` | the board `{rev, updated, cards}`; `since` = current rev → `{unchanged: true}` |
| `GET /api/kanban/K3` | one card |
| `GET /api/kanban/md` | markdown |
| `POST /api/kanban` | `{title, details?, column?, owner?, area?, links?, by}` → 201, the card |
| `POST /api/kanban/K3/move` | `{column, note?, by}` |
| `POST /api/kanban/K3/update` | `{title?, details?, owner?, area?, links? (replace), add_links?, by}` |
| `POST /api/kanban/K3/comment` | `{text, by}` |

Links: URLs, `qws:<sha>` / `apps:<sha>` (commits), `qws:<path>` / `apps:<path>` (files); the board links them to GitHub.
Limits: title 200, details 4000, comment 2000 characters, 20 links.

## Data

`~/pinpin-apps-data/kanban.json` on the Mini, written crash-safe (temp file, fsync, rename; the previous copy kept as
`kanban.json.1` and used if the file is ever unreadable), in the hourly data backup. Every 5 minutes
`tools/kanban_mirror.sh` commits it to quest-wall-screen `docs/captain/kanban.json` and regenerates
`docs/captain/KANBAN.md` (read-only: edits there are overwritten), so the board has git history. /status shows the
Doing and Review cards and the latest Done ones.
